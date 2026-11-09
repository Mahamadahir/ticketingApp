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
from app.models import ApprovalVote, Category, Notification, RequestType, Ticket, TicketApproval, TicketStatus, User
from app.notification_settings import AGENT_ROLES
from app.notifications import APPROVAL_WAITING, notification_text
from app.teams import AUDIENCE_AGENT, AUDIENCE_REQUESTER, KIND_LABELS, Unsendable, link_for, plain

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


def open_ticket_action(ticket: Optional[Ticket], audience: str = AUDIENCE_AGENT) -> list:
    """The button to the ticket for this audience (`teams.link_for`): "Open ticket" in the console for an agent, "Open in
    the portal" for a requester. Nothing while the web app's address is not known or there is no ticket."""
    link = link_for(ticket, audience) if ticket is not None else None
    if not link:
        return []
    return [{"type": "Action.OpenUrl", "title": "Open ticket" if audience == AUDIENCE_AGENT else "Open in the portal", "url": link}]


def audience_of(person: Optional[User]) -> str:
    """Agents are sent to the console, everyone else to the portal."""
    return AUDIENCE_AGENT if person is not None and person.role in AGENT_ROLES else AUDIENCE_REQUESTER


def what_happened_card(notification: Notification, ticket: Optional[Ticket], audience: str = AUDIENCE_AGENT) -> dict:
    """What happened and who did it, with a button to the ticket for the audience."""
    facts = [{"title": "What", "value": KIND_LABELS.get(notification.kind, plain(notification.kind))}]
    if notification.actor_email:
        facts.append({"title": "By", "value": plain(notification.actor_email)})
    body = [
        {"type": "TextBlock", "text": plain(notification_text(notification, ticket)), "wrap": True},
        {"type": "FactSet", "facts": facts},
    ]
    return card_shell(body, open_ticket_action(ticket, audience))


def portal_action(ticket: Ticket) -> list:
    """The button to the request's page in the portal (an approver is often not an agent, so not the console)."""
    return open_ticket_action(ticket, AUDIENCE_REQUESTER)


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
VERB_MORE = "intake.more"  # 8F: show another page of the person's requests on the choice card
VERB_MOVE = "intake.move"  # 8F: "Not this one? Start a new request": move the message just added to a new request
MAX_CHOICES = 5  # requests shown on one page of the choice card; "Show more" pages through the rest
CHOICE_TITLE_LENGTH = 70


def category_name(session: Session, ticket: Ticket) -> str:
    category = session.get(Category, ticket.category_id) if ticket.category_id else None
    return plain(category.name) if category is not None else "Not categorised yet"


def opened_on(ticket: Ticket) -> str:
    return f"{ticket.created_at.day} {ticket.created_at.strftime('%b %Y')}"


def ticket_facts(session: Session, ticket: Ticket, with_status: bool = False) -> list:
    """What tells the person which request this is: reference, subject, category and the date it was opened (and, on the
    choice card, its status)."""
    facts = [
        {"title": "Request", "value": ticket.reference},
        {"title": "Subject", "value": plain(ticket.subject)},
        {"title": "Category", "value": category_name(session, ticket)},
        {"title": "Opened", "value": opened_on(ticket)},
    ]
    if with_status:
        status = ticket.status.value.replace("_", " ").capitalize()
        facts.append({"title": "Status", "value": status + (" (adding to it reopens it)" if ticket.status == TicketStatus.RESOLVED else "")})
    return facts


def choose_ticket_card(session: Session, tickets: list[Ticket], activity_id: str, text: str, page: int = 0) -> dict:
    """The card that asks which request a message belongs to (8D, 8F): each request the person has open as requester (most
    recently updated first) with its reference, subject, category, date opened and status, and a button to add the message to
    it, MAX_CHOICES to a page with "Show more" (and "Show newer") to page through the rest, and one button for a new request.
    The card carries the message itself and the id of the activity it came in, so pressing a button needs no stored copy;
    the press only names a request (or a page), and app/teams_intake.py refuses it unless that request is one of the pressing
    person's own. A Resolved request is marked: adding to it reopens it."""
    last_page = max((len(tickets) - 1) // MAX_CHOICES, 0)
    page = min(max(page, 0), last_page)
    shown = tickets[page * MAX_CHOICES:(page + 1) * MAX_CHOICES]
    body = [{"type": "TextBlock", "text": "Which request is this message about?", "wrap": True, "weight": "Bolder"}]
    if len(tickets) > MAX_CHOICES:
        first = page * MAX_CHOICES + 1
        body.append({"type": "TextBlock", "text": f"Showing {first} to {first + len(shown) - 1} of your {len(tickets)} requests.", "wrap": True, "isSubtle": True})
    body.append({"type": "TextBlock", "text": plain(text), "wrap": True, "isSubtle": True})
    for ticket in shown:
        body.append({
            "type": "Container", "separator": True,
            "items": [
                {"type": "FactSet", "facts": ticket_facts(session, ticket, with_status=True)},
                {"type": "ActionSet", "actions": [{
                    "type": "Action.Execute", "title": f"Add to {ticket.reference}", "verb": VERB_PICK,
                    "data": {"m": activity_id, "t": text, "n": ticket.number},
                }]},
            ],
        })
    actions = [{"type": "Action.Execute", "title": "A new request", "verb": VERB_NEW, "data": {"m": activity_id, "t": text}, "style": "positive"}]
    if page < last_page:
        actions.append({"type": "Action.Execute", "title": "Show more", "verb": VERB_MORE, "data": {"m": activity_id, "t": text, "p": page + 1}})
    if page > 0:
        actions.append({"type": "Action.Execute", "title": "Show newer", "verb": VERB_MORE, "data": {"m": activity_id, "t": text, "p": page - 1}})
    return card_shell(body, actions)


def added_card(session: Session, ticket: Ticket, activity_id: str, note: str = "") -> dict:
    """What the bot answers when a message was added to the person's only open request without asking: it names the request
    and offers "Not this one? Start a new request", which moves the message (and its files) to a new request."""
    body = [
        {"type": "TextBlock", "text": f"Added to {ticket.reference}.", "wrap": True, "weight": "Bolder"},
        {"type": "FactSet", "facts": ticket_facts(session, ticket)},
    ]
    if note:
        body.append({"type": "TextBlock", "text": note, "wrap": True, "isSubtle": True})
    actions = [{"type": "Action.Execute", "title": "Not this one? Start a new request", "verb": VERB_MOVE, "data": {"m": activity_id}}]
    return card_shell(body, [*actions, *open_ticket_action(ticket, AUDIENCE_REQUESTER)])


def intake_result_card(text: str, ticket: Optional[Ticket] = None) -> dict:
    """What replaces the choice card once it is answered: what happened, with no buttons so a second press does nothing."""
    return card_shell([{"type": "TextBlock", "text": text, "wrap": True}], portal_action(ticket) if ticket is not None else None)


Builder = Callable[[Session, Notification, Optional[Ticket]], dict]
# Kinds that have a card of their own.
CARD_FOR_KIND: dict[str, Builder] = {APPROVAL_WAITING: approval_request_card}


def notification_card(session: Session, notification: Notification, ticket: Optional[Ticket]) -> dict:
    builder = CARD_FOR_KIND.get(notification.kind)
    if builder is not None:
        return builder(session, notification, ticket)
    return what_happened_card(notification, ticket, audience_of(session.get(User, notification.user_id)))
