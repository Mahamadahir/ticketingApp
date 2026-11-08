"""Subject access export (issue #175): a ZIP of everything held on one person.

The person is found by `app/person_data.find_person` (every address they have, every account that is theirs).
What goes in, and what does not:

- Tickets where the person is the requester, a follower or on copy, with the **requester-visible** messages
  (`messages.requester_visible_messages`) and nothing else of the thread. Internal notes on those tickets are in
  a separate file (`internal_notes.json`) so the data protection lead decides what to disclose.
- Form answers and the feedback rating and comment only on tickets the person raised (they are the requester's
  own words); approval votes, notifications, preferences, time zone, sign-in activity and sessions, audit entries
  they made or that are about their account, the account record, linked identities.
- Attachments as files: the ones the person sent or uploaded themselves, up to a size setting; others in a
  visible thread are listed with names and sizes, never added as files.
- Staff work (tickets worked, messages written, files uploaded) by reference only: numbers, dates, no text.
- Never: password or secret hashes, session ids, another person's account, a ticket the person is not on.

Nothing is kept: the ZIP is built in an anonymous temporary file that the caller streams and closes.
"""

import csv
import io
import json
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import IO, Any, Optional

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.attachments import clean_filename
from app.audit import log_action
from app.models import (
    ActivityLog,
    ApprovalGroup,
    ApprovalGroupMember,
    ApprovalVote,
    Attachment,
    AuditLog,
    InboundMail,
    LinkedIdentity,
    Message,
    Notification,
    NotificationPreference,
    OutboundEmail,
    RequesterTimeZone,
    Team,
    TeamMembership,
    Ticket,
    TicketApproval,
    TicketEvent,
    TicketFeedback,
    TicketFollower,
    User,
    UserPermission,
    UserSession,
)
from app.messages import requester_visible_answers, requester_visible_messages
from app.models import MessageDirection
from app.person_data import Person, email_matches, find_person, list_matches, mentions
from app.settings_store import SUBJECT_ACCESS_MAX_MB, count_setting
from app.storage import Storage
from app.templating import templates

AUDIT_ENTITY = "subject_access"
AUDIT_ACTION = "subject_access_export"

REQUESTER, FOLLOWER, COPIED = "requester", "follower", "copied"

NOT_INCLUDED = [
    "Password, one-time-code and session secrets (hashes and keys are never exported).",
    "Another person's account, tickets or messages. On a ticket the person shares with someone, only that ticket's "
    "requester-visible thread is included.",
    "The text of internal notes is in internal_notes.json, apart from the rest, for the data protection lead to decide on.",
    "Staff work (tickets worked, messages written, files uploaded) is listed by reference only.",
    "The bodies of emails sent from the shared mailbox (their subjects and recipients are listed).",
]


@dataclass
class Export:
    file: IO[bytes]  # an anonymous temporary file, positioned at the start; closing it deletes it
    size: int
    summary: dict


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    return value


def _json(data: Any) -> bytes:
    return json.dumps(data, indent=2, ensure_ascii=False, default=_iso).encode("utf-8")


def _csv(rows: list[dict], columns: list[str]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        # Text starting with a formula character gets an apostrophe, as in the report CSV.
        writer.writerow([_cell(row.get(c)) for c in columns])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


def _cell(value: Any) -> str:
    text = "" if value is None else str(_iso(value))
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _safe_part(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", clean_filename(name))[:120] or "attachment"


def _in(column, values):
    values = list(values)
    return column.in_(values) if values else column.is_(None) & column.is_not(None)


# ---- Sections -----------------------------------------------------------------------------------------------


def _accounts(session: Session, person: Person) -> list[dict]:
    rows = []
    for user in session.exec(select(User).where(_in(User.id, person.user_ids)).order_by(User.id)).all():
        rows.append(
            {
                "email": user.email, "display_name": user.display_name, "role": user.role.value,
                "is_active": user.is_active, "created_at": user.created_at, "time_zone": user.time_zone,
                "time_zone_source": user.time_zone_source, "teams_delay_minutes": user.teams_delay_minutes,
                "entra_object_id": user.entra_object_id, "is_break_glass": user.is_break_glass,
                "out_of_office": user.out_of_office, "locked_until": user.locked_until,
                "has_emergency_password": user.emergency_password_hash is not None,
                "has_password": user.password_hash is not None,
                "team_memberships": [
                    {"team": team, "level": level.value}
                    for team, level in session.exec(
                        select(Team.name, TeamMembership.level)
                        .join(TeamMembership, TeamMembership.team_id == Team.id)
                        .where(TeamMembership.user_id == user.id)
                    ).all()
                ],
                "permissions": session.exec(select(UserPermission.permission).where(UserPermission.user_id == user.id)).all(),
                "approval_groups": session.exec(
                    select(ApprovalGroup.name)
                    .join(ApprovalGroupMember, ApprovalGroupMember.group_id == ApprovalGroup.id)
                    .where(ApprovalGroupMember.user_id == user.id)
                ).all(),
            }
        )
    return rows


def _linked(session: Session, person: Person) -> list[dict]:
    rows = session.exec(
        select(LinkedIdentity)
        .where(
            or_(
                _in(LinkedIdentity.main_user_id, person.user_ids),
                _in(LinkedIdentity.secondary_user_id, person.user_ids),
                func.lower(LinkedIdentity.email).in_(person.addresses),
            )
        )
        .order_by(LinkedIdentity.id)
    ).all()
    return [
        {
            "address": r.email, "status": r.status, "method": r.method, "entra_object_id": r.entra_object_id,
            "created_at": r.created_at, "activated_at": r.activated_at, "unlinked_at": r.unlinked_at,
        }
        for r in rows
    ]


def _ticket_roles(session: Session, person: Person) -> tuple[dict[int, set[str]], set[int]]:
    """ticket id -> the ways the person is on it; and the tickets they follow only as staff (agent reference)."""
    roles: dict[int, set[str]] = {}
    for ticket_id in session.exec(select(Ticket.id).where(email_matches(Ticket.requester_email, person))).all():
        roles.setdefault(ticket_id, set()).add(REQUESTER)
    staff_followed: set[int] = set()
    for ticket_id, internal in session.exec(
        select(TicketFollower.ticket_id, TicketFollower.is_internal).where(email_matches(TicketFollower.email, person))
    ).all():
        if internal:
            staff_followed.add(ticket_id)
        else:
            roles.setdefault(ticket_id, set()).add(FOLLOWER)
    for ticket_id, cc in session.exec(
        select(Message.ticket_id, Message.cc).where(
            list_matches(Message.cc, person), Message.direction != MessageDirection.INTERNAL
        )
    ).all():
        if mentions(cc, person):
            roles.setdefault(ticket_id, set()).add(COPIED)
    staff_followed -= set(roles)
    return roles, staff_followed


def _is_own_message(message: Message, person: Person) -> bool:
    if message.author_user_id is not None and message.author_user_id in person.user_ids:
        return True
    sender = (message.sender or "").strip().lower()
    return mentions(sender, person) or (person.main is not None and sender == person.main.display_name.strip().lower())


def _message_row(message: Message, own: bool) -> dict:
    return {
        "id": message.id, "created_at": message.created_at, "direction": message.direction.value,
        "sender": message.sender, "cc": message.cc, "body": message.body, "written_by_the_person": own,
    }


def _tickets(session: Session, person: Person, roles: dict[int, set[str]], storage: Storage, budget: int):
    """Returns (ticket sections, internal-note sections, files to add, attachments listed but not added)."""
    sections, internal_sections, files, listed = [], [], [], []
    spent = 0
    tickets = session.exec(select(Ticket).where(_in(Ticket.id, roles)).order_by(Ticket.number)).all()
    for ticket in tickets:
        visible = requester_visible_messages(session, ticket)
        visible_ids = {m.id for m in visible}
        own_ids = {m.id for m in visible if _is_own_message(m, person)}
        is_requester = REQUESTER in roles[ticket.id]
        section = {
            "reference": ticket.reference, "number": ticket.number, "subject": ticket.subject,
            "status": ticket.status.value, "type": ticket.type.value, "created_at": ticket.created_at,
            "resolved_at": ticket.resolved_at, "closed_at": ticket.closed_at,
            "roles": sorted(roles[ticket.id]),
            "messages": [_message_row(m, m.id in own_ids) for m in visible],
        }
        if is_requester:
            section["form_answers"] = [
                {"question": a["label"], "answer": a["display"]} for a in requester_visible_answers(session, ticket)
            ]
            section["feedback"] = [
                {
                    "rating": f.rating, "comment": f.comment, "given_at": f.created_at,
                    "about": f"staff member user-{f.resolved_by_id}" if f.resolved_by_id else "automatic resolve",
                }
                for f in session.exec(select(TicketFeedback).where(TicketFeedback.ticket_id == ticket.id)).all()
            ]
        sections.append(section)

        notes = session.exec(
            select(Message).where(Message.ticket_id == ticket.id, ~Message.id.in_(visible_ids or {0})).order_by(Message.created_at, Message.id)
        ).all()
        note_ids = {n.id for n in notes}
        if notes:
            internal_sections.append(
                {"reference": ticket.reference, "notes": [_message_row(n, False) | {"visibility": n.visibility.value} for n in notes]}
            )

        for attachment in session.exec(select(Attachment).where(Attachment.ticket_id == ticket.id).order_by(Attachment.id)).all():
            row = {
                "attachment_id": attachment.id, "ticket": ticket.reference, "filename": attachment.filename,
                "size": attachment.size, "content_type": attachment.content_type,
            }
            if attachment.message_id in note_ids:
                listed.append(row | {"reason": "attached to an internal note"})
                continue
            if attachment.message_id is not None and attachment.message_id not in visible_ids:
                continue  # another ticket's message
            mine = attachment.message_id in own_ids or (
                attachment.uploaded_by_id is not None and attachment.uploaded_by_id in person.user_ids
            )
            if not mine:
                if attachment.message_id is not None:
                    listed.append(row | {"reason": "sent by someone else in this thread; not added as a file"})
                continue
            if spent + attachment.size > budget:
                listed.append(row | {"reason": "over the size limit; not added as a file"})
                continue
            if not storage.exists(attachment.blob_key):
                listed.append(row | {"reason": "the file is missing from storage"})
                continue
            spent += attachment.size
            path = f"attachments/{ticket.reference}/{attachment.id}-{_safe_part(attachment.filename)}"
            files.append((path, attachment.blob_key, attachment.size))
    return sections, internal_sections, files, listed


def _staff_references(session: Session, person: Person) -> dict:
    ids = list(person.user_ids)
    if not ids:
        return {}
    worked = []
    for ticket in session.exec(
        select(Ticket).where(
            or_(
                _in(Ticket.assigned_agent_id, ids), _in(Ticket.resolved_by_id, ids), _in(Ticket.triaged_by_id, ids),
                _in(Ticket.escalated_by_id, ids), _in(Ticket.created_by_id, ids),
            )
        ).order_by(Ticket.number)
    ).all():
        did = [
            name for name, value in (
                ("assigned", ticket.assigned_agent_id), ("resolved", ticket.resolved_by_id),
                ("triaged", ticket.triaged_by_id), ("escalated", ticket.escalated_by_id), ("created", ticket.created_by_id),
            ) if value in person.user_ids
        ]
        worked.append({"ticket": ticket.reference, "as": did})
    messages = [
        {"message_id": mid, "ticket": f"#{number}", "direction": direction.value, "written_at": at}
        for mid, number, direction, at in session.exec(
            select(Message.id, Ticket.number, Message.direction, Message.created_at)
            .join(Ticket, Ticket.id == Message.ticket_id)
            .where(_in(Message.author_user_id, ids))
            .order_by(Message.id)
        ).all()
    ]
    uploads = [
        {"attachment_id": aid, "ticket": f"#{number}", "uploaded_at": at}
        for aid, number, at in session.exec(
            select(Attachment.id, Ticket.number, Attachment.created_at)
            .join(Ticket, Ticket.id == Attachment.ticket_id)
            .where(_in(Attachment.uploaded_by_id, ids))
            .order_by(Attachment.id)
        ).all()
    ]
    if not (worked or messages or uploads):
        return {}
    return {"tickets_worked": worked, "messages_written": messages, "files_uploaded": uploads}


def _approvals(session: Session, person: Person) -> list[dict]:
    return [
        {
            "ticket": f"#{number}", "decision": vote.decision, "comment": vote.comment, "asked_at": vote.created_at,
            "decided_at": vote.decided_at, "approval_outcome": approval.status,
        }
        for vote, approval, number in session.exec(
            select(ApprovalVote, TicketApproval, Ticket.number)
            .join(TicketApproval, TicketApproval.id == ApprovalVote.approval_id)
            .join(Ticket, Ticket.id == TicketApproval.ticket_id)
            .where(_in(ApprovalVote.voter_id, person.user_ids))
            .order_by(ApprovalVote.id)
        ).all()
    ]


def _notifications(session: Session, person: Person) -> list[dict]:
    return [
        {"kind": n.kind, "ticket": f"#{number}" if number else None, "text": n.text, "created_at": n.created_at, "read_at": n.read_at}
        for n, number in session.exec(
            select(Notification, Ticket.number)
            .join(Ticket, Ticket.id == Notification.ticket_id, isouter=True)
            .where(_in(Notification.user_id, person.user_ids))
            .order_by(Notification.id)
        ).all()
    ]


def _preferences(session: Session, person: Person) -> dict:
    return {
        "notification_preferences": [
            {"event": p.kind, "channel": p.channel, "enabled": p.enabled}
            for p in session.exec(
                select(NotificationPreference).where(_in(NotificationPreference.user_id, person.user_ids)).order_by(NotificationPreference.id)
            ).all()
        ],
        "time_zones_read_from_mailbox": [
            {"address": z.email, "time_zone": getattr(z, "time_zone", None)}
            for z in session.exec(select(RequesterTimeZone).where(email_matches(RequesterTimeZone.email, person))).all()
        ],
    }


def _sessions(session: Session, person: Person) -> list[dict]:
    return [
        {
            "started_at": s.created_at, "last_seen_at": s.last_seen_at, "expires_at": s.expires_at, "revoked_at": s.revoked_at,
            "ip": s.ip, "user_agent": s.user_agent, "break_glass": s.break_glass, "emergency": s.emergency,
        }
        for s in session.exec(select(UserSession).where(_in(UserSession.user_id, person.user_ids)).order_by(UserSession.created_at)).all()
    ]


def _activity(session: Session, person: Person) -> list[dict]:
    rows = session.exec(
        select(ActivityLog)
        .where(or_(_in(ActivityLog.actor_user_id, person.user_ids), func.lower(ActivityLog.actor_email).in_(person.addresses)))
        .order_by(ActivityLog.id)
    ).all()
    return [
        {
            "time": r.created_at, "event": r.event_type, "method": r.method, "path": r.path, "status": r.status_code,
            "ip": r.ip, "user_agent": r.user_agent, "detail": r.detail,
        }
        for r in rows
    ]


def _audit(session: Session, person: Person) -> list[dict]:
    rows = session.exec(
        select(AuditLog)
        .where(
            or_(
                func.lower(AuditLog.actor_email).in_(person.addresses),
                (AuditLog.entity_type == "user") & _in(AuditLog.entity_id, person.user_ids),
            )
        )
        .order_by(AuditLog.id)
    ).all()
    return [
        {
            "time": r.created_at, "entity": f"{r.entity_type} {r.entity_id}", "action": r.action, "detail": r.detail,
            "done_by": r.actor_email if r.actor_email.strip().lower() in person.addresses else "a staff member",
            "about_the_person": r.entity_type == "user" and r.entity_id in person.user_ids,
        }
        for r in rows
    ]


def _ticket_events(session: Session, person: Person, in_scope: set[int]) -> list[dict]:
    """Events the person caused, or that name one of their addresses (a follower added). Another person's
    event on someone else's ticket is not included."""
    rows = session.exec(
        select(TicketEvent, Ticket.number)
        .join(Ticket, Ticket.id == TicketEvent.ticket_id)
        .where(
            or_(
                func.lower(TicketEvent.actor_email).in_(person.addresses),
                func.lower(TicketEvent.from_value).in_(person.addresses),
                func.lower(TicketEvent.to_value).in_(person.addresses),
            )
        )
        .order_by(TicketEvent.id)
    ).all()
    return [
        {"time": e.created_at, "ticket": f"#{number}", "event": e.event_type, "from": e.from_value, "to": e.to_value}
        for e, number in rows
        if e.ticket_id in in_scope or (e.actor_email or "").strip().lower() in person.addresses
    ]


def _mail(session: Session, person: Person, in_scope: set[int]) -> dict:
    sent_to = []
    for mail, number in session.exec(
        select(OutboundEmail, Ticket.number)
        .join(Ticket, Ticket.id == OutboundEmail.ticket_id, isouter=True)
        .where(email_matches(OutboundEmail.to_address, person) | list_matches(OutboundEmail.cc, person))
        .order_by(OutboundEmail.id)
    ).all():
        if not ((mail.to_address or "").strip().lower() in person.addresses or mentions(mail.cc, person)):
            continue
        # The subject of a mail about a ticket the person is not on is that ticket's, so it is left out.
        shown = mail.ticket_id is None or mail.ticket_id in in_scope
        sent_to.append(
            {
                "kind": mail.kind, "ticket": f"#{number}" if number else None, "to": mail.to_address, "cc": mail.cc if shown else None,
                "subject": mail.subject if shown else None, "queued_at": mail.created_at, "sent_at": mail.sent_at,
            }
        )
    received = [
        {"received_at": m.created_at, "outcome": m.outcome, "ticket": f"#{number}" if number else None}
        for m, number in session.exec(
            select(InboundMail, Ticket.number)
            .join(Ticket, Ticket.id == InboundMail.ticket_id, isouter=True)
            .where(list_matches(InboundMail.sender, person))
            .order_by(InboundMail.id)
        ).all()
        if mentions(m.sender, person)
    ]
    return {"emails_sent_to_the_person": sent_to, "mail_received_from_the_person": received}


# ---- The export ---------------------------------------------------------------------------------------------


def build_export(session: Session, storage: Storage, address: str, requested_by: User, now: Optional[datetime] = None) -> Export:
    """Builds the ZIP and records the audit entry (the caller commits). Does not keep the file anywhere."""
    now = now or datetime.now(timezone.utc)
    person = find_person(session, address)
    budget = count_setting(session, SUBJECT_ACCESS_MAX_MB) * 1024 * 1024

    roles, staff_followed = _ticket_roles(session, person)
    sections, internal_sections, files, listed = _tickets(session, person, roles, storage, budget)
    in_scope = set(roles)
    staff = _staff_references(session, person)
    if staff_followed:
        staff["tickets_followed_as_staff"] = [
            f"#{n}" for n in session.exec(select(Ticket.number).where(_in(Ticket.id, staff_followed)).order_by(Ticket.number)).all()
        ]
    accounts, linked = _accounts(session, person), _linked(session, person)
    activity, audit = _activity(session, person), _audit(session, person)
    data = {
        "account.json": {"accounts": accounts, "linked_identities": linked, "sessions": _sessions(session, person)},
        "tickets.json": sections,
        "internal_notes.json": {
            "warning": "Internal notes were written by staff for staff. They are NOT part of what the requester may see. "
            "The data protection lead decides what, if anything, to disclose.",
            "tickets": internal_sections,
        },
        "approvals.json": _approvals(session, person),
        "notifications.json": _notifications(session, person),
        "preferences.json": _preferences(session, person),
        "mail.json": _mail(session, person, in_scope),
        "ticket_events.json": _ticket_events(session, person, in_scope),
        "staff_references.json": staff,
        "attachments_not_added.json": listed,
    }
    summary = {
        "generated_at": now, "requested_by": requested_by.email, "address_asked": person.address,
        "addresses": sorted(person.addresses), "has_account": person.main is not None,
        "counts": {
            "tickets": len(sections), "tickets_as_requester": sum(REQUESTER in roles[t] for t in roles),
            "visible_messages": sum(len(s["messages"]) for s in sections),
            "internal_notes": sum(len(s["notes"]) for s in internal_sections),
            "attachment_files": len(files), "attachments_listed_only": len(listed),
            "approval_votes": len(data["approvals.json"]), "notifications": len(data["notifications.json"]),
            "activity_rows": len(activity), "audit_entries": len(audit),
        },
        "attachment_bytes_added": sum(size for _, _, size in files), "attachment_limit_bytes": budget,
        "files": sorted([*data, "activity.csv", "audit.csv", "summary.html", *(p for p, _, _ in files)]),
        "not_included": NOT_INCLUDED,
    }
    data["summary.json"] = summary

    handle = tempfile.TemporaryFile()
    try:
        with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in data.items():
                archive.writestr(name, _json(content))
            archive.writestr("activity.csv", _csv(activity, ["time", "event", "method", "path", "status", "ip", "user_agent", "detail"]))
            archive.writestr("audit.csv", _csv(audit, ["time", "entity", "action", "detail", "done_by", "about_the_person"]))
            archive.writestr("summary.html", templates.get_template("subject_access_summary.html").render(summary=summary, tickets=sections, internal=internal_sections, listed=listed, accounts=accounts, linked=linked))
            for path, key, _ in files:
                try:
                    content = storage.read(key)
                except FileNotFoundError:
                    continue  # gone since it was checked a moment ago; the summary's count is then one high
                archive.writestr(path, content)
        size = handle.tell()
        handle.seek(0)
    except BaseException:
        handle.close()
        raise

    log_action(
        session, requested_by.email, AUDIT_ENTITY, person.main.id if person.main is not None else 0, AUDIT_ACTION,
        f"for {person.address}: {len(sections)} tickets, {len(files)} files, {size} bytes",
    )
    return Export(handle, size, summary)
