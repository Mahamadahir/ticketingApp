"""Phase 4G: who sees the requester's feedback. Admins all of it; an agent only the feedback about
tickets they resolved, on the ticket page and on My feedback; never another agent's."""

import pytest
from sqlmodel import select
from datetime import datetime, timedelta, timezone

from tests.conftest import login_as, set_role
from tests.test_email_preview import ticket_for_email
from app.models import ActionToken, TicketFeedback, UserRole, TeamMembership



def give(db_session, ticket, rating, comment, agent_id, n=[0]):
    n[0] += 1
    now = datetime.now(timezone.utc)
    token = ActionToken(
        ticket_id=ticket.id, kind="feedback", token_hash=f"hash{n[0]}-{rating}-{comment}",
        resolved_by_id=agent_id, expires_at=now + timedelta(days=1), used_at=now,
    )
    db_session.add(token)
    db_session.flush()
    db_session.add(TicketFeedback(
        ticket_id=ticket.id, token_id=token.id, rating=rating, comment=comment, resolved_by_id=agent_id,
    ))
    db_session.commit()


def sign_in(client, email, name):
    client.post("/auth/logout")
    return login_as(client, email, name)


@pytest.fixture
def desk_with_feedback(client, db_session):
    """Ann (agent, resolved the ticket), Bob (agent in the same team, so he can open it), an admin."""
    ticket = ticket_for_email(client, db_session)  # signs in agent@example.com (Ann) in a team
    ann = sign_in(client, "agent@example.com", "Agent Ann")
    team_id = db_session.exec(select(TeamMembership).where(TeamMembership.user_id == ann)).one().team_id
    bob = sign_in(client, "bob@example.com", "Agent Bob")
    set_role(db_session, bob, UserRole.AGENT, team_id)
    carol = sign_in(client, "carol@example.com", "Agent Carol")
    set_role(db_session, carol, UserRole.AGENT, team_id)
    admin = sign_in(client, "admin@example.com", "Admin Alice")
    set_role(db_session, admin, UserRole.SUPER_ADMIN)
    give(db_session, ticket, 5, "Ann was brilliant", ann)
    give(db_session, ticket, 2, "Bob was slow", bob)
    give(db_session, ticket, 1, "Nobody answered the robot", None)
    return ticket


def as_agent(client, email, name):
    sign_in(client, email, name)


def test_an_agent_sees_only_their_own_feedback_on_the_ticket_page(client, db_session, desk_with_feedback):
    ticket = desk_with_feedback
    as_agent(client, "bob@example.com", "Agent Bob")
    page = client.get(f"/tickets/{ticket.number}")
    assert page.status_code == 200
    assert "Bob was slow" in page.text
    assert "Ann was brilliant" not in page.text and "Nobody answered" not in page.text


def test_an_agent_who_resolved_nothing_sees_no_feedback_section(client, db_session, desk_with_feedback):
    ticket = desk_with_feedback
    as_agent(client, "carol@example.com", "Agent Carol")
    page = client.get(f"/tickets/{ticket.number}")
    assert page.status_code == 200
    assert "Requester feedback" not in page.text and "Ann was brilliant" not in page.text and "Bob was slow" not in page.text


def test_an_admin_sees_all_feedback_on_the_ticket_page(client, db_session, desk_with_feedback):
    ticket = desk_with_feedback
    as_agent(client, "admin@example.com", "Admin Alice")
    text = client.get(f"/tickets/{ticket.number}").text
    assert "Ann was brilliant" in text and "Bob was slow" in text and "Nobody answered" in text


def test_my_feedback_lists_only_the_signed_in_agents_own(client, db_session, desk_with_feedback):
    as_agent(client, "agent@example.com", "Agent Ann")
    text = client.get("/my-feedback").text
    assert "Ann was brilliant" in text
    assert "Bob was slow" not in text and "Nobody answered" not in text


def test_my_feedback_has_no_way_to_ask_for_another_agents(client, db_session, desk_with_feedback):
    as_agent(client, "agent@example.com", "Agent Ann")
    bob_id = login_as(client, "bob@example.com", "Agent Bob")
    as_agent(client, "agent@example.com", "Agent Ann")
    text = client.get("/my-feedback", params={"agent": bob_id, "agent_id": bob_id, "user_id": bob_id}).text
    assert "Bob was slow" not in text and "Ann was brilliant" in text


def test_my_feedback_filters_by_rating(client, db_session, desk_with_feedback):
    as_agent(client, "agent@example.com", "Agent Ann")
    assert "Ann was brilliant" not in client.get("/my-feedback", params={"rating": "3"}).text
    assert "Ann was brilliant" in client.get("/my-feedback", params={"rating": "5"}).text


def test_a_requester_without_the_agent_role_cannot_open_my_feedback(client, db_session, desk_with_feedback):
    sign_in(client, "jo@example.com", "Jo")
    assert client.get("/my-feedback", follow_redirects=False).status_code in (303, 403)
    assert "brilliant" not in client.get("/my-feedback").text


def test_an_agent_cannot_open_the_admin_feedback_list(client, db_session, desk_with_feedback):
    as_agent(client, "agent@example.com", "Agent Ann")
    response = client.get("/admin/feedback")
    assert response.status_code == 403
    assert "Bob was slow" not in response.text


def test_the_admin_list_shows_everything_and_filters_by_agent_and_rating(client, db_session, desk_with_feedback):
    as_agent(client, "admin@example.com", "Admin Alice")
    everything = client.get("/admin/feedback").text
    assert all(c in everything for c in ("Ann was brilliant", "Bob was slow", "Nobody answered", "Resolved automatically"))

    ann = login_as(client, "agent@example.com", "Agent Ann")
    as_agent(client, "admin@example.com", "Admin Alice")
    by_agent = client.get("/admin/feedback", params={"agent": ann}).text
    assert "Ann was brilliant" in by_agent and "Bob was slow" not in by_agent and "Nobody answered" not in by_agent

    by_rating = client.get("/admin/feedback", params={"rating": "2"}).text
    assert "Bob was slow" in by_rating and "Ann was brilliant" not in by_rating

    both = client.get("/admin/feedback", params={"agent": ann, "rating": "2"}).text
    assert "No feedback yet." in both


def test_the_admin_list_is_paginated(client, db_session, desk_with_feedback):
    ticket = desk_with_feedback
    for i in range(52):
        give(db_session, ticket, 3, f"bulk {i}", None)
    as_agent(client, "admin@example.com", "Admin Alice")
    first = client.get("/admin/feedback").text
    assert "Page 1 of 2" in first
    assert "Page 2 of 2" in client.get("/admin/feedback", params={"page": "2"}).text


def test_a_made_up_filter_value_is_refused_like_the_other_lists(client, db_session, desk_with_feedback):
    as_agent(client, "admin@example.com", "Admin Alice")
    assert client.get("/admin/feedback", params={"agent": "x", "rating": "banana"}).status_code == 400


def test_an_admins_my_feedback_page_is_still_only_their_own(client, db_session, desk_with_feedback):
    admin_id = login_as(client, "admin@example.com", "Admin Alice")
    give(db_session, desk_with_feedback, 4, "Alice did fine", admin_id)
    as_agent(client, "admin@example.com", "Admin Alice")
    text = client.get("/my-feedback").text
    assert "Alice did fine" in text and "Ann was brilliant" not in text and "Bob was slow" not in text
