""""Treat as empty" rules (#359): lines above a forwarded block that do not count as text.

When the forward rules (app/forwards.py) decide who the requester is, they ask whether the forwarder wrote anything above the
forwarded block. Mail programs add text of their own there: "Sent from my iPhone", "Get Outlook for iOS", and the forwarder's own
signature (Outlook puts it above the block on every forward). `judge_above` drops those lines first, so a forward with nothing
typed still counts as empty.

Two kinds of line are dropped, after blank lines:
- a line that matches an enabled admin rule (this module's table, kept like the Thanks rules and matched by the same code,
  app/thanks_rules.py: normalised text, `*` wildcards, no regular expressions);
- the forwarder's own signature: a line that is only their display name, job title or department (from the directory), only a
  bracketed picture name (the `[Vardags]` rule of app/mail_display.py), or that starts with a contact label (`Tel:`, `Web:`...).
  The labels are a constant here, not settings.

This is only for the forward decision. The stored message text is never changed, and the Thanks rules are not affected.
Nothing here commits.
"""

import re
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from app.mail_display import is_picture_line
from app.models import TreatAsEmptyRule
from app.thanks_rules import MATCH_TYPES, clean_pattern, normalise_text, rule_matches

MAX_LINE_SCAN = 300  # a line longer than this (normalised) is real writing, never a rule's target, so it is not compared

# The labels a signature's contact lines start with. A constant, not a setting (owner decision, 27 Nov 2026).
CONTACT_LABELS = ("Tel", "Phone", "Mobile", "Fax", "Web", "Email")
_CONTACT_LINE = re.compile(r"^\s*(?:" + "|".join(CONTACT_LABELS) + r")\s*:", re.IGNORECASE)


# ---- Judging the text above a forwarded block ---------------------------------------------------


@dataclass
class Judged:
    text: str  # what is left above the block ("" when nothing counts)
    ignored: int  # how many non-blank lines were treated as empty


def enabled_rules(session: Session) -> list[TreatAsEmptyRule]:
    return list(session.exec(select(TreatAsEmptyRule).where(TreatAsEmptyRule.enabled == True).order_by(TreatAsEmptyRule.id)).all())  # noqa: E712


def matches_any(rules: list[TreatAsEmptyRule], line: str) -> bool:
    normalised = normalise_text(line)
    if not normalised or len(normalised) > MAX_LINE_SCAN:
        return False
    return any(rule_matches(r.match_type, r.pattern, MAX_LINE_SCAN, normalised) for r in rules)


def judge_above(session: Session, above: str, own_lines: tuple[str, ...] = ()) -> Judged:
    """The text above a forwarded block that still counts after the rules, and how many lines were ignored. `own_lines` is the
    forwarder's display name, job title and department (blank ones are skipped); a line that is only one of them is signature."""
    own = {n for n in (normalise_text(x) for x in own_lines) if n}
    rules = enabled_rules(session) if above.strip() else []
    kept: list[str] = []
    ignored = 0
    for line in above.splitlines():
        if not line.strip():
            continue
        if (
            matches_any(rules, line)
            or normalise_text(line) in own
            or is_picture_line(line)
            or _CONTACT_LINE.match(line)
        ):
            ignored += 1
            continue
        kept.append(line.strip())
    return Judged(text="\n".join(kept), ignored=ignored)


# ---- Rules (admin) ----------------------------------------------------------------------------


def all_rules(session: Session) -> list[TreatAsEmptyRule]:
    return list(session.exec(select(TreatAsEmptyRule).order_by(TreatAsEmptyRule.id)).all())


def add_rule(session: Session, match_type: str, pattern: str, created_by: str) -> TreatAsEmptyRule:
    match_type, pattern = clean_pattern(match_type, pattern)
    row = TreatAsEmptyRule(match_type=match_type, pattern=pattern, created_by=created_by)
    session.add(row)
    session.flush()
    return row


def update_rule(session: Session, rule_id: int, match_type: str, pattern: str) -> Optional[TreatAsEmptyRule]:
    """Changes a rule, or returns None if it is gone."""
    row = session.get(TreatAsEmptyRule, rule_id)
    if row is None:
        return None
    row.match_type, row.pattern = clean_pattern(match_type, pattern)
    session.add(row)
    return row


def set_rule_enabled(session: Session, rule_id: int, enabled: bool) -> Optional[TreatAsEmptyRule]:
    row = session.get(TreatAsEmptyRule, rule_id)
    if row is None:
        return None
    row.enabled = enabled
    session.add(row)
    return row


def delete_rule(session: Session, rule_id: int) -> Optional[TreatAsEmptyRule]:
    """Removes a rule and returns it (for the audit entry), or None if it is gone."""
    row = session.get(TreatAsEmptyRule, rule_id)
    if row is None:
        return None
    session.delete(row)
    return row


def describe(rule: TreatAsEmptyRule) -> str:
    return f"{MATCH_TYPES.get(rule.match_type, rule.match_type)} \"{rule.pattern}\""
