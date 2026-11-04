"""Automatic sub-requests: templates on a request type (#118 part 2). Nothing here commits.

An admin gives a request type sub-request templates (`RequestSubTemplate`): New starter makes New laptop, New phone
and New account. Each names the team and category of the sub-request, optionally its kind of request, impact and
urgency, a subject pattern using the form answers ("Laptop for {Name}"), a named agent or "the parent's assignee",
and "due N days before the parent's Required by". A template can have conditions on the answers, in groups: every
condition of a group must hold (AND) and the template applies when any group does (OR); no groups means always.
The operators are equals, not equals, is one of and is answered, evaluated on the ticket's `TicketAnswer` rows
(app/request_questions.py); an answer that was never given is no row, so "not equals" holds for it.

When: once approval passes, or at triage when the type needs no approval (a portal request that triages itself
included). An agent triaging by hand sees the list the rules produce, all ticked, and can untick or tick others before
confirming (`after_triage` takes that choice); an automatic triage creates everything the rules produce. When the
request must wait for approval the confirmed list is recorded on the ticket (`pending_subrequests`: the template
and the subject as confirmed) and made by `after_approval` when approval passes (approved or waived). A decline or
withdrawal keeps the list: if an agent later reopens the request and it is approved, the same list is made.

Creation reuses the splitting machinery (`app/splitting.make_sub_ticket`): each is a sub-ticket of the request with
the shared thread, the derived status and "resolve the original when every part is done". The parent's approval
covers its sub-requests unless the sub-request's own kind of request has `subrequests_need_own_approval`. The sub-ticket
cap is the one `max_sub_tickets` setting shared with manual splitting: an agent's list over it is refused, an automatic
creation makes the first ones that fit and records the rest as skipped. A template that cannot be made (its category
was switched off, say) is skipped with an event and does not stop the others.

Answers changed after sub-requests exist change nothing; `answers_changed_note` tells the agents."""

import re
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.audit import log_action
from app.categories import is_selectable, team_id_for
from app.models import (
    Category,
    RequestSubTemplate,
    RequestType,
    RequestTypeQuestion,
    Team,
    Ticket,
    TicketEvent,
    TicketStatus,
    User,
    UserRole,
)
from app.notifications import notify_subrequests_created
from app.presence import is_out_of_office
from app.request_questions import answers_of, clean_value, display_value, questions_for, QuestionError
from app.required_by import sub_request_due
from app.sla import Level, recalculate_deadlines
from app.splitting import (
    SplitError,
    lock_ticket,
    make_sub_ticket,
    max_sub_tickets,
    sub_ticket_count,
    sync_original_status,
)
from app.ticket_service import (
    EVENT_DUE_CHANGED,
    EVENT_SUBREQUEST,
    EVENT_SUBREQUEST_SKIPPED,
    EVENT_SUBREQUESTS_PLANNED,
    record_event,
)
from app.triage import TriageError, set_assignee
from app.visibility import team_members

EQUALS, NOT_EQUALS, ONE_OF, ANSWERED = "equals", "not_equals", "one_of", "answered"
OPERATORS = {EQUALS: "equals", NOT_EQUALS: "does not equal", ONE_OF: "is one of", ANSWERED: "is answered"}
# How a condition is typed in the admin box, per operator (see parse_conditions).
_SIGNS = {EQUALS: " = ", NOT_EQUALS: " != ", ONE_OF: " in ", ANSWERED: " is answered"}
GROUP_SEPARATORS = {"---", "or"}
MAX_PATTERN_LENGTH = 200
MAX_SUBJECT_LENGTH = 200
MAX_GROUPS = 10
MAX_CONDITIONS_PER_GROUP = 10
MAX_TEMPLATES_PER_TYPE = 30
MAX_DUE_DAYS = 365

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


class TemplateError(ValueError):
    """A reason a template cannot be saved, or a chosen list cannot be made, worded for the person."""


# ---- conditions ---------------------------------------------------------------------------------------------------


def parse_conditions(session: Session, request_type_id: int, text: Optional[str]) -> list[list[dict]]:
    """The conditions as typed in the admin box: one per line, groups separated by a line holding `---` or `or`.
    A line is `<question> = <answer>`, `<question> != <answer>`, `<question> in <answer> | <answer>` or `<question>
    is answered`; the question is named as on the form (the longest match wins) and each answer is checked against
    it (a pick list answer must be one of its options, yes/no, a date). Blank text means no conditions. Returns the
    stored form: a list of groups of {"question_id", "operator", "value"}."""
    questions = sorted(questions_for(session, request_type_id, include_inactive=True), key=lambda q: -len(q.label))
    groups: list[list[dict]] = [[]]
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() in GROUP_SEPARATORS:
            if groups[-1]:
                groups.append([])
            continue
        groups[-1].append(_parse_line(line, questions))
    groups = [g for g in groups if g]
    if len(groups) > MAX_GROUPS:
        raise TemplateError(f"At most {MAX_GROUPS} groups of conditions")
    if any(len(g) > MAX_CONDITIONS_PER_GROUP for g in groups):
        raise TemplateError(f"At most {MAX_CONDITIONS_PER_GROUP} conditions in a group")
    return groups


def _parse_line(line: str, questions: list[RequestTypeQuestion]) -> dict:
    lowered = line.lower()
    for question in questions:
        label = question.label.lower()
        if not lowered.startswith(label):
            continue
        rest = line[len(label):]
        if rest and not rest[0].isspace() and rest[0] not in "=!":
            continue  # the label is only the start of a longer word
        rest = rest.strip()
        if rest.lower() == "is answered":
            return {"question_id": question.id, "operator": ANSWERED, "value": ""}
        for operator, sign in ((NOT_EQUALS, "!="), (EQUALS, "="), (ONE_OF, "in ")):  # != before =
            if rest.lower().startswith(sign):
                return {
                    "question_id": question.id, "operator": operator,
                    "value": _canonical(question, operator, rest[len(sign):].strip()),
                }
    raise TemplateError(
        f"Cannot read the condition {line!r}. Write it as: question = answer, question != answer, "
        "question in answer | answer, or question is answered (the question as named on the form)"
    )


def _canonical(question: RequestTypeQuestion, operator: str, text: str) -> str:
    """The answer(s) of a condition in the canonical text the answers are stored in; several are stored one per line
    (a pick-list option is one line, so it never holds a newline)."""
    parts = [p.strip() for p in text.split("|")] if operator == ONE_OF else [text.strip()]
    parts = [p for p in parts if p]
    if not parts:
        raise TemplateError(f"Give the answer to compare {question.label!r} with")
    try:
        return "\n".join(clean_value(question, p) or "" for p in parts)
    except QuestionError as error:
        raise TemplateError(str(error))


def format_conditions(session: Session, groups: list, labels: Optional[dict[int, str]] = None) -> str:
    """The stored conditions as the admin box shows them (the inverse of parse_conditions). `labels` (question id ->
    label) saves a lookup when many are shown at once."""
    if labels is None:
        labels = {q.id: q.label for q in session.exec(select(RequestTypeQuestion)).all()}
    blocks = []
    for group in groups or []:
        lines = []
        for c in group:
            label = labels.get(c["question_id"], f"question {c['question_id']}")
            shown = c["value"].replace("\n", " | ")
            lines.append(f"{label} is answered" if c["operator"] == ANSWERED else f"{label}{_SIGNS[c['operator']]}{shown}")
        blocks.append("\n".join(lines))
    return "\n---\n".join(blocks)


def condition_holds(condition: dict, answers: dict[int, str]) -> bool:
    """One condition against the ticket's answers (question id -> canonical text, only for answered questions)."""
    given = answers.get(condition["question_id"])
    operator, wanted = condition["operator"], condition["value"]
    if operator == ANSWERED:
        return given is not None
    if operator == EQUALS:
        return given is not None and given.casefold() == wanted.casefold()
    if operator == NOT_EQUALS:
        return given is None or given.casefold() != wanted.casefold()
    if operator == ONE_OF:
        return given is not None and given.casefold() in {w.strip().casefold() for w in wanted.split("\n")}
    return False  # an operator this code does not know never matches


def template_applies(template: RequestSubTemplate, answers: dict[int, str]) -> bool:
    """Every condition of a group holds, for any group; no groups means always."""
    groups = template.condition_groups or []
    return not groups or any(all(condition_holds(c, answers) for c in group) for group in groups)


# ---- subject ------------------------------------------------------------------------------------------------------


def render_subject(pattern: str, labels_to_answers: dict[str, str], fallback: str) -> str:
    """The pattern with each {Question} replaced by its answer (blank when unanswered), in one pass, so braces in an
    answer stay as typed and are never expanded again. A {name} that is no question stays as written. An empty result
    falls back to `fallback`; the result is cut to the longest subject."""
    def replace(match: re.Match) -> str:
        key = match.group(1).strip().casefold()
        return labels_to_answers[key] if key in labels_to_answers else match.group(0)

    subject = " ".join(_PLACEHOLDER.sub(replace, pattern).split())
    return (subject or fallback)[:MAX_SUBJECT_LENGTH]


def answers_for_subject(session: Session, ticket: Ticket, answers: dict[int, str]) -> dict[str, str]:
    """The ticket's request type's questions' labels (lower case) with the answer as shown, "" when unanswered."""
    return {
        q.label.casefold(): display_value(q.kind, answers[q.id]) if q.id in answers else ""
        for q in questions_for(session, ticket.request_type_id, include_inactive=True)
    }


def ticket_answers(session: Session, ticket: Ticket) -> dict[int, str]:
    return {question_id: row.value for question_id, row in answers_of(session, ticket).items()}


# ---- templates (admin) --------------------------------------------------------------------------------------------


def templates_for(session: Session, request_type_id: int, include_inactive: bool = False) -> list[RequestSubTemplate]:
    query = select(RequestSubTemplate).where(RequestSubTemplate.request_type_id == request_type_id)
    if not include_inactive:
        query = query.where(RequestSubTemplate.is_active)
    return list(session.exec(query.order_by(RequestSubTemplate.position, RequestSubTemplate.id)).all())


def all_templates_by_type(session: Session) -> dict[int, list[RequestSubTemplate]]:
    by_type: dict[int, list[RequestSubTemplate]] = {}
    for template in session.exec(select(RequestSubTemplate).order_by(RequestSubTemplate.position, RequestSubTemplate.id)).all():
        by_type.setdefault(template.request_type_id, []).append(template)
    return by_type


def _check_template(
    session: Session, request_type: RequestType, pattern: str, team_id: Optional[int], category_id: Optional[int],
    sub_type_id: Optional[int], impact: Optional[str], urgency: Optional[str], assignee_id: Optional[int],
    parent_assignee: bool, due_days: Optional[int],
) -> str:
    pattern = (pattern or "").strip()
    if not pattern:
        raise TemplateError("A template needs a subject")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise TemplateError(f"The subject can be at most {MAX_PATTERN_LENGTH} characters")
    known = {q.label.casefold() for q in questions_for(session, request_type.id, include_inactive=True)}
    for name in _PLACEHOLDER.findall(pattern):
        if name.strip().casefold() not in known:
            raise TemplateError(f"{{{name}}} is not one of this request type's questions")
    team = session.get(Team, team_id) if team_id is not None else None
    if team is None:
        raise TemplateError("Choose the team the sub-request goes to")
    category = session.get(Category, category_id) if category_id is not None else None
    if category is None or not is_selectable(session, category):
        raise TemplateError("Choose a category that is in use")
    if team_id_for(session, category) != team.id:
        raise TemplateError(f"That category belongs to a different team than {team.name}")
    if sub_type_id is not None:
        sub_type = session.get(RequestType, sub_type_id)
        if sub_type is None:
            raise TemplateError("That kind of request does not exist")
        if sub_type.id == request_type.id:
            raise TemplateError("A sub-request cannot be of the same kind as the request it comes from")
    for value, label in ((impact, "impact"), (urgency, "urgency")):
        if value is not None and value not in {level.value for level in Level}:
            raise TemplateError(f"Choose the {label} from the list")
    if assignee_id is not None and parent_assignee:
        raise TemplateError("Name an agent or choose the parent's assignee, not both")
    if assignee_id is not None:
        agent = session.get(User, assignee_id)
        if agent is None or agent.role != UserRole.AGENT or agent.id not in {m.id for m in team_members(session, team.id)}:
            raise TemplateError(f"Choose an agent who is a member of {team.name}")
    if due_days is not None and not 0 <= due_days <= MAX_DUE_DAYS:
        raise TemplateError(f"Days before Required by must be from 0 to {MAX_DUE_DAYS}, or blank to use the setting")
    return pattern


def save_template(
    session: Session, request_type: RequestType, template: Optional[RequestSubTemplate], *,
    subject_pattern: str, team_id: Optional[int], category_id: Optional[int], sub_request_type_id: Optional[int] = None,
    impact: Optional[str] = None, urgency: Optional[str] = None, assignee_id: Optional[int] = None,
    assign_to_parent_assignee: bool = False, due_days_before: Optional[int] = None, conditions_text: Optional[str] = None,
    is_active: bool = True,
) -> RequestSubTemplate:
    """Creates (`template` None) or updates a template on the request type. Raises TemplateError."""
    pattern = _check_template(
        session, request_type, subject_pattern, team_id, category_id, sub_request_type_id, impact, urgency,
        assignee_id, assign_to_parent_assignee, due_days_before,
    )
    groups = parse_conditions(session, request_type.id, conditions_text)
    if template is None:
        existing = templates_for(session, request_type.id, include_inactive=True)
        if len(existing) >= MAX_TEMPLATES_PER_TYPE:
            raise TemplateError(f"A request type can have at most {MAX_TEMPLATES_PER_TYPE} templates")
        template = RequestSubTemplate(
            request_type_id=request_type.id, position=(existing[-1].position + 1) if existing else 0,
            subject_pattern=pattern, team_id=team_id, category_id=category_id,
        )
    template.subject_pattern, template.team_id, template.category_id = pattern, team_id, category_id
    template.sub_request_type_id, template.impact, template.urgency = sub_request_type_id, impact, urgency
    template.assignee_id, template.assign_to_parent_assignee = assignee_id, assign_to_parent_assignee
    template.due_days_before, template.condition_groups, template.is_active = due_days_before, groups, is_active
    session.add(template)
    session.flush()
    return template


def delete_template(session: Session, template: RequestSubTemplate) -> None:
    """Deletes a template. A list recorded on a ticket still waiting for approval skips it with an event."""
    session.delete(template)
    session.flush()


def move_template(session: Session, template: RequestSubTemplate, up: bool) -> bool:
    siblings = templates_for(session, template.request_type_id, include_inactive=True)
    for index, sibling in enumerate(siblings):
        sibling.position = index
        session.add(sibling)
    index = next(i for i, sibling in enumerate(siblings) if sibling.id == template.id)
    other = index - 1 if up else index + 1
    moved = 0 <= other < len(siblings)
    if moved:
        siblings[index].position, siblings[other].position = siblings[other].position, siblings[index].position
    session.flush()
    return moved


def describe_template(session: Session, template: RequestSubTemplate) -> str:
    """One line for the audit log."""
    team = session.get(Team, template.team_id)
    return f"{template.subject_pattern!r} to {team.name if team else '?'}, {len(template.condition_groups or [])} condition group(s)"


# ---- what a ticket gets -------------------------------------------------------------------------------------------


def plan_rows(
    session: Session, ticket: Ticket, answers: Optional[dict[int, str]] = None
) -> list[tuple[RequestSubTemplate, str, bool]]:
    """Every active template of the ticket's request type with the subject it would get and whether the rules produce it
    (its conditions hold on `answers`, the ticket's own by default), in template order."""
    if ticket.request_type_id is None:
        return []
    answers = ticket_answers(session, ticket) if answers is None else answers
    labels = answers_for_subject(session, ticket, answers)
    return [
        (t, render_subject(t.subject_pattern, labels, ticket.subject), template_applies(t, answers))
        for t in templates_for(session, ticket.request_type_id)
    ]


def specs_to_make(
    session: Session, ticket: Ticket, ticked: Optional[list[int]] = None, listed: Optional[list[int]] = None
) -> list[dict]:
    """What to make as [{"template_id", "subject"}]: with no choice (`listed` empty) everything the rules produce.
    With one, the ticked templates, plus any the rules produce that the agent was never shown (the list they saw was
    drawn before an answer changed)."""
    rows = plan_rows(session, ticket)
    if not listed:
        return [{"template_id": t.id, "subject": subject} for t, subject, applies in rows if applies]
    ticked_ids, listed_ids = set(ticked or []), set(listed)
    return [
        {"template_id": t.id, "subject": subject}
        for t, subject, applies in rows
        if t.id in ticked_ids or (applies and t.id not in listed_ids)
    ]


def _guard(ticket: Ticket) -> bool:
    return ticket.request_type_id is not None and ticket.split_from_id is None and ticket.triaged_at is not None


def after_triage(
    session: Session, ticket: Ticket, actor: Optional[User], ticked: Optional[list[int]] = None,
    listed: Optional[list[int]] = None, strict: Optional[bool] = None,
) -> list[Ticket]:
    """Called once a request has been triaged. When it is Open the sub-requests are made now; when it waits for
    approval the list is recorded on it and made by `after_approval`. `actor` is the agent who triaged it, or None for
    an automatic triage. A list an agent confirmed (`strict`, by default whenever there is an agent) that would go past
    the sub-ticket cap is refused (TemplateError, the caller rolls back); otherwise what fits is made and the rest
    recorded as skipped. Returns the sub-tickets made."""
    if not _guard(ticket):
        return []
    specs = specs_to_make(session, ticket, ticked, listed)
    if not specs:
        return []
    if actor is not None if strict is None else strict:
        room = max_sub_tickets(session) - sub_ticket_count(session, ticket)
        if len(specs) > room:
            raise TemplateError(
                f"That is {len(specs)} sub-requests but a ticket can have at most {max_sub_tickets(session)} sub-tickets "
                f"({max(room, 0)} more fit): untick some"
            )
    if ticket.status == TicketStatus.AWAITING_APPROVAL:
        ticket.pending_subrequests = specs
        session.add(ticket)
        record_event(
            session, ticket, EVENT_SUBREQUESTS_PLANNED, actor.email if actor else "system", to_value=str(len(specs)),
            detail="to be made once approved: " + "; ".join(s["subject"] for s in specs),
        )
        return []
    return create_from_specs(session, ticket, specs, actor)


def after_approval(session: Session, ticket: Ticket) -> list[Ticket]:
    """Approval passed (approved or waived): makes the list recorded at triage, once. Nothing recorded, nothing made."""
    specs = ticket.pending_subrequests
    if not specs or not _guard(ticket):
        return []
    ticket.pending_subrequests = None
    session.add(ticket)
    return create_from_specs(session, ticket, specs, None)


def create_from_specs(session: Session, original: Ticket, specs: list[dict], actor: Optional[User]) -> list[Ticket]:
    """Makes the sub-requests of `specs` ([{"template_id", "subject"}]) as sub-tickets of the original, in order, the
    first ones that fit under the cap. One that cannot be made (template deleted, category or team gone, kind of
    request switched off) is skipped with an event. Records an audit entry and one bell entry for the requester."""
    lock_ticket(session, original.id)
    session.refresh(original)
    actor_email = actor.email if actor is not None else "system"
    room = max_sub_tickets(session) - sub_ticket_count(session, original)
    made: list[Ticket] = []
    for spec in specs:
        subject = spec["subject"]
        if room <= 0:
            _skip(session, original, actor_email, subject, f"a ticket can have at most {max_sub_tickets(session)} sub-tickets")
            continue
        template = session.get(RequestSubTemplate, spec["template_id"])
        if template is None:
            _skip(session, original, actor_email, subject, "its template was deleted")
            continue
        try:
            with session.begin_nested():
                sub = _make(session, original, template, subject, actor)
        except (SplitError, TriageError, ValueError) as error:  # ValueError: a stored impact or urgency that is no level
            _skip(session, original, actor_email, subject, str(error))
            continue
        made.append(sub)
        room -= 1
    if made:
        original.updated_at = datetime.now(timezone.utc)
        session.add(original)
        sync_original_status(session, original.id)
        log_action(session, actor_email, "ticket", original.id, "subrequests_created", ", ".join(s.tag for s in made))
        notify_subrequests_created(session, original, made, actor_email)
    return made


def _skip(session: Session, original: Ticket, actor_email: str, subject: str, reason: str) -> None:
    record_event(session, original, EVENT_SUBREQUEST_SKIPPED, actor_email, to_value=subject, detail=reason)


def _make(session: Session, original: Ticket, template: RequestSubTemplate, subject: str, actor: Optional[User]) -> Ticket:
    category = session.get(Category, template.category_id)
    if category is None or not is_selectable(session, category):
        raise SplitError("its category is no longer in use")
    if team_id_for(session, category) != template.team_id:
        raise SplitError("its category now belongs to a different team")
    request_type = session.get(RequestType, template.sub_request_type_id) if template.sub_request_type_id else None
    impact = Level(template.impact or original.impact)
    urgency = Level(template.urgency or original.urgency)
    sub = make_sub_ticket(
        session, original, actor, subject=subject, category=category, impact=impact, urgency=urgency,
        request_type=request_type, approval_covered=request_type is None or not request_type.subrequests_need_own_approval,
        answers_required=False,  # a sub-request has no answers of its own to give: they are its parent's
        parent_event=EVENT_SUBREQUEST,
    )
    assignee = _assignee(session, template, original, sub)
    if assignee is not None:
        set_assignee(session, sub, assignee, actor)
    _set_due(session, original, sub, template, actor)
    return sub


def _assignee(session: Session, template: RequestSubTemplate, parent: Ticket, sub: Ticket) -> Optional[User]:
    """The named agent, or the parent's assignee when the template says so, if they are active, not out of office and
    work the sub-request's team; otherwise None, which leaves it unassigned in the team's queue."""
    person_id = template.assignee_id if template.assignee_id is not None else (
        parent.assigned_agent_id if template.assign_to_parent_assignee else None
    )
    person = session.get(User, person_id) if person_id is not None else None
    if person is None or not person.is_active or is_out_of_office(person):
        return None
    return person if person.id in {m.id for m in team_members(session, sub.team_id)} else None


def _set_due(session: Session, parent: Ticket, sub: Ticket, template: RequestSubTemplate, actor: Optional[User]) -> None:
    due = sub_request_due(session, parent, template.due_days_before)
    if due is None:
        return
    sub.due_on = due
    recalculate_deadlines(session, sub)
    session.add(sub)
    record_event(
        session, sub, EVENT_DUE_CHANGED, actor.email if actor else "system", to_value=due.isoformat(),
        detail="from the parent's Required by",
    )


# ---- for the ticket page ------------------------------------------------------------------------------------------


def answers_changed_note(session: Session, ticket: Ticket) -> bool:
    """True when sub-requests were made for this ticket and its answers were changed after the last of them, so the
    agents are told nothing was added or removed for the change."""
    if ticket.answers_changed_at is None:
        return False
    last = session.exec(
        select(TicketEvent.created_at).where(
            TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == EVENT_SUBREQUEST
        ).order_by(TicketEvent.created_at.desc()).limit(1)
    ).first()
    if last is None:
        return False
    changed = ticket.answers_changed_at
    return (changed if changed.tzinfo else changed.replace(tzinfo=timezone.utc)) > (
        last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    )


def pending_subjects(ticket: Ticket) -> list[str]:
    return [spec["subject"] for spec in ticket.pending_subrequests or []]


def used_by_template(session: Session, question: RequestTypeQuestion) -> bool:
    """Whether any template of the question's request type has a condition on it (it then cannot be deleted)."""
    return any(
        c["question_id"] == question.id
        for t in templates_for(session, question.request_type_id, include_inactive=True)
        for group in t.condition_groups or [] for c in group
    )


def label_used_by_template(session: Session, question: RequestTypeQuestion) -> bool:
    """Whether any template of the question's request type names it as {Label} in its subject (the question then
    cannot be deleted or renamed, or the subjects would keep the braces)."""
    label = question.label.strip().casefold()
    return any(
        name.strip().casefold() == label
        for t in templates_for(session, question.request_type_id, include_inactive=True)
        for name in _PLACEHOLDER.findall(t.subject_pattern)
    )


def preview(
    session: Session, ticket: Ticket, request_type_id: Optional[int], typed: dict[int, str],
    listed: list[int], ticked: list[int],
) -> list[dict]:
    """The sub-requests list the triage form shows for the kind of request chosen and the answers typed so far: each
    template of the kind with its subject, and whether it is ticked (what the agent ticked when they have seen it,
    otherwise whether the rules produce it)."""
    if request_type_id is None:
        return []
    probe = Ticket(subject=ticket.subject, request_type_id=request_type_id)
    stored = ticket_answers(session, ticket) if ticket.request_type_id == request_type_id else {}
    questions = {q.id: q for q in questions_for(session, request_type_id)}
    answers = dict(stored)
    for question_id, text in typed.items():
        question = questions.get(question_id)
        if question is None:
            continue
        try:
            value = clean_value(question, text)
        except QuestionError:
            value = None
        if value is None:
            answers.pop(question_id, None)
        else:
            answers[question_id] = value
    rows = []
    for template, subject, applies in plan_rows(session, probe, answers):
        rows.append({
            "template": template, "subject": subject, "applies": applies,
            "ticked": (template.id in ticked) if template.id in listed else applies,
        })
    return rows
