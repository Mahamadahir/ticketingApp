from sqlmodel import select

from app.models import MembershipLevel, Notification, Team, Ticket, User, UserRole
from app.notifications import count_unread, mentioned_in
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_raising import desk, raise_it
from tests.test_team_activity import classified, ticket_named


def agent_in_desk(client, db_session, email, name=None, level=MembershipLevel.MEMBER):
    """Another agent in the desk team (the team exists once desk() has run)."""
    client.post("/auth/logout")
    team = db_session.exec(select(Team)).first()
    agent_id = login_as(client, email, name or email.split("@")[0].title())
    set_role(db_session, agent_id, UserRole.AGENT)
    add_membership(db_session, agent_id, team.id, level)
    return db_session.get(User, agent_id)


def back_to(client, email="agent@example.com", name="Agent Ann"):
    client.post("/auth/logout")
    login_as(client, email, name)


def notifications_for(db_session, email):
    db_session.expire_all()
    user = db_session.exec(select(User).where(User.email == email)).one()
    return db_session.exec(
        select(Notification).where(Notification.user_id == user.id).order_by(Notification.id)
    ).all()


def note(client, ticket, body):
    assert client.post(f"/tickets/{ticket.number}/note", data={"body": body}).status_code == 200


# ---- Assignment -------------------------------------------------------------------------------------


def test_assigning_a_ticket_notifies_the_assignee_but_not_yourself(client, db_session):
    team, category, ann_id = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com", "Bob Builder")
    back_to(client)
    classified(client, category, "Printer jam")
    ticket = ticket_named(db_session, "Printer jam")

    assert client.post(f"/tickets/{ticket.number}/assign").status_code == 200  # Ann takes it herself
    assert notifications_for(db_session, "agent@example.com") == []

    client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": team.id, "agent_id": bob.id})
    [told] = notifications_for(db_session, "bob@example.com")
    assert (told.kind, told.ticket_id, told.actor_email, told.read_at) == ("assigned", ticket.id, "agent@example.com", None)
    assert told.text == f"{ticket.tag} Printer jam was assigned to you"


def test_assigning_during_triage_and_by_a_super_admin_notifies_too(client, db_session):
    team, category, ann_id = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    raise_it(client, subject="Triaged with an owner", for_email="walkin@example.com",
             category_id=category.id, impact="low", urgency="low", agent_id=bob.id)
    first = ticket_named(db_session, "Triaged with an owner")
    assert [n.ticket_id for n in notifications_for(db_session, "bob@example.com")] == [first.id]

    raise_it(client, subject="For the admin to assign", for_email="walkin@example.com",
             category_id=category.id, impact="low", urgency="low")
    second = ticket_named(db_session, "For the admin to assign")
    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    client.post(f"/tickets/{second.number}/assign", data={"agent_id": bob.id})

    assert [n.ticket_id for n in notifications_for(db_session, "bob@example.com")] == [first.id, second.id]


def test_assigning_to_the_same_person_again_tells_them_nothing_new(client, db_session):
    team, category, ann_id = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Once")
    ticket = ticket_named(db_session, "Once")
    for _ in range(2):
        client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": team.id, "agent_id": bob.id})
    assert len(notifications_for(db_session, "bob@example.com")) == 1


# ---- Mentions ---------------------------------------------------------------------------------------


def test_a_mention_in_a_note_notifies_the_agent_who_can_open_the_ticket(client, db_session):
    team, category, _ = desk(client, db_session)
    agent_in_desk(client, db_session, "bob@example.com", "Bob Builder")
    back_to(client)
    classified(client, category, "Router down")
    ticket = ticket_named(db_session, "Router down")

    note(client, ticket, "Can you look at this, @bob? Thanks @bob.")

    [told] = notifications_for(db_session, "bob@example.com")  # once, however often they are named
    assert (told.kind, told.actor_email) == ("mentioned", "agent@example.com")
    assert told.text == f"Agent Ann mentioned you on {ticket.tag} Router down"
    assert notifications_for(db_session, "agent@example.com") == []


def test_mention_forms_and_what_they_do_not_match(client, db_session):
    team, category, ann_id = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    cat = agent_in_desk(client, db_session, "cat@example.com")
    back_to(client)
    classified(client, category, "Forms")
    ticket = ticket_named(db_session, "Forms")
    ann = db_session.exec(select(User).where(User.email == "agent@example.com")).one()

    def who(text):
        return sorted(u.email for u in mentioned_in(db_session, ticket, text, ann))

    assert who("@BOB please") == ["bob@example.com"]  # any case
    assert who("(@bob.)") == ["bob@example.com"]  # punctuation round it
    assert who("@bob@example.com and @cat") == ["bob@example.com", "cat@example.com"]  # the whole address too
    assert who("@agent is me") == []  # never the author
    assert who("mail bob@example.com") == []  # an address is not a mention
    assert who("@nobody") == [] and who("@") == [] and who("") == [] and who(None) == []
    assert who("@bobby") == []  # whole names only, not prefixes


def test_a_name_two_agents_share_mentions_nobody(client, db_session):
    team, category, _ = desk(client, db_session)
    agent_in_desk(client, db_session, "sam@example.com")
    agent_in_desk(client, db_session, "sam@other.example")
    back_to(client)
    classified(client, category, "Two Sams")
    ticket = ticket_named(db_session, "Two Sams")

    note(client, ticket, "@sam can you help?")

    assert notifications_for(db_session, "sam@example.com") == notifications_for(db_session, "sam@other.example") == []
    note(client, ticket, "@sam@other.example can you help?")  # the whole address says which
    assert len(notifications_for(db_session, "sam@other.example")) == 1


def test_an_agent_who_cannot_open_the_ticket_is_not_told_about_it(client, db_session):
    team, category, _ = desk(client, db_session)
    other = create_team(db_session, "Network")
    outsider = login_as(client, "outsider@example.com")
    set_role(db_session, outsider, UserRole.AGENT, other.id)
    viewer = agent_in_desk(client, db_session, "viewer@example.com", level=MembershipLevel.VIEWER)
    back_to(client)
    classified(client, category, "Desk only")
    ticket = ticket_named(db_session, "Desk only")

    note(client, ticket, "@outsider @viewer fyi")

    assert notifications_for(db_session, "outsider@example.com") == []  # not their team
    assert len(notifications_for(db_session, "viewer@example.com")) == 1  # a viewer can read it


def test_only_notes_mention_people_not_emails_to_the_requester(client, db_session):
    team, category, _ = desk(client, db_session)
    agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Emailing")
    ticket = ticket_named(db_session, "Emailing")

    client.post(f"/tickets/{ticket.number}/email", data={"body": "Hello @bob"})

    assert notifications_for(db_session, "bob@example.com") == []


def test_end_users_are_never_notified(client, db_session):
    team, category, _ = desk(client, db_session)
    from app.notifications import notify

    client.post("/auth/logout")
    jo = db_session.get(User, login_as(client, "jo@example.com"))
    assert notify(db_session, jo, "assigned", None, "x", None) is None


# ---- The bell ---------------------------------------------------------------------------------------


def test_the_bell_counts_unread_and_the_page_lists_only_my_own(client, db_session):
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    cat = agent_in_desk(client, db_session, "cat@example.com")
    back_to(client)
    classified(client, category, "Busy")
    ticket = ticket_named(db_session, "Busy")
    note(client, ticket, "@bob one")
    note(client, ticket, "@bob two")
    note(client, ticket, "@cat other")

    client.post("/auth/logout")
    login_as(client, "bob@example.com")
    assert client.get("/notifications/count").text == "Notifications (2)"
    page = client.get("/notifications").text
    assert page.count("mentioned you on") == 2 and "Cat" not in page
    assert page.index("<b>Agent Ann mentioned you on") >= 0

    client.post("/auth/logout")
    login_as(client, "cat@example.com")
    assert client.get("/notifications/count").text == "Notifications (1)"


def test_a_clear_bell_says_just_notifications(client, db_session):
    desk(client, db_session)
    response = client.get("/notifications/count")
    assert response.text == "Notifications" and response.headers["cache-control"] == "no-store"


def test_opening_one_marks_it_read_and_goes_to_the_ticket(client, db_session):
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Open me")
    ticket = ticket_named(db_session, "Open me")
    note(client, ticket, "@bob look")
    note(client, ticket, "@bob look again")
    first, second = notifications_for(db_session, "bob@example.com")
    client.post("/auth/logout")
    login_as(client, "bob@example.com")

    response = client.post(f"/notifications/{first.id}/open", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == f"/tickets/{ticket.number}"
    assert count_unread(db_session, bob) == 1
    assert client.get("/notifications/count").text == "Notifications (1)"


def test_a_get_never_marks_anything_read(client, db_session):
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Safe")
    ticket = ticket_named(db_session, "Safe")
    note(client, ticket, "@bob look")
    [one] = notifications_for(db_session, "bob@example.com")
    client.post("/auth/logout")
    login_as(client, "bob@example.com")

    assert client.get(f"/notifications/{one.id}/open").status_code == 405  # link scanners and prefetch only GET
    client.get("/notifications")
    assert count_unread(db_session, bob) == 1


def test_nobody_can_open_or_clear_someone_elses(client, db_session):
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Private")
    ticket = ticket_named(db_session, "Private")
    note(client, ticket, "@bob look")
    [one] = notifications_for(db_session, "bob@example.com")

    assert client.post(f"/notifications/{one.id}/open").status_code == 404  # Ann, not Bob
    client.post("/notifications/read-all")
    assert count_unread(db_session, bob) == 1  # Ann's "read all" cleared only her own


def test_read_all_clears_the_bell(client, db_session):
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Many")
    ticket = ticket_named(db_session, "Many")
    for i in range(3):
        note(client, ticket, f"@bob {i}")
    client.post("/auth/logout")
    login_as(client, "bob@example.com")

    assert client.post("/notifications/read-all", follow_redirects=False).status_code == 303

    assert count_unread(db_session, bob) == 0
    assert client.get("/notifications").text.count("<b>") == 0  # nothing bold: all read


def test_the_bell_is_for_agents_only_and_polls_without_clearing_messages(client, db_session):
    login_as(client, "jo@example.com")
    assert client.get("/notifications").status_code == 403
    assert client.get("/notifications/count").status_code == 403
    assert 'href="/notifications"' not in client.get("/portal").text  # no bell for end users

    client.post("/auth/logout")
    desk(client, db_session)
    page = client.get("/tickets").text
    assert 'hx-get="/notifications/count"' in page and "every 30s" in page and "data-poll" in page
    assert 'hasAttribute("data-poll")' in page  # the poll must not wipe the flash area
    assert client.get("/notifications", follow_redirects=False).status_code == 200
    client.post("/auth/logout")
    assert client.get("/notifications", follow_redirects=False).status_code in (303, 307, 401, 403)


# ---- Review fixes -----------------------------------------------------------------------------------


def test_the_stored_text_uses_the_number_not_a_prefix_that_can_change(client, db_session):
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Retyped later")
    ticket = ticket_named(db_session, "Retyped later")
    client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": team.id, "agent_id": bob.id})

    [told] = notifications_for(db_session, "bob@example.com")

    assert f"#{ticket.number} " in told.text and "INC-" not in told.text


def test_an_assignee_who_cannot_open_the_ticket_is_not_told_about_it(client, db_session):
    from app.notifications import notify_assigned

    team, category, _ = desk(client, db_session)
    other = create_team(db_session, "Network")
    outsider = db_session.get(User, login_as(client, "outsider@example.com"))
    set_role(db_session, outsider.id, UserRole.AGENT, other.id)
    back_to(client)
    classified(client, category, "Desk only")
    ticket = ticket_named(db_session, "Desk only")

    assert notify_assigned(db_session, ticket, outsider, "agent@example.com") is None
    assert notifications_for(db_session, "outsider@example.com") == []


def test_a_notification_about_a_ticket_that_left_my_team_disappears(client, db_session):
    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Moving out")
    ticket = ticket_named(db_session, "Moving out")
    note(client, ticket, "@bob look")
    classified(client, category, "Staying")
    staying = ticket_named(db_session, "Staying")
    note(client, staying, "@bob look too")
    client.post("/auth/logout")
    login_as(client, "bob@example.com")
    assert client.get("/notifications/count").text == "Notifications (2)"

    other = create_team(db_session, "Network")
    moved = db_session.get(Ticket, ticket.id)
    moved.team_id = other.id
    db_session.add(moved)
    db_session.commit()

    page = client.get("/notifications").text
    assert "Moving out" not in page and "Staying" in page
    assert client.get("/notifications/count").text == "Notifications (1)"


def test_a_super_admin_still_sees_all_of_theirs(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Admin sees")
    ticket = ticket_named(db_session, "Admin sees")
    client.post("/auth/logout")
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    from app.notifications import notify

    notify(db_session, db_session.get(User, admin_id), "assigned", ticket, "x was assigned to you", "agent@example.com")
    db_session.commit()

    assert client.get("/notifications/count").text == "Notifications (1)"


def test_read_all_is_one_statement_and_reports_how_many(client, db_session):
    from app.notifications import mark_all_read

    team, category, _ = desk(client, db_session)
    bob = agent_in_desk(client, db_session, "bob@example.com")
    back_to(client)
    classified(client, category, "Counting")
    ticket = ticket_named(db_session, "Counting")
    for i in range(4):
        note(client, ticket, f"@bob {i}")

    assert mark_all_read(db_session, bob) == 4
    assert mark_all_read(db_session, bob) == 0


def test_the_poll_is_not_swapped_into_the_menu_when_the_session_has_ended(client, db_session):
    desk(client, db_session)
    page = client.get("/tickets").text
    assert "xhr.responseURL" in page and "event.detail.shouldSwap = false" in page and "/auth/" in page


def test_there_is_an_index_for_the_unread_poll(db_session):
    from sqlalchemy import inspect

    indexes = {i["name"]: i for i in inspect(db_session.get_bind()).get_indexes("notification")}
    unread = indexes["ix_notification_user_unread"]
    assert unread["column_names"] == ["user_id"]
    assert "read_at IS NULL" in str(unread.get("dialect_options", {}).get("postgresql_where"))  # only unread rows