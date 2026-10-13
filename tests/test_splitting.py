from sqlmodel import select

from app.models import AuditLog, Category, Message, Ticket, TicketSource, TicketStatus, TicketType, UserRole
from tests.conftest import add_membership, create_team, login_as, set_role
from tests.test_sla import triaged_ticket
from tests.test_triage import agent, events


def split(client, ticket, *parts):
    """parts are (subject, details) pairs, posted the way the form posts its boxes."""
    return client.post(
        f"/tickets/{ticket.number}/split",
        data={"subject": [s for s, _ in parts], "details": [d for _, d in parts]},
    )


def subs_of(db_session, ticket):
    db_session.expire_all()
    return db_session.exec(select(Ticket).where(Ticket.split_from_id == ticket.id).order_by(Ticket.id)).all()


def world(client, db_session):
    original, desk = triaged_ticket(client, db_session)
    return original, desk


# ---- What a split makes -------------------------------------------------------------------------


def test_each_part_becomes_a_triaged_ticket_with_the_originals_classification(client, db_session):
    original, desk = world(client, db_session)

    response = split(client, original, ("Replace keyboard", "The E key sticks"), ("Install Office", ""))

    assert response.status_code == 200
    first, second = subs_of(db_session, original)
    db_session.refresh(original)
    assert [t.subject for t in (first, second)] == ["Replace keyboard", "Install Office"]
    assert len({original.number, first.number, second.number}) == 3  # each has its own number
    for sub in (first, second):
        assert sub.split_from_id == original.id
        assert sub.requester_email == original.requester_email and sub.source == original.source
        assert (sub.type, sub.category_id, sub.team_id) == (original.type, original.category_id, original.team_id)
        assert (sub.impact, sub.urgency, sub.priority) == (original.impact, original.urgency, original.priority)
        assert sub.triaged_at is not None and sub.status == TicketStatus.OPEN
        assert sub.first_response_due_at is not None  # its own SLA clock
        assert sub.assigned_agent_id is None


def test_the_details_become_an_internal_note_and_a_blank_one_makes_none(client, db_session):
    original, _ = world(client, db_session)
    split(client, original, ("One", "  Details for one  "), ("Two", ""))

    first, second = subs_of(db_session, original)
    notes = db_session.exec(select(Message).where(Message.ticket_id.in_([first.id, second.id]))).all()

    assert [(m.ticket_id, m.body, m.direction.value) for m in notes] == [(first.id, "Details for one", "internal")]


def test_both_sides_get_an_event_and_the_audit_log_names_the_parts(client, db_session):
    original, _ = world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))

    first, second = subs_of(db_session, original)

    assert [e.to_value for e in events(db_session, original, "split")] == [first.tag, second.tag]
    [from_first] = events(db_session, first, "split_from")
    assert (from_first.to_value, from_first.actor_email) == (original.tag, "agent@example.com")
    [audit] = db_session.exec(select(AuditLog).where(AuditLog.action == "split")).all()
    assert audit.detail == f"{first.tag}, {second.tag}"


def test_both_pages_show_the_relationship(client, db_session):
    original, _ = world(client, db_session)
    split(client, original, ("Replace keyboard", ""), ("Install Office", ""))
    first, _ = subs_of(db_session, original)

    original_page = client.get(f"/tickets/{original.number}").text
    sub_page = client.get(f"/tickets/{first.number}").text

    assert "Replace keyboard" in original_page and "Install Office" in original_page
    assert f'Split from <a href="/tickets/{original.number}">{original.reference}</a>' in sub_page


def test_blank_boxes_are_skipped_so_unused_parts_do_not_matter(client, db_session):
    original, _ = world(client, db_session)

    split(client, original, ("One", ""), ("", ""), ("Two", ""), ("", ""))

    assert [t.subject for t in subs_of(db_session, original)] == ["One", "Two"]


def test_more_parts_can_be_added_later_up_to_ten_in_all(client, db_session):
    original, _ = world(client, db_session)
    assert split(client, original, ("A", ""), ("B", "")).status_code == 200

    assert split(client, original, ("C", "")).status_code == 200  # one more is fine once it has two
    assert split(client, original, *[(f"X{i}", "") for i in range(8)]).status_code == 400  # 3 + 8 > 10
    assert split(client, original, *[(f"X{i}", "") for i in range(7)]).status_code == 200  # exactly 10

    assert len(subs_of(db_session, original)) == 10


# ---- Rules ----------------------------------------------------------------------------------


def refused(client, db_session, ticket, parts, message):
    before = db_session.exec(select(Ticket)).all()
    response = split(client, ticket, *parts)
    assert response.status_code == 400 and message in response.text, response.text
    assert len(subs_of(db_session, ticket)) == 0
    assert len(db_session.exec(select(Ticket)).all()) == len(before)  # nothing half-made


def test_one_part_is_not_a_split(client, db_session):
    original, _ = world(client, db_session)
    refused(client, db_session, original, [("Only", "")], "at least 2")


def test_an_empty_form_splits_nothing_even_when_the_ticket_already_has_enough_parts(client, db_session):
    original, _ = world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    before = db_session.exec(select(AuditLog).where(AuditLog.action == "split")).all()

    response = split(client, original, ("", ""), ("", ""))

    assert response.status_code == 400 and "at least one part" in response.text
    assert len(subs_of(db_session, original)) == 2
    assert len(db_session.exec(select(AuditLog).where(AuditLog.action == "split")).all()) == len(before)


def test_the_parts_of_a_backdated_ticket_have_no_sla_either(client, db_session):
    original, _ = world(client, db_session)
    original.is_backdated = True
    db_session.add(original)
    db_session.commit()

    split(client, original, ("One", ""), ("Two", ""))

    for sub in subs_of(db_session, original):
        assert sub.is_backdated and sub.first_response_due_at is None and sub.resolution_due_at is None


def test_splitting_marks_the_original_as_touched(client, db_session):
    original, _ = world(client, db_session)
    before = original.updated_at

    split(client, original, ("One", ""), ("Two", ""))

    db_session.refresh(original)
    assert original.updated_at > before


def test_a_part_with_details_but_no_subject_is_refused_not_dropped(client, db_session):
    original, _ = world(client, db_session)
    refused(client, db_session, original, [("One", ""), ("", "typed details")], "needs a subject")


def test_a_part_needs_a_subject(client, db_session):
    original, _ = world(client, db_session)
    refused(client, db_session, original, [("One", ""), ("", "details but no subject")], "needs a subject")


def test_an_untriaged_ticket_cannot_be_split(client, db_session):
    from tests.test_triage import new_ticket

    desk = create_team(db_session, "Service Desk")
    ticket = new_ticket(db_session, desk)
    agent(client, db_session, member_of=[desk])
    refused(client, db_session, ticket, [("One", ""), ("Two", "")], "Triage this ticket")


def test_a_finished_ticket_cannot_be_split(client, db_session):
    original, _ = world(client, db_session)
    client.post(f"/tickets/{original.number}/status", data={"status": "resolved"})
    refused(client, db_session, original, [("One", ""), ("Two", "")], "cannot be split")


def test_a_sub_ticket_cannot_be_split_again_and_its_page_does_not_offer_it(client, db_session):
    original, _ = world(client, db_session)
    split(client, original, ("One", ""), ("Two", ""))
    first, _ = subs_of(db_session, original)

    assert split(client, first, ("X", ""), ("Y", "")).status_code == 400
    assert "Split ticket" not in client.get(f"/tickets/{first.number}").text
    assert subs_of(db_session, first) == []


def test_a_category_that_has_since_been_switched_off_refuses_cleanly(client, db_session):
    original, _ = world(client, db_session)
    category = db_session.get(Category, original.category_id)
    category.is_active = False
    db_session.add(category)
    db_session.commit()

    refused(client, db_session, original, [("One", ""), ("Two", "")], "no longer in use")


def test_too_many_boxes_or_too_long_a_subject_is_refused(client, db_session):
    original, _ = world(client, db_session)
    refused(client, db_session, original, [(f"P{i}", "") for i in range(11)], "Too many parts")
    refused(client, db_session, original, [("x" * 201, ""), ("Two", "")], "too long")


def test_a_split_error_reaches_the_agent_through_htmx(client, db_session):
    original, _ = world(client, db_session)

    response = client.post(
        f"/tickets/{original.number}/split",
        data={"subject": ["Only"], "details": [""]},
        headers={"HX-Request": "true"},
    )

    assert response.headers.get("HX-Retarget") == "#flash" and "at least 2" in response.text


def test_two_splits_made_at_once_cannot_go_past_the_limit(client, db_session):
    import threading
    import time

    from sqlmodel import Session

    from app import splitting
    from app.database import engine
    from app.models import User

    original, _ = world(client, db_session)
    split(client, original, *[(f"P{i}", "") for i in range(9)])
    holding, release, outcome = threading.Event(), threading.Event(), {}

    def first():  # the tenth part, then waits before committing
        with Session(engine) as s:
            actor = s.exec(select(User).where(User.email == "agent@example.com")).one()
            splitting.split_ticket(s, s.get(Ticket, original.id), actor, [("Tenth", "")])
            holding.set()
            release.wait(10)
            s.commit()

    def second():  # an eleventh, started while the first is still open
        with Session(engine) as s:
            actor = s.exec(select(User).where(User.email == "agent@example.com")).one()
            try:
                splitting.split_ticket(s, s.get(Ticket, original.id), actor, [("Eleventh", "")])
                s.commit()
                outcome["second"] = "split"
            except splitting.SplitError as error:
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

    assert outcome["second"] != "split" and len(subs_of(db_session, original)) == 10


# ---- Who may split and who sees what --------------------------------------------------------------


def test_a_viewer_cannot_split_and_the_form_is_not_offered(client, db_session):
    original, desk = world(client, db_session)
    client.post("/auth/logout")
    viewer = login_as(client, "viewer@example.com")
    set_role(db_session, viewer, UserRole.AGENT)
    add_membership(db_session, viewer, desk.id, "viewer")

    assert split(client, original, ("One", ""), ("Two", "")).status_code == 403
    assert "Split ticket" not in client.get(f"/tickets/{original.number}").text
    assert subs_of(db_session, original) == []


def test_an_end_user_cannot_split(client, db_session):
    original, _ = world(client, db_session)
    client.post("/auth/logout")
    login_as(client, "someone@example.com")

    assert split(client, original, ("One", ""), ("Two", "")).status_code == 403


def test_an_agent_not_on_the_originals_team_cannot_split_it(client, db_session):
    original, _ = world(client, db_session)
    other = create_team(db_session, "Network")
    client.post("/auth/logout")
    agent(client, db_session, member_of=[other], email="other@example.com")

    assert split(client, original, ("One", ""), ("Two", "")).status_code in (403, 404)
    assert subs_of(db_session, original) == []


def test_the_original_hides_sub_tickets_in_teams_the_viewer_cannot_see(client, db_session):
    original, desk = world(client, db_session)
    secret = create_team(db_session, "Security")
    split(client, original, ("Visible part", ""), ("Secret part", ""))
    visible, hidden = subs_of(db_session, original)
    hidden.team_id = secret.id
    db_session.add(hidden)
    db_session.commit()

    page = client.get(f"/tickets/{original.number}").text

    assert "Visible part" in page and "Secret part" not in page and hidden.reference not in page
    assert "1 more sub-ticket you cannot open" in page


def test_a_sub_ticket_of_an_original_you_cannot_see_does_not_name_it(client, db_session):
    original, desk = world(client, db_session)
    secret = create_team(db_session, "Security")
    split(client, original, ("One", ""), ("Two", ""))
    first, _ = subs_of(db_session, original)
    original.team_id = secret.id
    db_session.add(original)
    db_session.commit()

    page = client.get(f"/tickets/{first.number}").text

    assert "Split from a ticket you cannot open" in page and original.reference not in page


def test_a_sub_ticket_keeps_the_agent_raised_source_so_it_can_be_backdated(client, db_session):
    original, _ = world(client, db_session)
    original.source = TicketSource.AGENT
    db_session.add(original)
    db_session.commit()

    split(client, original, ("One", ""), ("Two", ""))

    assert {t.source for t in subs_of(db_session, original)} == {TicketSource.AGENT}
