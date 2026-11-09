"""The Adaptive Cards the Teams bot sends to a person in their own chat (phase 8B, 8C), built in this one module.

A card here is the bare card (what `app.bot_connector.send_card` takes), not the Workflows payload the channel posts
wrap it in. Every word that came from a requester (a ticket subject, a form answer) goes through `app.teams.plain`, so
it can never become a link or markup. The person is already in the chat, so there is no @mention.

One card per notification kind: `CARD_FOR_KIND` maps a kind to its builder and `notification_card` falls back to the
plain "what happened, who did it, Open ticket" card. The "waiting for your approval" notification has its own card
(8C: Approve and Decline buttons, `Action.Execute`), and `decided_card` is what replaces it once it is answered; the
button press itself is answered by app/teams_approvals.py. Nothing else about delivery changes, because routing, the
unread delay, the person's settings, the access check and the backoff all happen before a card is built
(app/teams.py).
"""

from typing import Callable, Optional

from sqlmodel import Session

from app.approval_groups import is_member
from app.identity import is_requester
from app.approvals import (
    APPROVED, DECLINED, MAX_COMMENT_LENGTH, NOT_NEEDED, PENDING, SUPERSEDED, WAIVED, WITHDRAWN,
    describe_rule, vote_for,
)
from app.messages import requester_visible_answers
from app.models import ApprovalVote, Notification, RequestType, Ticket, TicketApproval, TicketStatus, User
from app.notifications import APPROVAL_WAITING, notification_text
from app.teams import KIND_LABELS, Unsendable, plain, portal_link, ticket_link

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.4"

VERB_APPROVE = "approval.approve"
VERB_DECLINE = "approval.decline"
MAX_ANSWERS = 8  # a long form would make a card nobody reads; the rest are on the portal page

OUTCOME_LABELS = {
    PENDING: "Still waiting for the other approvers",
    APPROVED: "Approved",
    DECLINED: "Declined",
    WAIVED: "Let through by an administrator",
    WITHDRAWN: "Withdrawn by the person who asked",
    SUPERSEDED: "Replaced by a newer round",
}
YOUR_ANSWER_LABELS = {APPROVED: "You approved", DECLINED: "You declined", NOT_NEEDED: "Settled without your answer", PENDING: "Not answered"}


def card_shell(body: list, actions: Optional[list] = None) -> dict:
    """An Adaptive Card around these body elements and buttons. The shared frame for every card the bot sends."""
    card = {"$schema": SCHEMA, "type": "AdaptiveCard", "version": VERSION, "body": body}
    if actions:
        card["actions"] = actions
    return card


def open_ticket_action(ticket: Optional[Ticket]) -> list:
    """The "Open ticket" button, or nothing while the web app's address is not known or there is no ticket."""
    link = ticket_link(ticket) if ticket is not None else None
    return [{"type": "Action.OpenUrl", "title": "Open ticket", "url": link}] if link else []


def what_happened_card(notification: Notification, ticket: Optional[Ticket]) -> dict:
    """What happened and who did it, with an Open ticket button."""
    facts = [{"title": "What", "value": KIND_LABELS.get(notification.kind, plain(notification.kind))}]
    if notification.actor_email:
        facts.append({"title": "By", "value": plain(notification.actor_email)})
    body = [
        {"type": "TextBlock", "text": plain(notification_text(notification, ticket)), "wrap": True},
        {"type": "FactSet", "facts": facts},
    ]
    return card_shell(body, open_ticket_action(ticket))


def portal_action(ticket: Ticket) -> list:
    """The button to the request's page in the portal (an approver is often not an agent, so not the console)."""
    link = portal_link(ticket)
    return [{"type": "Action.OpenUrl", "title": "Open in the portal", "url": link}] if link else []


def request_facts(session: Session, ticket: Ticket, approval: TicketApproval) -> list:
    """What the approver needs to decide: the type of request, who asked, what they answered, and the vote needed."""
    request_type = session.get(RequestType, ticket.request_type_id) if ticket.request_type_id else None
    facts = []
    if request_type is not None:
        facts.append({"title": "Request", "value": plain(request_type.name)})
    facts.append({"title": "Asked by", "value": plain(ticket.requester_email)})
    answers = requester_visible_answers(session, ticket)
    facts += [{"title": plain(row["label"]), "value": plain(row["display"] or "-")} for row in answers[:MAX_ANSWERS]]
    if len(answers) > MAX_ANSWERS:
        facts.append({"title": "More answers", "value": f"{len(answers) - MAX_ANSWERS} more in the portal"})
    facts.append({"title": "Needed", "value": plain(describe_rule(approval))})
    return facts


def approval_request_card(session: Session, notification: Notification, ticket: Optional[Ticket]) -> dict:
    """The card for a request waiting for this person's vote: the request, Approve and Decline buttons with an
    optional comment box, and a link to the portal page. Raises Unsendable when there is nothing to vote on any more
    (answered, settled, withdrawn, or the person was taken out of the voters), so a stale copy is never sent. The
    buttons carry the ids of the approval and the vote, which only say what is being answered: who answers is
    always the person Teams says pressed the button (app/teams_approvals.py)."""
    person = session.get(User, notification.user_id)
    vote = vote_for(session, ticket, person) if ticket is not None and person is not None else None
    approval = session.get(TicketApproval, vote.approval_id) if vote is not None else None
    if approval is None or approval.status != PENDING or vote.decision != PENDING or ticket.status != TicketStatus.AWAITING_APPROVAL:
        raise Unsendable("The request is no longer waiting for this person's answer")
    if not person.is_active or is_requester(ticket, person) or not is_member(session, approval.group_id, person.id):
        raise Unsendable("The person can no longer answer this request")  # the buttons would only be refused
    data = {"approval": approval.id, "vote": vote.id}
    body = [
        {"type": "TextBlock", "text": plain(notification_text(notification, ticket)), "wrap": True, "weight": "Bolder"},
        {"type": "FactSet", "facts": request_facts(session, ticket, approval)},
        {"type": "Input.Text", "id": "comment", "placeholder": "Comment (optional)", "isMultiline": True, "maxLength": MAX_COMMENT_LENGTH},
    ]
    actions = [
        {"type": "Action.Execute", "title": "Approve", "verb": VERB_APPROVE, "data": data, "style": "positive"},
        {"type": "Action.Execute", "title": "Decline", "verb": VERB_DECLINE, "data": data, "style": "destructive"},
        *portal_action(ticket),
    ]
    return card_shell(body, actions)


def decided_card(session: Session, vote: ApprovalVote, note: Optional[str] = None) -> dict:
    """The card that replaces the request once it is answered or no longer waiting: who decided and what came of it,
    with no buttons, so a second press is not possible. `note` is why a press did nothing (already answered, settled,
    withdrawn), shown beneath."""
    approval = session.get(TicketApproval, vote.approval_id)
    ticket = session.get(Ticket, approval.ticket_id)
    voter = session.get(User, vote.voter_id)
    facts = []
    if vote.decision in (APPROVED, DECLINED):
        facts.append({"title": "Decided by", "value": plain(voter.display_name or voter.email)})
    facts.append({"title": "Your answer", "value": YOUR_ANSWER_LABELS.get(vote.decision, plain(vote.decision))})
    if vote.comment:
        facts.append({"title": "Comment", "value": plain(vote.comment)})
    facts.append({"title": "Outcome", "value": OUTCOME_LABELS.get(approval.status, plain(approval.status))})
    body = [
        {"type": "TextBlock", "text": plain(f"{ticket.reference} {ticket.subject}"), "wrap": True, "weight": "Bolder"},
        {"type": "FactSet", "facts": facts},
    ]
    if note:
        body.append({"type": "TextBlock", "text": plain(note), "wrap": True, "isSubtle": True})
    return card_shell(body, portal_action(ticket))


VERB_PICK = "intake.pick"  # 8D: add the message to this open request
VERB_NEW = "intake.new"  # 8D: raise a new request from the message
MAX_CHOICES = 5  # a longer list is a wall of buttons; the rest are reached by writing new: or on the site
CHOICE_TITLE_LENGTH = 70


def choose_ticket_card(tickets: list[Ticket], activity_id: str, text: str) -> dict:
    """The card that asks which request a message belongs to (8D): one button per request the person has open from Teams
    (at most MAX_CHOICES, most recently active first) and one for a new request. The card carries the message itself and
    the id of the activity it came in, so pressing a button needs no stored copy; the press only names a request, and
    app/teams_intake.py refuses it unless that request is one of the pressing person's own. A Resolved request is
    marked: adding to it reopens it."""
    buttons = []
    for ticket in tickets[:MAX_CHOICES]:
        title = f"{ticket.reference}: {ticket.subject}"
        if ticket.status == TicketStatus.RESOLVED:
            title = f"{title} (resolved, reopens)"
        buttons.append({
            "type": "Action.Execute", "title": " ".join(title.split())[:CHOICE_TITLE_LENGTH], "verb": VERB_PICK,  # a button title is not markdown
            "data": {"m": activity_id, "t": text, "n": ticket.number},
        })
    buttons.append({"type": "Action.Execute", "title": "A new request", "verb": VERB_NEW, "data": {"m": activity_id, "t": text}, "style": "positive"})
    body = [{"type": "TextBlock", "text": "Which request is this message about?", "wrap": True, "weight": "Bolder"}]
    if len(tickets) > MAX_CHOICES:
        body.append({"type": "TextBlock", "text": f"Showing {MAX_CHOICES} of your {len(tickets)} requests.", "wrap": True, "isSubtle": True})
    body.append({"type": "TextBlock", "text": plain(text), "wrap": True, "isSubtle": True})
    return card_shell(body, buttons)


def intake_result_card(text: str, ticket: Optional[Ticket] = None) -> dict:
    """What replaces the choice card once it is answered: what happened, with no buttons so a second press does nothing."""
    return card_shell([{"type": "TextBlock", "text": text, "wrap": True}], portal_action(ticket) if ticket is not None else None)


Builder = Callable[[Session, Notification, Optional[Ticket]], dict]
# Kinds that have a card of their own.
CARD_FOR_KIND: dict[str, Builder] = {APPROVAL_WAITING: approval_request_card}


def notification_card(session: Session, notification: Notification, ticket: Optional[Ticket]) -> dict:
    builder = CARD_FOR_KIND.get(notification.kind)
    return builder(session, notification, ticket) if builder is not None else what_happened_card(notification, ticket)
