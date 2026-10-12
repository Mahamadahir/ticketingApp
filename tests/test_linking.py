from datetime import datetime, timezone

from sqlmodel import select

from app.models import AuditLog, Ticket, TicketStatus, TicketType, UserRole
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_triage import agent, events, new_ticket

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def triaged(db_session, team, ticket_type=TicketType.INCIDENT, subject="Something"):
    """A ticket that has been triaged, without going through the triage form."""
    category = create_category(db_session, team, f"Cat for {subject}")
    ticket = new_ticket(db_session, team, ticket_type=ticket_type)
    ticket.subject, ticket.triaged_at, ticket.status, ticket.category_id = subject, NOW, TicketStatus.OPEN, category.id
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def link(client, child, parent_reference):
    return client.post(f"/tickets/{child.id}/link", data={"parent_reference": parent_reference})


def world(client, db_session):
    desk = create_team(db_session, "Service Desk")
    agent(client, db_session, member_of=[desk])
    major = triaged(db_session, desk, TicketType.MAJOR_INCIDENT, "Email is down")
    child = triaged(db_session, desk, TicketType.INCIDENT, "Cannot send")
    return desk, major, child


# ---- Linking ------------------------------------------------------------------------------


def test_linking_records_an_event_on_both_tickets_and_the_audit_log(client, db_session):
    _, major, child = world(client, db_session)

    response = link(client, child, major.reference)

    assert response.status_code == 200
    db_session.refresh(child)
    assert child.linked_parent_id == major.id
    [on_child] = events(db_session, child, "linked")
    [on_parent] = events(db_session, major, "child_linked")
    assert (on_child.to_value, on_child.actor_email) == (major.reference, "agent@example.com")
    assert on_parent.to_value == child.reference
    audit = db_session.exec(select(AuditLog).where(AuditLog.action == "linked")).all()
    assert [a.detail for a in audit] == [major.reference]


def test_both_pages_show_the_link(client, db_session):
    _, major, child = world(client, db_session)
    link(client, child, major.reference)

    assert f"Linked to <a href=\"/tickets/{major.id}\">{major.reference}</a>" in client.get(f"/tickets/{child.id}").text
    parent_page = client.get(f"/tickets/{major.id}").text
    assert child.reference in parent_page and "Cannot send" in parent_page


def test_a_reference_is_matched_in_any_case_and_through_an_alias(client, db_session):
    desk, major, child = world(client, db_session)
    from app.models import TicketAlias
    db_session.add(TicketAlias(reference="INC-999", ticket_id=major.id))
    db_session.commit()

    assert link(client, child, f"  {major.reference.lower()} ").status_code == 200
    other = triaged(db_session, desk, subject="Second")
    assert link(client, other, "inc-999").status_code == 200
    db_session.refresh(other)
    assert other.linked_parent_id == major.id


def test_the_rules_of_a_link(client, db_session):
    desk, major, child = world(client, db_session)
    problem = triaged(db_session, desk, TicketType.PROBLEM, "Flaky relay")
    plain = triaged(db_session, desk, TicketType.INCIDENT, "Just a ticket")
    untriaged_major = new_ticket(db_session, desk, ticket_type=TicketType.MAJOR_INCIDENT)

    assert link(client, major, major.reference).status_code == 400  # itself (a valid parent type, so only the self rule stops it)
    assert link(client, child, child.reference).status_code == 400
    assert link(client, child, plain.reference).status_code == 400  # only MI or Problem can be a parent
    assert link(client, child, untriaged_major.reference).status_code == 400  # parent must be triaged
    assert link(client, child, "NOPE-1").status_code == 400
    assert link(client, child, major.reference).status_code == 200
    assert link(client, child, major.reference).status_code == 400  # already linked
    assert link(client, child, problem.reference).status_code == 400  # unlink first

    # One level only.
    third = triaged(db_session, desk, subject="Third")
    assert link(client, problem, major.reference).status_code == 200  # a Problem may sit under an MI
    assert link(client, third, problem.reference).status_code == 400  # but that parent is itself linked
    assert link(client, major, problem.reference).status_code == 400  # and a parent with children cannot be linked
    fresh_problem = triaged(db_session, desk, TicketType.PROBLEM, "Another")
    assert link(client, major, fresh_problem.reference).status_code == 400  # a parent with linked tickets cannot go under another
    db_session.refresh(child)
    assert child.linked_parent_id == major.id


def test_unlinking_records_both_sides_and_frees_the_ticket(client, db_session):
    _, major, child = world(client, db_session)
    link(client, child, major.reference)

    assert client.post(f"/tickets/{child.id}/unlink").status_code == 200

    db_session.refresh(child)
    assert child.linked_parent_id is None
    assert [e.from_value for e in events(db_session, child, "unlinked")] == [major.reference]
    assert [e.from_value for e in events(db_session, major, "child_unlinked")] == [child.reference]
    assert link(client, child, major.reference).status_code == 200  # can be linked again


def test_unlinking_a_ticket_with_no_link_changes_and_records_nothing(client, db_session):
    _, _, child = world(client, db_session)
    assert client.post(f"/tickets/{child.id}/unlink").status_code == 200
    assert events(db_session, child, "unlinked") == []


# ---- Who may link and who may see it ----------------------------------------------------------------


def test_a_viewer_cannot_link_or_unlink_and_the_form_is_not_offered(client, db_session):
    desk, other = create_team(db_session, "Service Desk"), create_team(db_session, "Network")
    mine = triaged(db_session, other, subject="Watched")
    major = triaged(db_session, desk, TicketType.MAJOR_INCIDENT, "Big one")
    agent_id = agent(client, db_session, member_of=[desk])
    add_membership(db_session, agent_id, other.id, "viewer")

    assert link(client, mine, major.reference).status_code == 403
    assert "Link under a Major Incident" not in client.get(f"/tickets/{mine.id}").text
    db_session.refresh(mine)
    assert mine.linked_parent_id is None


def test_you_cannot_link_under_a_parent_you_cannot_see_and_the_answer_does_not_reveal_it(client, db_session):
    desk, secret_team = create_team(db_session, "Service Desk"), create_team(db_session, "Security")
    hidden = triaged(db_session, secret_team, TicketType.MAJOR_INCIDENT, "Breach")
    child = triaged(db_session, desk, subject="Odd")
    agent(client, db_session, member_of=[desk])

    refused = link(client, child, hidden.reference)
    missing = link(client, child, "MI-99999")

    assert refused.status_code == missing.status_code == 400
    assert refused.text == missing.text


def test_a_viewer_of_the_parent_sees_only_the_linked_tickets_they_could_open(client, db_session):
    desk, secret_team = create_team(db_session, "Service Desk"), create_team(db_session, "Security")
    major = triaged(db_session, desk, TicketType.MAJOR_INCIDENT, "Email is down")
    open_child = triaged(db_session, desk, subject="Visible child")
    secret_child = triaged(db_session, secret_team, subject="Secret child")
    for child in (open_child, secret_child):
        child.linked_parent_id = major.id
        db_session.add(child)
    db_session.commit()
    agent(client, db_session, member_of=[desk])

    page = client.get(f"/tickets/{major.id}").text

    assert "Visible child" in page and "Secret child" not in page and secret_child.reference not in page
    assert "1 more linked ticket you cannot open" in page


def test_the_child_of_a_parent_you_cannot_see_does_not_name_it(client, db_session):
    desk, secret_team = create_team(db_session, "Service Desk"), create_team(db_session, "Security")
    hidden = triaged(db_session, secret_team, TicketType.MAJOR_INCIDENT, "Breach")
    child = triaged(db_session, desk, subject="Odd")
    child.linked_parent_id = hidden.id
    db_session.add(child)
    db_session.commit()
    agent(client, db_session, member_of=[desk])

    page = client.get(f"/tickets/{child.id}").text

    assert "Linked to a ticket you cannot open" in page
    assert hidden.reference not in page and "Breach" not in page


def test_a_super_admin_can_link_anything(client, db_session):
    desk, secret_team = create_team(db_session, "Service Desk"), create_team(db_session, "Security")
    hidden = triaged(db_session, secret_team, TicketType.PROBLEM, "Root cause")
    child = triaged(db_session, desk, subject="Odd")
    boss = login_as(client, "boss@example.com")
    set_role(db_session, boss, UserRole.SUPER_ADMIN)

    assert link(client, child, hidden.reference).status_code == 200


def test_end_users_cannot_link(client, db_session):
    _, major, child = world(client, db_session)
    client.post("/auth/logout")
    login_as(client, "someone@example.com")
    assert link(client, child, major.reference).status_code in (401, 403)


# ---- Claiming ------------------------------------------------------------------------------------------


def test_assign_to_me_cannot_take_a_ticket_from_its_assignee_but_re_assign_can(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = triaged(db_session, desk, subject="Owned")
    first = agent(client, db_session, member_of=[desk], email="first@example.com")
    ticket.assigned_agent_id = first
    db_session.add(ticket)
    db_session.commit()
    client.post("/auth/logout")
    second = agent(client, db_session, member_of=[desk], email="second@example.com")

    taken = client.post(f"/tickets/{ticket.id}/assign")

    assert taken.status_code == 400 and "Re-assign" in taken.text
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == first
    again = client.post(f"/tickets/{ticket.id}/reassign", data={"team_id": desk.id, "agent_id": second})
    assert again.status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == second


def test_an_agent_can_claim_an_unassigned_ticket_and_claiming_your_own_is_harmless(client, db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = triaged(db_session, desk, subject="Free")
    me = agent(client, db_session, member_of=[desk])

    assert client.post(f"/tickets/{ticket.id}/assign").status_code == 200
    assert client.post(f"/tickets/{ticket.id}/assign").status_code == 200
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == me


# ---- Review follow-ups ------------------------------------------------------------------------------------


def test_finished_untriaged_and_closed_parents_cannot_be_linked(client, db_session):
    desk, major, child = world(client, db_session)
    done = triaged(db_session, desk, subject="Done")
    done.status = TicketStatus.RESOLVED
    fresh = new_ticket(db_session, desk)
    closed_major = triaged(db_session, desk, TicketType.MAJOR_INCIDENT, "Old outage")
    closed_major.status = TicketStatus.CLOSED
    for ticket in (done, closed_major):
        db_session.add(ticket)
    db_session.commit()

    assert link(client, done, major.reference).status_code == 400
    assert link(client, fresh, major.reference).status_code == 400
    assert link(client, child, closed_major.reference).status_code == 400
    # A resolved parent can still take new reports of the same thing.
    closed_major.status = TicketStatus.RESOLVED
    db_session.add(closed_major)
    db_session.commit()
    assert link(client, child, closed_major.reference).status_code == 200


def test_an_agent_can_still_unlink_when_the_parent_is_no_longer_visible_to_them(client, db_session):
    desk, secret_team = create_team(db_session, "Service Desk"), create_team(db_session, "Security")
    hidden = triaged(db_session, secret_team, TicketType.MAJOR_INCIDENT, "Breach")
    child = triaged(db_session, desk, subject="Odd")
    child.linked_parent_id = hidden.id
    db_session.add(child)
    db_session.commit()
    agent(client, db_session, member_of=[desk])

    assert f"/tickets/{child.id}/unlink" in client.get(f"/tickets/{child.id}").text
    assert client.post(f"/tickets/{child.id}/unlink").status_code == 200
    db_session.refresh(child)
    assert child.linked_parent_id is None


def test_a_parent_lists_a_capped_number_and_says_how_many_more(client, db_session):
    from app.linking import MAX_LISTED

    desk, major, _ = world(client, db_session)
    for number in range(MAX_LISTED + 3):
        child = new_ticket(db_session, desk)
        child.linked_parent_id, child.triaged_at, child.subject = major.id, NOW, f"Linked-{number:03d}-end"
        db_session.add(child)
    db_session.commit()

    page = client.get(f"/tickets/{major.id}").text

    import re
    assert len(set(re.findall(r"Linked-\d{3}-end", page))) == MAX_LISTED
    assert "3 more you can open are not listed" in page


def test_a_link_error_reaches_the_agent_through_htmx(client, db_session):
    _, _, child = world(client, db_session)

    response = client.post(
        f"/tickets/{child.id}/link", data={"parent_reference": "MI-99999"}, headers={"HX-Request": "true"}
    )

    assert response.headers.get("HX-Retarget") == "#flash" and "No ticket with that reference" in response.text


def test_two_links_made_at_once_cannot_build_a_chain(client, db_session):
    import threading
    import time

    from sqlmodel import Session

    from app import linking
    from app.database import engine

    desk = create_team(db_session, "Service Desk")
    a = triaged(db_session, desk, TicketType.PROBLEM, "A")
    b = triaged(db_session, desk, TicketType.PROBLEM, "B")
    c = triaged(db_session, desk, TicketType.PROBLEM, "C")
    ids = (a.id, b.id, c.id)
    holding, release, outcome = threading.Event(), threading.Event(), {}

    def first():  # A under B, then waits before committing
        with Session(engine) as s:
            linking.link_ticket(s, s.get(Ticket, ids[0]), s.get(Ticket, ids[1]), "x@example.com")
            holding.set()
            release.wait(10)
            s.commit()

    def second():  # B under C, started while the first is still open
        with Session(engine) as s:
            try:
                linking.link_ticket(s, s.get(Ticket, ids[1]), s.get(Ticket, ids[2]), "y@example.com")
                s.commit()
                outcome["second"] = "linked"
            except linking.LinkError as error:
                outcome["second"] = str(error)

    one = threading.Thread(target=first)
    one.start()
    assert holding.wait(10)
    two = threading.Thread(target=second)
    two.start()
    time.sleep(0.7)  # with the lock, the second is waiting here
    release.set()
    one.join(10)
    two.join(10)

    db_session.expire_all()
    assert outcome["second"] != "linked", "a two-level chain was created"
    assert db_session.get(Ticket, ids[1]).linked_parent_id is None
    assert db_session.get(Ticket, ids[0]).linked_parent_id == ids[1]