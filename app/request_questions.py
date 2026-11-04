"""Form questions on request types, and the answers on tickets (#118 part 1). Nothing here commits.

An admin adds questions to a request type: short text, a pick list, yes/no or a date, each optionally required and
in an order they can change (`RequestTypeQuestion`). A question that has been answered is switched off, never
deleted, so the ticket keeps its answers; one never answered can be deleted.

Answers belong to the ticket (`TicketAnswer`), one row per answered question, with the value as canonical text
(the typed text, the chosen option, "yes"/"no", an ISO date) and the question's label and kind as they were when
answered. The row's existence is "is answered" and the text is what "equals", "not equals" and "is one of" compare, so
the conditions of part 2 (sub-request templates) can be evaluated on this table with no further change. A blank
answer is no row.

Who gives the answers: the requester in the portal form (required ones enforced there), or, for an email or
Teams request, the agent at triage when picking the kind of request: `triage_ticket` refuses to finish while a
required answer is missing, and a portal request self-triages (app/request_types.py) only when its required answers
are present. Agents who work the ticket can change answers later (`visibility.may_edit_answers`); each change is a
ticket event and the route writes the audit entry. A change after the first filling in sets
`ticket.answers_changed_at`, the hook for part 2: nothing is created or removed because of it.
"""

from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models import RequestType, RequestTypeQuestion, Ticket, TicketAnswer
from app.ticket_service import EVENT_ANSWER_CHANGED, record_event

KIND_TEXT, KIND_CHOICE, KIND_YES_NO, KIND_DATE = "text", "choice", "yes_no", "date"
KINDS = {
    KIND_TEXT: "Short text",
    KIND_CHOICE: "Pick list",
    KIND_YES_NO: "Yes / no",
    KIND_DATE: "Date",
}
MAX_LABEL_LENGTH = 200
MAX_OPTIONS = 30
MAX_OPTION_LENGTH = 100
MAX_ANSWER_LENGTH = 500
YES, NO = "yes", "no"


class QuestionError(ValueError):
    """A reason a question or an answer cannot be saved, worded for whoever is filling in the form."""


def parse_options(text: Optional[str]) -> list[str]:
    """A pick list typed one option per line: blank lines dropped, repeats (ignoring case) refused."""
    options: list[str] = []
    for line in (text or "").splitlines():
        option = line.strip()
        if not option:
            continue
        if len(option) > MAX_OPTION_LENGTH:
            raise QuestionError(f"An option can be at most {MAX_OPTION_LENGTH} characters")
        if option.lower() in {o.lower() for o in options}:
            raise QuestionError(f"The option {option!r} is listed twice")
        options.append(option)
    if len(options) > MAX_OPTIONS:
        raise QuestionError(f"A pick list can have at most {MAX_OPTIONS} options")
    return options


def _check_label(label: Optional[str]) -> str:
    label = (label or "").strip()
    if not label:
        raise QuestionError("A question needs some text")
    if len(label) > MAX_LABEL_LENGTH:
        raise QuestionError(f"A question can be at most {MAX_LABEL_LENGTH} characters")
    return label


def _check_options(kind: str, options_text: Optional[str]) -> list[str]:
    if kind != KIND_CHOICE:
        return []
    options = parse_options(options_text)
    if len(options) < 2:
        raise QuestionError("A pick list needs at least two options (one per line)")
    return options


def questions_for(session: Session, request_type_id: Optional[int], include_inactive: bool = False) -> list[RequestTypeQuestion]:
    """A type's questions in their order."""
    if request_type_id is None:
        return []
    query = select(RequestTypeQuestion).where(RequestTypeQuestion.request_type_id == request_type_id)
    if not include_inactive:
        query = query.where(RequestTypeQuestion.is_active)
    return list(session.exec(query.order_by(RequestTypeQuestion.position, RequestTypeQuestion.id)).all())


def has_answers(session: Session, question: RequestTypeQuestion) -> bool:
    return session.exec(select(TicketAnswer.id).where(TicketAnswer.question_id == question.id)).first() is not None


def add_question(
    session: Session, request_type: RequestType, label: str, kind: str, options_text: Optional[str] = None,
    is_required: bool = False,
) -> RequestTypeQuestion:
    if kind not in KINDS:
        raise QuestionError("Choose the kind of answer from the list")
    last = questions_for(session, request_type.id, include_inactive=True)
    question = RequestTypeQuestion(
        request_type_id=request_type.id, label=_check_label(label), kind=kind,
        options=_check_options(kind, options_text), is_required=is_required,
        position=(last[-1].position + 1) if last else 0,
    )
    session.add(question)
    session.flush()
    return question


def update_question(
    session: Session, question: RequestTypeQuestion, label: str, options_text: Optional[str],
    is_required: bool, is_active: bool,
) -> RequestTypeQuestion:
    """The kind of answer cannot change (answers already given would no longer fit it). The options of a pick list
    can: an answer given earlier keeps the text it was given with."""
    question.label = _check_label(label)
    question.options = _check_options(question.kind, options_text)
    question.is_required, question.is_active = is_required, is_active
    session.add(question)
    session.flush()
    return question


def delete_question(session: Session, question: RequestTypeQuestion) -> None:
    if has_answers(session, question):
        raise QuestionError("This question has been answered on tickets: switch it off instead of deleting it")
    session.delete(question)
    session.flush()


def move_question(session: Session, question: RequestTypeQuestion, up: bool) -> bool:
    """Swaps the question with its neighbour in the type's order. Returns whether it moved."""
    siblings = questions_for(session, question.request_type_id, include_inactive=True)
    # Renumbered first, so questions that share a position (or gaps from deleted ones) still move one place.
    for index, sibling in enumerate(siblings):
        sibling.position = index
        session.add(sibling)
    index = next(i for i, sibling in enumerate(siblings) if sibling.id == question.id)
    other = index - 1 if up else index + 1
    moved = 0 <= other < len(siblings)
    if moved:
        siblings[index].position, siblings[other].position = siblings[other].position, siblings[index].position
    session.flush()
    return moved


def clean_value(question: RequestTypeQuestion, text: Optional[str]) -> Optional[str]:
    """The canonical text of an answer, or None when blank. Raises QuestionError when it does not fit the question."""
    text = (text or "").strip()
    if not text:
        return None
    if question.kind == KIND_TEXT:
        if len(text) > MAX_ANSWER_LENGTH:
            raise QuestionError(f"The answer to {question.label!r} can be at most {MAX_ANSWER_LENGTH} characters")
        return text
    if question.kind == KIND_CHOICE:
        for option in question.options:
            if option.lower() == text.lower():
                return option
        raise QuestionError(f"Choose an answer to {question.label!r} from the list")
    if question.kind == KIND_YES_NO:
        if text.lower() not in (YES, NO):
            raise QuestionError(f"Answer {question.label!r} with yes or no")
        return text.lower()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise QuestionError(f"Enter the answer to {question.label!r} as a date (YYYY-MM-DD)")


def display_value(kind: str, value: str) -> str:
    return {YES: "Yes", NO: "No"}.get(value, value) if kind == KIND_YES_NO else value


def answers_of(session: Session, ticket: Ticket) -> dict[int, TicketAnswer]:
    """The ticket's answers by question id."""
    if ticket.id is None:
        return {}
    rows = session.exec(select(TicketAnswer).where(TicketAnswer.ticket_id == ticket.id)).all()
    return {row.question_id: row for row in rows}


def missing_required(
    session: Session, ticket: Ticket, extra: Optional[dict[int, Optional[str]]] = None
) -> list[RequestTypeQuestion]:
    """The ticket's request type's required questions (those switched on) with no answer, counting `extra`
    (question id -> canonical value, as about to be saved) over what is stored."""
    given = answers_of(session, ticket)
    extra = extra or {}
    return [
        q for q in questions_for(session, ticket.request_type_id)
        if q.is_required and not (extra[q.id] if q.id in extra else (given[q.id].value if q.id in given else None))
    ]


def _complain_missing(missing: list[RequestTypeQuestion]) -> None:
    raise QuestionError("Answer the required questions: " + ", ".join(q.label for q in missing))


def save_answers(
    session: Session, ticket: Ticket, raw: dict[int, str], actor_email: str, *, first_time: bool = False,
    require_all: bool = False,
) -> list[str]:
    """Saves the answers typed on a form (`raw`: question id -> text) for the ticket's request type. A question not
    in `raw` keeps its answer; one in it and blank loses it. Questions of another kind of request, or switched off,
    are ignored. A required question cannot be left blank in `raw`; with `require_all` every required question must
    end up answered (raising a ticket, triage). Each change is a ticket event. `first_time` is the first filling in
    (raising or triaging); any later change sets `ticket.answers_changed_at` (the hook for part 2). Returns the
    labels of the questions that changed. Raises QuestionError, having changed nothing."""
    questions = {q.id: q for q in questions_for(session, ticket.request_type_id)}
    stored = answers_of(session, ticket)
    cleaned: dict[int, Optional[str]] = {}
    for question_id, text in raw.items():
        question = questions.get(question_id)
        if question is not None:
            cleaned[question_id] = clean_value(question, text)
    blank_required = [questions[i] for i, v in cleaned.items() if v is None and questions[i].is_required]
    if blank_required:
        _complain_missing(blank_required)
    if require_all:
        missing = missing_required(session, ticket, cleaned)
        if missing:
            _complain_missing(missing)

    changed: list[str] = []
    now = datetime.now(timezone.utc)
    for question_id, value in cleaned.items():
        question, row = questions[question_id], stored.get(question_id)
        before = row.value if row is not None else None
        if before == value:
            continue
        if value is None:
            session.delete(row)
        elif row is None:
            session.add(TicketAnswer(
                ticket_id=ticket.id, question_id=question_id, question_label=question.label,
                question_kind=question.kind, value=value,
            ))
        else:
            row.value, row.question_label, row.question_kind, row.updated_at = value, question.label, question.kind, now
            session.add(row)
        record_event(
            session, ticket, EVENT_ANSWER_CHANGED, actor_email,
            from_value=display_value(question.kind, before) if before else None,
            to_value=display_value(question.kind, value) if value else None, detail=question.label,
        )
        changed.append(question.label)
    if changed and not first_time:
        ticket.answers_changed_at = now
        session.add(ticket)
    session.flush()
    return changed


def form_rows(
    session: Session, request_type_id: Optional[int], ticket: Optional[Ticket] = None
) -> list[tuple[RequestTypeQuestion, str]]:
    """The questions to show on a form for this kind of request with each one's current answer ("" when none),
    for the portal form, the triage form and the answers box on the ticket page."""
    stored = answers_of(session, ticket) if ticket is not None else {}
    return [(q, stored[q.id].value if q.id in stored else "") for q in questions_for(session, request_type_id)]


def answer_rows(session: Session, ticket: Ticket) -> list[dict]:
    """The answers on the ticket for reading, in the question order, as they were given (the label and kind
    copied when answered), including those of questions since switched off."""
    if ticket.request_type_id is None:
        return []
    order = {q.id: q.position for q in questions_for(session, ticket.request_type_id, include_inactive=True)}
    rows = [row for row in answers_of(session, ticket).values() if row.question_id in order]
    rows.sort(key=lambda row: (order[row.question_id], row.question_id))
    return [
        {"question_id": row.question_id, "label": row.question_label, "kind": row.question_kind,
         "value": row.value, "display": display_value(row.question_kind, row.value)}
        for row in rows
    ]
