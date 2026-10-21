"""Admin-managed wording for the emails the app sends by itself (phase 4T).

The registry below is the one place a template exists: its key, what it is for, the
placeholders it may use and its default subject and body. A later email adds one entry to
REGISTRY and calls `render_template` where it builds the mail; the admin page, the preview,
the validation and the reset all follow from the entry.

Only the rows an admin has changed are stored (`EmailTemplate`). A template with no row uses
the default in this file, so a better default shipped later reaches everyone who never edited
it, and "reset to default" is deleting the row.

Rendering is plain placeholder substitution, one pass: `{name}` is replaced by its value and
nothing else happens. No Jinja, no formatting mini-language, and a value is never scanned for
more placeholders, so a requester's words containing `{link}` stay as written. A placeholder
that is unknown for the template, or missing where the template needs it, cannot be saved.

The signed approval link is never stored: the body holds only `{link}`, and the link is made
and put in at send time (app/mail.py). A pasted address of the approval page is refused.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session

from app.models import EmailTemplate, Ticket

log = logging.getLogger("app.email_templates")

MAX_SUBJECT_LENGTH = 200
MAX_BODY_LENGTH = 10000
_TOKEN = re.compile(r"\{([^{}]*)\}")

# What stands in for the link in the copy of an approval email kept on the outbox row.
LINK_LEFT_OUT = "[answer link left out]"
# The framing around the requester's own words in an approval email. It is code, not template
# text: it says the words are theirs and not IT's, beside a genuine link.
REQUEST_BLOCK = "What they wrote (the requester's own words, not from IT):\n---\n{words}\n---"


class TemplateError(ValueError):
    """A reason a template cannot be saved, worded for the admin."""


@dataclass(frozen=True)
class TemplateDef:
    key: str
    label: str
    description: str
    body_default: str
    body_placeholders: dict[str, str]  # name -> what it stands for
    sample: dict[str, str]  # values for the preview
    # None: the subject is not the template's. The email goes under the ticket's own subject
    # ("[INC-1042] Printer jam"), whose reference is what threads replies back (messages.mail_subject).
    subject_default: Optional[str] = None
    subject_placeholders: dict[str, str] = field(default_factory=dict)
    required: tuple[str, ...] = ()  # body placeholders that must appear
    subject_required: tuple[str, ...] = ()  # subject placeholders that must appear

    @property
    def subject_editable(self) -> bool:
        return self.subject_default is not None


_COMMON = {
    "reference": "the ticket reference, such as INC-1042",
    "ticket_number": "the ticket number, such as 1042",
    "subject": "the ticket's subject",
}
_COMMON_SAMPLE = {"reference": "INC-1042", "ticket_number": "1042", "subject": "Printer on floor 2 is jammed"}

REGISTRY: dict[str, TemplateDef] = {
    d.key: d
    for d in (
        TemplateDef(
            key="raised_for",
            label="Raised for you",
            description="Sent to a person when an agent raises a ticket on their behalf.",
            body_default=(
                "{raised_by} has raised a ticket for you with IT Support.\n\n"
                "Reference: {reference}\nSubject: {subject}\n\n{request_text}\n\n"
                "Reply to this email to add anything else. Please keep the reference in the subject."
            ),
            body_placeholders={
                **_COMMON,
                "raised_by": "the name of the agent who raised it",
                "request_text": "what was written on the ticket (left out if nothing was)",
            },
            sample={**_COMMON_SAMPLE, "raised_by": "Alex Agent", "request_text": "The printer shows a paper jam error."},
        ),
        TemplateDef(
            key="acknowledgement",
            label="Acknowledgement",
            description=(
                "Sent once to the person who emails IT, when their email opens a ticket. It is only a receipt: "
                "keep it free of ticket content, a queue position or a promised time (an emailed ticket has no "
                "priority until it is triaged). It goes out under the ticket's own subject."
            ),
            body_default="Your issue has been logged as {reference} with IT. You will be updated once the ticket is taken.",
            body_placeholders={"reference": _COMMON["reference"], "ticket_number": _COMMON["ticket_number"]},
            sample={"reference": "INC-1042", "ticket_number": "1042"},
        ),
        TemplateDef(
            key="follower_update",
            label="Follower update",
            description=(
                "Sent to each follower (and the requester, when a follower wrote it) who was not already sent an update "
                "that someone emailed or commented on a ticket. It goes out under the ticket's own subject, so a reply "
                "still finds the ticket."
            ),
            body_default=(
                "{sender} has added an update to {reference} ({subject}):\n\n{message_text}\n\n"
                "You are receiving this because you follow this ticket. Reply to this email to add to it."
            ),
            body_placeholders={
                **_COMMON,
                "sender": "who wrote the update",
                "message_text": "what they wrote",
            },
            required=("message_text",),
            sample={**_COMMON_SAMPLE, "sender": "sam@example.com", "message_text": "It is still jamming after the restart."},
        ),
        TemplateDef(
            key="resolution",
            label="Resolution email",
            description=(
                "Sent to the requester, copying the followers, when an agent resolves a ticket. It goes out under the "
                "ticket's own subject, so a reply still finds the ticket."
            ),
            body_default=(
                "Your ticket {reference} has been resolved.\n\n"
                "What was done:\n{resolution_notes}\n\n"
                "If this has not fixed the problem, reply to this email and tell us."
            ),
            body_placeholders={**_COMMON, "resolution_notes": "what the agent wrote when resolving the ticket"},
            required=("resolution_notes",),
            sample={**_COMMON_SAMPLE, "resolution_notes": "Cleared the paper jam and replaced the roller."},
        ),
        TemplateDef(
            key="auto_resolve_note",
            label="Automatic resolution note",
            description=(
                "The resolution notes written on a ticket that is resolved automatically (no agent resolved it). "
                "They then go into the resolution email like an agent's notes."
            ),
            body_default=(
                "No reply was received after {days} days, so this ticket has been resolved automatically. "
                "If you still need help, reply to this email."
            ),
            body_placeholders={**_COMMON, "days": "how many days we waited for a reply"},
            sample={**_COMMON_SAMPLE, "days": "3"},
        ),
        TemplateDef(
            key="approval_request",
            label="Approval request",
            description="Sent to each approver of a service request. The link is made when the email is sent.",
            subject_default="[{reference}] Approval needed: {subject}",
            subject_placeholders=_COMMON,
            body_default=(
                "{requester_email} has asked for something that needs your approval.\n\n"
                "Reference: {reference}\nSubject: {subject}\n\n{request_block}\n\n{link}\n\n"
                "Open the link to approve or decline. Opening it decides nothing.\n"
                "The link works for {link_days} days."
            ),
            body_placeholders={
                **_COMMON,
                "requester_email": "who asked",
                "request_block": "the requester's own words, set apart and marked as theirs (left out if none)",
                "link": "the personal link to answer from, made when the email is sent",
                "link_days": "how many days the link works",
            },
            required=("link",),
            subject_required=("reference",),  # the [INC-n] that lets a reply find its ticket
            sample={
                **_COMMON_SAMPLE,
                "requester_email": "sam@example.com",
                "request_block": REQUEST_BLOCK.format(words="Please install the design software on my laptop."),
                "link": "https://tickets.example.org/approvals/(a personal link, signed when sent)",
                "link_days": "14",
            },
        ),
        TemplateDef(
            key="approval_approved",
            label="Notice: request approved",
            description="Told to the requester when their request has been approved.",
            body_default="Your request has been approved. IT will pick it up.",
            body_placeholders=dict(_COMMON),
            sample=dict(_COMMON_SAMPLE),
        ),
        TemplateDef(
            key="approval_declined",
            label="Notice: request declined",
            description="Told to the requester when their request was declined and closed.",
            body_default="Your request was not approved, so it has been closed.{reason_text}",
            body_placeholders={**_COMMON, "reason_text": "the approvers' reasons, worded as ' Reason: ...' (empty if none)"},
            sample={**_COMMON_SAMPLE, "reason_text": " Reason: Not in this year's budget."},
        ),
        TemplateDef(
            key="approval_waived",
            label="Notice: approval waived",
            description="Told to the requester when a super admin lets a request through without the remaining approvals.",
            body_default="Your request is going ahead: IT has let it through without waiting for further approval.",
            body_placeholders=dict(_COMMON),
            sample=dict(_COMMON_SAMPLE),
        ),
    )
}


def definition(key: str) -> TemplateDef:
    try:
        return REGISTRY[key]
    except KeyError:
        raise TemplateError("There is no email template with that key")


def _check_text(text: str, allowed: dict[str, str], what: str) -> None:
    unknown = sorted({m.group(1) for m in _TOKEN.finditer(text) if m.group(1) not in allowed})
    if unknown:
        listed = ", ".join("{" + n + "}" for n in unknown)
        raise TemplateError(f"The {what} uses {listed}, which this email cannot fill in")


def _lines(text: Optional[str]) -> str:
    """Browsers post text areas with CRLF line ends; the defaults and the stored text use LF."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def ticket_values(ticket: Ticket) -> dict[str, str]:
    """The values of the placeholders every template shares (_COMMON). Callers add their own."""
    return {"reference": ticket.reference, "ticket_number": str(ticket.number), "subject": ticket.subject}


def validate(defn: TemplateDef, subject: Optional[str], body: str) -> tuple[Optional[str], str]:
    """The cleaned subject (None when the template has none of its own) and body, or TemplateError."""
    body = _lines(body).strip()
    if not body:
        raise TemplateError("The email needs some text")
    if len(body) > MAX_BODY_LENGTH:
        raise TemplateError(f"The text can be at most {MAX_BODY_LENGTH} characters")
    _check_text(body, defn.body_placeholders, "text")
    if "/approvals/" in body.lower():
        raise TemplateError("Approval links cannot be written into a template; use {link}, which is made when the email is sent")
    for name in defn.required:
        if "{" + name + "}" not in body:
            raise TemplateError("The text must include {" + name + "}")
    if not defn.subject_editable:
        return None, body
    subject = " ".join((subject or "").split())
    if not subject:
        raise TemplateError("The email needs a subject")
    if len(subject) > MAX_SUBJECT_LENGTH:
        raise TemplateError(f"The subject can be at most {MAX_SUBJECT_LENGTH} characters")
    _check_text(subject, defn.subject_placeholders, "subject")
    for name in defn.subject_required:
        if "{" + name + "}" not in subject:
            raise TemplateError("The subject must include {" + name + "}")
    return subject, body


def render_text(text: str, values: dict[str, str]) -> str:
    """Substitutes `{name}` once, leaving anything unknown as written. A placeholder alone on its
    line whose value is empty takes the line, and the blank line before it, with it."""
    for name, value in values.items():
        if value == "":
            text = re.sub(r"(?:\n[ \t]*)?\n[ \t]*\{" + re.escape(name) + r"\}[ \t]*(?=\n|$)", "", text)
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), text).strip()


@dataclass
class Rendered:
    subject: Optional[str]  # None: use the ticket's own subject line
    body: str


def current_text(session: Session, key: str) -> tuple[Optional[str], str]:
    """The subject and body now in force: the admin's if saved (and still valid), else the default."""
    defn = definition(key)
    row = session.get(EmailTemplate, key)
    if row is not None:
        try:
            return validate(defn, row.subject, row.body)
        except TemplateError as error:
            # A placeholder removed from the registry since the admin saved. Better the default
            # than no email.
            log.warning("Email template %s no longer validates (%s); using the default", key, error)
    return defn.subject_default, defn.body_default


def render_template(session: Session, key: str, values: dict[str, str]) -> Rendered:
    subject, body = current_text(session, key)
    return Rendered(
        subject=render_text(subject, values) if subject is not None else None,
        body=render_text(body, values),
    )


def render_preview(defn: TemplateDef, subject: Optional[str], body: str) -> Rendered:
    """What the text would look like with sample data. Raises TemplateError if it is not valid."""
    subject, body = validate(defn, subject, body)
    return Rendered(
        subject=render_text(subject, defn.sample) if subject is not None else None,
        body=render_text(body, defn.sample),
    )


def is_customised(session: Session, key: str) -> bool:
    return session.get(EmailTemplate, key) is not None


def save_template(session: Session, key: str, subject: Optional[str], body: str, editor_email: str) -> Optional[EmailTemplate]:
    """Saves the admin's wording. Returns None, storing nothing, if it equals the default (so the
    template keeps following the default). Does not commit."""
    defn = definition(key)
    subject, body = validate(defn, subject, body)
    row = session.get(EmailTemplate, key)
    if (subject, body) == (defn.subject_default, defn.body_default):
        if row is not None:
            session.delete(row)
        return None
    if row is None:
        row = EmailTemplate(key=key, subject="", body="", updated_by=editor_email)
    row.subject, row.body, row.updated_by = subject or "", body, editor_email
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    return row


def reset_template(session: Session, key: str) -> bool:
    """Back to the default. True if there was anything to reset. Does not commit."""
    definition(key)
    row = session.get(EmailTemplate, key)
    if row is None:
        return False
    session.delete(row)
    return True
