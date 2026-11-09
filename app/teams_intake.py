"""Ticket intake from Teams (phase 8D): a person messages the bot and a ticket is raised or added to.

The bot (app/teams_bot.py) has already checked the token and the tenant. This module is what a direct message means, and
it follows the email intake (app/inbound.py) wherever it can; the rules both channels share live in app/intake.py.

Who. The Entra object id Teams sends (`from.aadObjectId`), through `identity.person_for_object_id`. A person with no
account yet is looked up in the chat with the bot (`bot_connector.get_member`, the bot's own token: it gives the address),
then matched or created exactly as an Entra sign-in is (`identity.resolve_sso_user`: the id is stored on an account that has
the address, or a new end-user account is made), so they are one person whichever way they come. Never a requester: the
shared mailbox, an address on the never-follow list (their words are ignored, audited), an inactive account (an
anonymised one is inactive and has a placeholder address). A person who cannot be matched is told so and nothing is made.

Which ticket. `new:` at the start of a message always raises a new ticket. Otherwise the person's open tickets that were
raised in Teams decide: none, a new ticket; one, the message is added to it; several, a card asks which (their open
requests and "a new request"), and the press answers in place (`answer_card`). With none open but some Resolved, a
thank-you (the same rules as email, app/thanks_rules.py) is added to the latest Resolved one and leaves it resolved; anything
else asks, since adding to a Resolved ticket reopens it. A Closed ticket is never added to: the next message raises a
new one. A message from Teams never joins a ticket the person did not raise.

What is made. A new ticket is `source=teams`, created by `ticket_service.create_ticket` in New for triage like email, its
subject the first line of the message and the whole message its first public message; the standard acknowledgement text
(the admin-edited template) is the bot's answer, in the chat. A reply is recorded by `intake.record_reply`, so the
assignee is told, the status moves and the followers are updated as for mail. Files are fetched within email's limits (how
many, how large) and stored by `intake.store_files`; what is not kept leaves an event and is named in the answer. Files
sent with a message that needs the choice card are not kept (the person is told to send them again): the address Teams
gives is short-lived and there is nowhere to hold the file before there is a ticket.

Never twice. `TeamsIntake` holds one row per message, unique on a keyed hash of the person and the activity id, claimed in the
transaction that makes the ticket, so Teams sending an activity again after a slow answer does nothing. The same row
makes a card answerable once. Rate limits: one person's tickets, replies and questions in an hour are capped by an admin
setting, counted from the rows whose ticket is theirs (a question carries the ticket it is about), so the table holds no
name or address; and a burst limit per minute (app/rate_limit.py) keeps one person from making the bot answer without end.

Answers are sent after the response (`Handled.after`), a card press is answered in the response itself. A failure while
handling is rolled back whole and the person is told to try again or use the site.
"""

import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app import bot_connector
from app.attachments import clean_filename
from app.bot_cards import VERB_NEW, VERB_PICK, choose_ticket_card, intake_result_card
from app.config import settings
from app.email_templates import render_template, ticket_values
from app.follower_updates import queue_follower_updates
from app.followers import InvalidEmail, normalise_email
from app.forms import MAX_MESSAGE_LENGTH
from app.identity import SsoRefused, normal_oid, person_for_object_id, requester_emails, resolve_sso_user
from app.intake import (
    IncomingAttachment, clean_body, clean_subject, clean_text, ignored_as_never_follow, record_first_message, record_reply, store_files,
)
from app.models import FINISHED_STATUSES, Ticket, TicketSource, TicketStatus, TeamsIntake, User
from app.person_data import is_placeholder_address
from app.rate_limit import TEAMS_PERSON, TEAMS_PERSON_NOTICE
from app.settings_store import INBOUND_MAX_ATTACHMENTS, TEAMS_MAX_PER_PERSON_HOUR, count_setting
from app.storage import Storage
from app.teams_approvals import ACTION_NAME, card_response, message_response
from app.teams_bot import Handled, _person, _sub, _text
from app.teams_conversations import save_reference
from app.thanks_rules import is_thanks_reply
from app.ticket_service import create_ticket

log = logging.getLogger("app.teams_bot")

# What came of a message.
TICKET_CREATED = "ticket_created"
REPLY_ADDED = "reply_added"
ASKED = "asked"  # a card is waiting for the person to choose
IGNORED_NEVER_FOLLOW = "ignored_never_follow"

NEW_PREFIX = re.compile(r"^\s*new\s*:\s*", re.IGNORECASE)
FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"
MAX_PENDING_TEXT = 3000  # the longest message that can wait inside a choice card
MAX_CARD_BYTES = 20_000  # the card carries the message once on each button; Teams refuses an activity over about 28 KB
FILES_BUDGET_SECONDS = 20.0  # all the downloads of one message together: Teams waits about 15 seconds for the answer
MAX_NOT_KEPT_SHOWN = 5
FILES_ONLY = "(files only)"

HELP = (
    "Message me what you need and I will raise a request with IT. While you have one open, what you write next is added to "
    "it; if you have several I will ask which. Start a message with new: to raise a separate request."
)
COULD_NOT_MATCH = "I could not work out who you are from Teams, so I have not raised anything. Please use the ticketing site for now."
NOT_ACTIVE = "Your account is not active, so I cannot raise requests for you. Please contact the IT service desk."
NEW_NEEDS_TEXT = "Write what you need after new: and I will raise it as a new request."
TOO_LONG_TO_ASK = (
    "You have more than one open request and this message is too long for me to hold while you choose. Start it with new: "
    "to raise a new request, or add to a request on the ticketing site."
)
FILES_NOT_KEPT_WHILE_ASKING = "Files sent with this message are not kept while you choose. Send them again once it is added."
SLOW_DOWN = "You have sent a lot of messages in the last hour, so I have not added this one. Please try again later or use the ticketing site."
FAILED = "Something went wrong and nothing was recorded. Please try again, or use the ticketing site."
CARD_FAILED = "I could not show you the choice of request. Please send your message again, starting with new: for a new request."
ALREADY_DONE = "This question has already been answered."
NO_LONGER_OPEN = "That request is not open any more. Send your message again, starting with new: for a new request."

@dataclass
class Result:
    """What was done for a message: the outcome, the ticket and message it made or reached, and the text to answer with."""

    outcome: str
    text: str
    ticket: Optional[Ticket] = None


# ---- Who -----------------------------------------------------------------------------------------------


def activity_key(oid: str, activity_id: str) -> str:
    """The key of a message: the person and the Teams activity id, keyed with the session secret (as the anonymisation
    hash is), so the table cannot be matched against a list of guessed object ids and message ids without it. Rotating the
    secret only means an activity Teams sends again within the minutes it takes is no longer recognised."""
    key = settings.session_secret_key.encode("utf-8")
    return hmac.new(key, f"teams-intake:{oid}:{activity_id}".encode("utf-8"), hashlib.sha256).hexdigest()


def _member_address(activity: dict, oid: str) -> tuple[Optional[str], str]:
    """(address, name) of the person who wrote, from the chat with the bot. The address is None when Teams gave none usable,
    or described someone else."""
    sender = activity.get("from") if isinstance(activity.get("from"), dict) else {}
    name = _text(sender.get("name"))
    try:
        member = bot_connector.get_member(_text(activity.get("serviceUrl")), _text(_sub(activity, "conversation", "id")), _text(sender.get("id")))
    except Exception as error:  # httpx, the token credential or a bad address: say so below
        log.warning("Teams intake: could not look the sender up (%s)", type(error).__name__)
        return None, name
    member_oid = normal_oid(_text(member.get("objectId")) or None)
    if member_oid is not None and member_oid != oid:
        log.warning("Teams intake: the chat member is not the sender, so nothing was made")
        return None, name
    raw = _text(member.get("email")) or _text(member.get("userPrincipalName"))
    try:
        return normalise_email(raw), _text(member.get("name")) or name
    except InvalidEmail:
        return None, name


def requester_of(session: Session, activity: dict, oid: str) -> tuple[Optional[User], Optional[str]]:
    """(the person, None), or (None, what to tell them) when this message cannot become a ticket. (None, None) means ignore
    it quietly (the shared mailbox, a never-follow address)."""
    person = person_for_object_id(session, oid)
    if person is None:
        address, name = _member_address(activity, oid)
        if address is None:
            return None, COULD_NOT_MATCH
        try:
            person = resolve_sso_user(session, oid, address, name)
        except SsoRefused as refused:
            session.commit()  # the refusal is audited
            return None, refused.message
        # Their chat with the bot is theirs now: it is found by the account too.
        save_reference(session, oid, _text(activity.get("serviceUrl")), _text(_sub(activity, "conversation", "id")), _person(activity)[1])
    if not person.is_active or is_placeholder_address(person.email):
        return None, NOT_ACTIVE
    mailbox = settings.graph_shared_mailbox.strip().lower()
    if mailbox and person.email.strip().lower() == mailbox:
        return None, None
    return person, None


def _over_limit(session: Session, person: User, now: datetime) -> bool:
    count = session.exec(
        select(func.count()).select_from(TeamsIntake).join(Ticket, Ticket.id == TeamsIntake.ticket_id).where(
            func.lower(func.trim(Ticket.requester_email)).in_(requester_emails(person, session)),
            TeamsIntake.created_at >= now - timedelta(hours=1),
        )
    ).one()
    return count >= count_setting(session, TEAMS_MAX_PER_PERSON_HOUR)


# ---- Which ticket --------------------------------------------------------------------------------------


def teams_tickets(session: Session, person: User, resolved: bool = False) -> list[Ticket]:
    """The person's tickets that were raised in Teams and are open (or, with `resolved`, Resolved), the most recently active
    first. A Closed ticket is never one of them."""
    status = Ticket.status == TicketStatus.RESOLVED if resolved else Ticket.status.not_in(FINISHED_STATUSES)
    return list(
        session.exec(
            select(Ticket)
            .where(
                Ticket.source == TicketSource.TEAMS,
                Ticket.split_from_id.is_(None),  # a part of a split request (or an automatic sub-request) is not a conversation of its own
                func.lower(func.trim(Ticket.requester_email)).in_(requester_emails(person, session)),
                status,
            )
            .order_by(Ticket.updated_at.desc(), Ticket.id.desc())
        ).all()
    )


NEW, REPLY, ASK = "new", "reply", "ask"


@dataclass
class Plan:
    kind: str
    ticket: Optional[Ticket] = None
    choices: Optional[list[Ticket]] = None
    text: str = ""  # the message without a new: prefix


def plan_for(session: Session, person: User, text: str, has_files: bool) -> Plan:
    prefix = NEW_PREFIX.match(text)
    if prefix:
        return Plan(NEW, text=text[prefix.end():].strip())
    text = text.strip()
    open_tickets = teams_tickets(session, person)
    if len(open_tickets) == 1:
        return Plan(REPLY, open_tickets[0], text=text)
    if open_tickets:
        return Plan(ASK, choices=open_tickets, text=text)
    resolved = teams_tickets(session, person, resolved=True)
    if not resolved:
        return Plan(NEW, text=text)
    # Only Resolved tickets are left. A thank-you is added to the latest and leaves it resolved (app/thanks_rules.py); any
    # other message would reopen one of them, so the person chooses.
    if is_thanks_reply(session, text, has_files):
        return Plan(REPLY, resolved[0], text=text)
    return Plan(ASK, choices=resolved, text=text)


# ---- Files ---------------------------------------------------------------------------------------------


def offered_files(activity: dict) -> list[dict]:
    """The files in an activity: an uploaded file (`file.download.info`) or a picture pasted into the chat (an image with a
    `contentUrl`). Teams also adds an HTML copy of the text as an attachment; that is not a file."""
    found = []
    for item in activity.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("contentType"))
        if kind == FILE_DOWNLOAD_INFO and isinstance(item.get("content"), dict):
            found.append({"name": _text(item.get("name")), "url": _text(item["content"].get("downloadUrl")), "token": False})
        elif kind.startswith("image/") and _text(item.get("contentUrl")):
            found.append({"name": _text(item.get("name")) or "image", "url": _text(item.get("contentUrl")), "token": True})
    return found


def load_files(session: Session, activity: dict) -> list[IncomingAttachment]:
    """The files fetched within email's limits: how many (the same setting), how large each is (the same limit). One over a
    limit, or that cannot be fetched, is listed as skipped with why, and never kept. app/attachments.py then decides what may be
    stored."""
    limit_mb = settings.attachment_max_bytes // (1024 * 1024)
    max_files = count_setting(session, INBOUND_MAX_ATTACHMENTS)
    session.commit()  # what was read and made so far is kept, and no transaction (no pooled connection) is held open while Teams answers
    found: list[IncomingAttachment] = []
    deadline = time.monotonic() + FILES_BUDGET_SECONDS
    for index, item in enumerate(offered_files(activity)):
        name = clean_filename(clean_text(item["name"]))
        if index >= max_files:
            found.append(IncomingAttachment(name, skipped=f"more than {max_files} files on one message"))
            continue
        if time.monotonic() > deadline:
            found.append(IncomingAttachment(name, skipped="took too long to download"))
            continue
        try:
            data = bot_connector.fetch_file(
                item["url"], settings.attachment_max_bytes, service_url=_text(activity.get("serviceUrl")), with_token=item["token"]
            )
        except bot_connector.FileRefused as refused:
            reason = f"larger than the {limit_mb} MB limit" if "larger" in str(refused) else str(refused)
            found.append(IncomingAttachment(name, skipped=reason))
        except Exception as error:  # the network, or Teams refused the address
            log.warning("Teams intake: a file could not be fetched (%s)", type(error).__name__)
            found.append(IncomingAttachment(name, skipped="could not be downloaded"))
        else:
            found.append(IncomingAttachment(name, data=data))
    return found


# ---- Doing it ------------------------------------------------------------------------------------------


def _claim(session: Session, key: str, outcome: str, ticket_id: Optional[int] = None) -> Optional[TeamsIntake]:
    """Records that this message is being handled, or returns None if it already was. The unique key decides, so two
    deliveries at once cannot both win: the loser waits for the winner's commit and then fails here."""
    row = TeamsIntake(activity_key=key, outcome=outcome, ticket_id=ticket_id)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return None
    return row


def _first_line(text: str, files: list[IncomingAttachment]) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    return f"File: {files[0].filename}" if files else ""


def _not_kept_note(not_kept: list[str]) -> str:
    if not not_kept:
        return ""
    shown = "; ".join(not_kept[:MAX_NOT_KEPT_SHOWN])
    more = f" and {len(not_kept) - MAX_NOT_KEPT_SHOWN} more" if len(not_kept) > MAX_NOT_KEPT_SHOWN else ""
    return f"\n\nNot kept: {shown}{more}."


def act(
    session: Session, person: User, row: TeamsIntake, plan: Plan, files: list[IncomingAttachment], storage: Optional[Storage] = None
) -> Result:
    """Makes the ticket or adds the message, as the plan says, and settles the claim. Adds everything to the session; the
    caller commits, so the claim, the ticket, the message and the files stand or fall together."""
    sender = person.email.strip().lower()
    body = plan.text
    if plan.kind == REPLY:
        ticket = plan.ticket
        before = ticket.status
        message = record_reply(
            session, ticket, sender=sender, body=clean_body_of(body, files), author_id=person.id, text=body,
            has_attachment=bool(files), action="replied_by_teams", how="from a message to the bot",
        )
        not_kept = store_files(session, storage, ticket, message, files)
        queue_follower_updates(session, ticket, message, sender)
        row.outcome = REPLY_ADDED
        text = f"Added to {ticket.reference}."
        if before == TicketStatus.RESOLVED and ticket.status != TicketStatus.RESOLVED:
            text += " It has been reopened."
    else:
        ticket = create_ticket(session, subject=clean_subject(_first_line(body, files)), requester_email=sender, source=TicketSource.TEAMS)
        message = record_first_message(
            session, ticket, sender=sender, body=clean_body_of(body, files), author_id=person.id, action="created_by_teams"
        )
        not_kept = store_files(session, storage, ticket, message, files)
        row.outcome = TICKET_CREATED
        text = render_template(session, "acknowledgement", ticket_values(ticket)).body
    row.ticket_id, row.message_id = ticket.id, message.id
    session.add(row)
    return Result(row.outcome, text + _not_kept_note(not_kept), ticket)


def clean_body_of(text: str, files: list[IncomingAttachment]) -> str:
    return clean_body(text) if text.strip() or not files else FILES_ONLY


def _reply(activity: dict, *, text: Optional[str] = None, card: Optional[dict] = None) -> bool:
    """Answers the person in their chat. True when it went; a failure is logged and never raised (nobody is waiting)."""
    try:
        bot_connector.reply_to(activity, text=text, card=card)
    except Exception as error:
        log.warning("Teams bot: reply failed: %s", type(error).__name__)
        return False
    return True


def _say(activity: dict, text: str) -> Handled:
    return Handled(after=lambda: _reply(activity, text=text))


def answer(session: Session, activity: dict, storage: Optional[Storage] = None) -> Optional[Handled]:
    """What a direct message to the bot does: raises a ticket, adds to one, or asks which. Called by the `message` handler
    once the conversation is stored. Returns what to answer after the response, or None for nothing."""
    oid = normal_oid(_person(activity)[0])
    activity_id = _text(activity.get("id"))
    text = clean_text(activity.get("text")).strip()
    if oid is None or not activity_id:
        return None
    if TEAMS_PERSON.hit(oid) is not None:
        # Whatever the message is (a refusal, the help, a question), one person cannot make the bot answer without end.
        return _say(activity, SLOW_DOWN) if TEAMS_PERSON_NOTICE.hit(oid) is None else None
    now = datetime.now(timezone.utc)
    key = activity_key(oid, activity_id)
    if session.exec(select(TeamsIntake.id).where(TeamsIntake.activity_key == key)).first() is not None:
        return None  # Teams sent it again: it was handled
    try:
        person, refusal = requester_of(session, activity, oid)
        if person is None:
            session.commit()
            return _say(activity, refusal) if refusal else None
        if ignored_as_never_follow(session, person.email, "a message to the Teams bot", now):
            session.commit()
            return None
        offered = offered_files(activity)
        if not text and not offered:
            session.commit()
            return _say(activity, HELP)
        if text.lower().rstrip("!. ") in ("help", "?"):
            session.commit()
            return _say(activity, HELP)
        if _over_limit(session, person, now):
            log.warning("Teams intake: one person's messages set aside, too many tickets and replies in an hour")
            session.commit()
            return _say(activity, SLOW_DOWN) if TEAMS_PERSON_NOTICE.hit(oid) is None else None
        plan = plan_for(session, person, text, bool(offered))
        if plan.kind == NEW and not plan.text and not offered:
            session.commit()
            return _say(activity, NEW_NEEDS_TEXT)
        if plan.kind == ASK:
            return _ask(session, activity, key, plan, bool(offered))
        files = load_files(session, activity) if offered else []  # before the claim: a slow download is not a lock
        row = _claim(session, key, "processing")
        if row is None:
            session.rollback()
            return None
        result = act(session, person, row, plan, files, storage)
        session.commit()
    except Exception:
        log.exception("Teams intake: handling a message failed")
        session.rollback()
        return _say(activity, FAILED)
    return _say(activity, result.text)


def _ask(session: Session, activity: dict, key: str, plan: Plan, has_files: bool) -> Optional[Handled]:
    card = choose_ticket_card(plan.choices, _text(activity.get("id")), plan.text)
    if len(plan.text) > MAX_PENDING_TEXT or len(json.dumps(card)) > MAX_CARD_BYTES:
        session.commit()
        return _say(activity, TOO_LONG_TO_ASK)
    # The claim names the most recent of the tickets asked about, so the hourly cap counts the question as theirs.
    if _claim(session, key, ASKED, plan.choices[0].id) is None:
        session.rollback()
        return None
    session.commit()
    if has_files:
        card["body"].append({"type": "TextBlock", "text": FILES_NOT_KEPT_WHILE_ASKING, "wrap": True, "isSubtle": True})

    def send() -> None:
        if not _reply(activity, card=card):
            _reply(activity, text=CARD_FAILED)

    return Handled(after=send)


# ---- The choice card -----------------------------------------------------------------------------------


def answer_card(session: Session, activity: dict, storage: Optional[Storage] = None) -> Optional[dict]:
    """The body to answer an invoke with when the person pressed a button on the choice card. The card names a request and
    carries the message; the press is the pressing person's own (`from.aadObjectId`), the request must be one of theirs, and
    the question is answered once (the claim row made when the card was sent)."""
    if _text(activity.get("name")) != ACTION_NAME:
        return None
    verb = _text(_sub(activity, "value", "action", "verb"))
    data = _sub(activity, "value", "action", "data")
    data = data if isinstance(data, dict) else {}
    oid = normal_oid(_person(activity)[0])
    asked = _text(data.get("m"))
    if oid is None or not asked or verb not in (VERB_PICK, VERB_NEW):
        return message_response(NO_LONGER_OPEN)
    person = person_for_object_id(session, oid)
    if person is None or not person.is_active or is_placeholder_address(person.email):
        return message_response(NOT_ACTIVE)
    now = datetime.now(timezone.utc)
    text = clean_text(data.get("t"))[:MAX_MESSAGE_LENGTH].strip()
    try:
        if verb == VERB_PICK:
            number = data.get("n")
            candidates = teams_tickets(session, person) + teams_tickets(session, person, resolved=True)
            ticket = next((t for t in candidates if isinstance(number, int) and not isinstance(number, bool) and t.number == number), None)
            if ticket is None:
                return message_response(NO_LONGER_OPEN)  # the card stays, so another button can be pressed
            plan = Plan(REPLY, ticket, text=text)
        else:
            if not text:
                return message_response(NEW_NEEDS_TEXT)
            plan = Plan(NEW, text=text)
        row = session.exec(
            select(TeamsIntake).where(TeamsIntake.activity_key == activity_key(oid, asked)).with_for_update()
        ).first()
        if row is None or row.outcome != ASKED:
            session.rollback()
            return message_response(ALREADY_DONE)  # not a new card: it would hide the answer the first press got
        if _over_limit(session, person, now):
            session.rollback()
            return message_response(SLOW_DOWN)
        result = act(session, person, row, plan, [], storage)
        session.commit()
    except Exception:
        log.exception("Teams intake: answering the choice card failed")
        session.rollback()
        return message_response(FAILED)
    return card_response(intake_result_card(result.text, result.ticket))
