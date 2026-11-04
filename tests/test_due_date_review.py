"""Follow-ups found in code review of the Due date work (tests/test_due_date.py has the main tests)."""

from datetime import datetime, timedelta, timezone

from app.notifications import notification_text
from app.models import Ticket, TicketStatus, TicketType, User, UserRole
from app.settings_store import SCHEDULED_MORNING_HOUR, set_setting
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_due_date import day, desk, offering, scheduled, today, untriaged, utc_default_zone, worked  # noqa: F401
from tests.test_raising import last_ticket, raise_it
from tests.test_triage import agent


def test_moving_required_by_at_triage_with_the_old_due_still_in_the_box_is_not_a_conflict(client, db_session):
    request_type = offering(db_session)
    team = create_team(db_session, "Service Desk")
    category = create_category(db_session, team, "Hardware")
    login_as(client, "jo@example.com", "Jo")
    raise_it(client, request_type_id=request_type.id, required_by=day(10))
    ticket = last_ticket(db_session)
    client.post("/auth/logout")
    agent(client, db_session, member_of=[team])
    assert f'name="due_on" value="{day(9)}"' in client.get(f"/tickets/{ticket.number}").text  # prefilled

    response = client.post(
        f"/tickets/{ticket.number}/triage",
        data={
            "ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id,
            "required_by": day(4), "due_on": day(9), "has_due": "1",
        },
    )

    assert response.status_code == 200, response.text
    db_session.refresh(ticket)
    assert ticket.required_by == today() + timedelta(days=4) and ticket.due_on == today() + timedelta(days=3)


def test_changing_the_kind_of_request_at_triage_works_the_default_due_out_again(client, db_session):
    short, long = offering(db_session, "Short", due_days_before=1), offering(db_session, "Long", due_days_before=5)
    team, ticket = untriaged(db_session, short)
    ticket.type, ticket.required_by, ticket.due_on = TicketType.SERVICE_REQUEST, today() + timedelta(days=12), today() + timedelta(days=11)
    db_session.add(ticket)
    db_session.commit()
    category = create_category(db_session, team, "Hardware")
    agent(client, db_session, member_of=[team])

    client.post(
        f"/tickets/{ticket.number}/triage",
        data={"ticket_type": "service_request", "impact": "low", "urgency": "low", "category_id": category.id, "request_type_id": long.id},
    )

    db_session.refresh(ticket)
    assert ticket.request_type_id == long.id and ticket.due_on == today() + timedelta(days=7)


def test_the_requester_does_not_move_the_day_work_is_scheduled_for_only_clamp_it(client, db_session):
    request_type = offering(db_session)
    login_as(client, "jo@example.com", "Jo")
    raise_it(client, request_type_id=request_type.id, required_by=day(10))
    ticket = last_ticket(db_session)
    ticket.triaged_at, ticket.status, ticket.due_on = datetime.now(timezone.utc), TicketStatus.SCHEDULED, today() + timedelta(days=6)
    db_session.add(ticket)
    db_session.commit()

    client.post(f"/portal/{ticket.number}/required-by", data={"required_by": day(20)}, follow_redirects=False)
    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=6)  # the planned day stays

    client.post(f"/portal/{ticket.number}/required-by", data={"required_by": day(3)}, follow_redirects=False)
    db_session.refresh(ticket)
    assert ticket.due_on == today() + timedelta(days=3)  # never after Required by


def test_scheduling_for_a_day_whose_morning_has_started_is_refused(client, db_session):
    team, _, _ = desk(client, db_session)
    ticket = worked(db_session, team, due_on=today())
    set_setting(db_session, SCHEDULED_MORNING_HOUR, "0")
    db_session.commit()

    response = client.post(f"/tickets/{ticket.number}/status", data={"status": "scheduled"})

    assert response.status_code == 400 and "morning of that day has already started" in response.text


def test_the_triage_swap_keeps_the_reason_box_and_reads_only_its_own_form(client, db_session):
    request_type = offering(db_session)
    _, ticket = untriaged(db_session, request_type)
    ticket.due_on = today() + timedelta(days=4)
    db_session.add(ticket)
    db_session.commit()
    agent(client, db_session)

    page = client.get(f"/tickets/{ticket.number}").text
    assert 'hx-include="closest form"' in page and 'name="due_reason"' in page
    swapped = client.get("/tickets/triage-dates", params={"request_type_id": request_type.id, "due_on": day(4), "has_due": "1"})
    assert 'name="due_reason"' in swapped.text and 'name="has_due"' in swapped.text


def test_scheduling_a_split_original_gives_the_follows_its_parts_message(client, db_session):
    from tests.test_split_family import family

    original, _, _ = family(client, db_session, parts=2)

    response = client.post(f"/tickets/{original.number}/status", data={"status": "scheduled", "due_on": day(3)})

    assert response.status_code == 400 and "follows them" in response.text
    db_session.expire_all()
    assert db_session.get(Ticket, original.id).due_on is None


def test_the_bell_text_uses_the_number_not_the_reference_that_follows_the_type(db_session):
    from app.notifications import notify_due_today

    team = create_team(db_session, "Service Desk")
    assignee = User(email="ann@example.com", display_name="Ann")
    db_session.add(assignee)
    db_session.commit()
    set_role(db_session, assignee.id, UserRole.AGENT, team.id)
    ticket = scheduled(db_session, team, today(), assignee=assignee.id)

    notification = notify_due_today(db_session, ticket)

    assert notification_text(notification, ticket) == f"{ticket.reference} {ticket.subject} is due today"
