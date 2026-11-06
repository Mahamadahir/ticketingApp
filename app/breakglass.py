"""Break-glass accounts (#163): two local super admins that are not anyone's Entra account, so there is
always a way in when Entra is down or an admin has been locked out.

Created and reset only by a command, never from a web page, so no secret ever crosses the web app:

    python -m app.breakglass create <name>    (also: reset <name>, unlock <name>, list)

`create` prints the password, the TOTP secret and an otpauth:// URI once; they go into IT Glue and are
never shown again. The password is stored as a bcrypt hash, like any local account. The TOTP secret has
to be readable to check codes, so it is stored encrypted (Fernet) with a key derived from
SESSION_SECRET_KEY. Changing that key makes the stored secrets unreadable: run `reset` on each account.

Sign-in is its own form (/auth/break-glass, web app only): username, password and the current code
(RFC 6238: SHA-1, 6 digits, 30 seconds; one step either side is accepted; a step is accepted once).
The ordinary local sign-in refuses these accounts, so a password alone is never enough. Failures count
towards the same lockout as local accounts (app/auth.py).

Nothing here logs, prints (except the one-time command output) or audits a password, a code or a secret.
"""

import argparse
import base64
import hashlib
import hmac
import logging
import re
import secrets
import struct
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlencode

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import update
from sqlmodel import Session, select

from app.auth import _DUMMY_HASH, _is_locked, _record_failed_login, _verify_password, hash_password
from app.audit import log_action
from app.config import settings
from app.database import engine
from app.models import User, UserRole
from app.sessions import revoke_user_sessions

logger = logging.getLogger(__name__)

# Not an address that could match an Entra user (.invalid is reserved and never resolves).
BREAK_GLASS_DOMAIN = "local.invalid"
# create is refused once this many exist (a constant, not a setting: a third account is a decision, not a tweak).
MAX_BREAK_GLASS_ACCOUNTS = 2
MIN_PASSWORD_CHARS = 24
PASSWORD_BYTES = 24  # token_urlsafe(24) is 32 characters, about 192 bits
TOTP_SECRET_BYTES = 20  # 160 bits, the RFC 4226 recommendation
TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_WINDOW = 1  # steps either side of now that are accepted
ISSUER = "IT Support Ticketing"
AUDIT_ACTOR = "break-glass command"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")


class BreakGlassError(Exception):
    """A command refusal; the message is shown to the person running it."""


# --- TOTP (RFC 6238 over RFC 4226 HOTP) --------------------------------------------------------------

def hotp(secret: bytes, counter: int) -> str:
    mac = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 10**TOTP_DIGITS:0{TOTP_DIGITS}d}"


def time_step(moment: datetime) -> int:
    return int(moment.timestamp()) // TOTP_PERIOD


def totp_code(secret: bytes, moment: datetime) -> str:
    return hotp(secret, time_step(moment))


def matching_step(secret: bytes, code: str, moment: datetime) -> Optional[int]:
    """The time step this code belongs to, within one step either side of now, or None. Every candidate is
    compared (constant-time) whether or not an earlier one matched."""
    code = "".join(code.split())
    found = None
    now_step = time_step(moment)
    for step in range(now_step - TOTP_WINDOW, now_step + TOTP_WINDOW + 1):
        if hmac.compare_digest(hotp(secret, step).encode(), code.encode()):
            found = step
    return found


def encode_secret(secret: bytes) -> str:
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def otpauth_uri(email: str, secret: bytes) -> str:
    query = urlencode({"secret": encode_secret(secret), "issuer": ISSUER, "algorithm": "SHA1", "digits": TOTP_DIGITS, "period": TOTP_PERIOD})
    return f"otpauth://totp/{quote(ISSUER)}:{quote(email)}?{query}"


# --- Storing the secret ------------------------------------------------------------------------------

def _fernet() -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"ticketing break-glass totp v1").derive(
        settings.session_secret_key.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(secret: bytes) -> str:
    return _fernet().encrypt(secret).decode("ascii")


def decrypt_secret(stored: str) -> Optional[bytes]:
    try:
        return _fernet().decrypt(stored.encode("ascii"))
    except (InvalidToken, ValueError):
        return None


# --- Signing in --------------------------------------------------------------------------------------

@dataclass
class BreakGlassResult:
    user: Optional[User] = None
    # unknown_account, account_locked, wrong_password, wrong_code, code_reused or secret_unreadable. For the logs
    # only; the person at the form sees one message.
    failure: Optional[str] = None
    locked_until: Optional[datetime] = None


def attempt_break_glass_login(
    session: Session, username: str, password: str, code: str, now: Optional[datetime] = None
) -> BreakGlassResult:
    now = now or datetime.now(timezone.utc)
    user = session.exec(select(User).where(User.email == username.strip().lower(), User.is_break_glass)).first()
    if user is None or not user.password_hash or not user.totp_secret:
        _verify_password(password, _DUMMY_HASH)  # the same bcrypt time as a real check
        return BreakGlassResult(failure="unknown_account")

    password_ok = _verify_password(password, user.password_hash)
    if _is_locked(user, now):
        return BreakGlassResult(failure="account_locked")  # attempts during a lock change nothing

    secret = decrypt_secret(user.totp_secret)
    if secret is None:
        # The stored secret cannot be read (SESSION_SECRET_KEY changed): not the person's fault, so no strike.
        logger.error("Break-glass TOTP secret for account %s cannot be decrypted; run `reset`", user.id)
        return BreakGlassResult(failure="secret_unreadable")
    step = matching_step(secret, code, now)

    failure = None
    if not password_ok:
        failure = "wrong_password"
    elif step is None:
        failure = "wrong_code"
    elif not _claim_step(session, user, step):
        failure = "code_reused"
    if failure is not None:
        locked_until = _record_failed_login(session, user, now)
        session.commit()
        return BreakGlassResult(failure=failure, locked_until=locked_until)

    user.failed_login_count = 0
    user.locked_until = None
    session.add(user)
    session.commit()
    return BreakGlassResult(user=user)


def _claim_step(session: Session, user: User, step: int) -> bool:
    """Records the step as used, only if it is newer than the last one accepted. One statement, so two
    requests with the same code cannot both succeed."""
    claimed = session.exec(
        update(User)
        .where(User.id == user.id, (User.totp_last_step.is_(None)) | (User.totp_last_step < step))
        .values(totp_last_step=step)
    )
    return bool(claimed.rowcount)


# --- The commands ------------------------------------------------------------------------------------

def email_for(name: str) -> str:
    name = name.strip().lower()
    suffix = "@" + BREAK_GLASS_DOMAIN
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    if not NAME_PATTERN.match(name):
        raise BreakGlassError("The name must be 2 to 31 characters: lower case letters, digits and hyphens, e.g. breakglass1.")
    return name + suffix


@dataclass
class Issued:
    """What a command prints once. Never stored or logged."""
    email: str
    password: str
    totp_secret: str
    uri: str


def _issue(user: User) -> Issued:
    password = secrets.token_urlsafe(PASSWORD_BYTES)
    assert len(password) >= MIN_PASSWORD_CHARS
    secret = secrets.token_bytes(TOTP_SECRET_BYTES)
    user.password_hash = hash_password(password)
    user.totp_secret = encrypt_secret(secret)
    user.totp_last_step = None
    user.failed_login_count = 0
    user.locked_until = None
    return Issued(user.email, password, encode_secret(secret), otpauth_uri(user.email, secret))


def break_glass_accounts(session: Session) -> list[User]:
    return list(session.exec(select(User).where(User.is_break_glass).order_by(User.email)).all())


def create_account(session: Session, name: str) -> Issued:
    email = email_for(name)
    if len(break_glass_accounts(session)) >= MAX_BREAK_GLASS_ACCOUNTS:
        raise BreakGlassError(f"There are already {MAX_BREAK_GLASS_ACCOUNTS} break-glass accounts. Use `reset` to reissue one.")
    if session.exec(select(User).where(User.email == email)).first() is not None:
        raise BreakGlassError(f"{email} already exists.")
    user = User(email=email, display_name=f"Break-glass {email.split('@')[0]}", role=UserRole.SUPER_ADMIN, is_break_glass=True)
    issued = _issue(user)
    session.add(user)
    session.flush()
    log_action(session, AUDIT_ACTOR, "user", user.id, "break_glass_created", email)
    session.commit()
    return issued


def _existing(session: Session, name: str) -> User:
    email = email_for(name)
    user = session.exec(select(User).where(User.email == email, User.is_break_glass)).first()
    if user is None:
        raise BreakGlassError(f"No break-glass account {email}.")
    return user


def reset_account(session: Session, name: str) -> Issued:
    """A new password and a new TOTP secret; the old ones stop working, every session ends and any lock is cleared."""
    user = _existing(session, name)
    issued = _issue(user)
    session.add(user)
    ended = revoke_user_sessions(session, user.id)
    log_action(session, AUDIT_ACTOR, "user", user.id, "break_glass_reset", f"password and TOTP secret replaced; {ended} session(s) ended")
    session.commit()
    return issued


def unlock_account(session: Session, name: str) -> None:
    user = _existing(session, name)
    user.failed_login_count = 0
    user.locked_until = None
    session.add(user)
    log_action(session, AUDIT_ACTOR, "user", user.id, "unlocked", "break-glass command")
    session.commit()


def describe_accounts(session: Session) -> list[str]:
    now = datetime.now(timezone.utc)
    lines = []
    for user in break_glass_accounts(session):
        state = ["active" if user.is_active else "INACTIVE"]
        if _is_locked(user, now):
            state.append(f"locked until {user.locked_until.isoformat()}")
        lines.append(f"{user.email}  {', '.join(state)}  created {user.created_at:%Y-%m-%d}")
    return lines


def _print_issued(issued: Issued) -> None:
    print(f"Account:      {issued.email}")
    print(f"Password:     {issued.password}")
    print(f"TOTP secret:  {issued.totp_secret}  (base32, SHA-1, 6 digits, 30 seconds)")
    print(f"otpauth URI:  {issued.uri}")
    print()
    print("These are shown once and are not stored anywhere readable. Put them in IT Glue now.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.breakglass", description="Manage the break-glass accounts.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "reset", "unlock"):
        commands.add_parser(command).add_argument("name", help="for example breakglass1")
    commands.add_parser("list")
    args = parser.parse_args(argv)
    try:
        with Session(engine) as session:
            if args.command == "create":
                _print_issued(create_account(session, args.name))
            elif args.command == "reset":
                _print_issued(reset_account(session, args.name))
            elif args.command == "unlock":
                unlock_account(session, args.name)
                print("Unlocked.")
            else:
                lines = describe_accounts(session)
                print("\n".join(lines) if lines else "No break-glass accounts.")
    except BreakGlassError as refusal:
        print(str(refusal), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
