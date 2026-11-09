from sqlmodel import select

from app.models import MembershipLevel, Message, MessageDirection, Ticket, UserRole
from app.team_activity import describe
from tests.conftest import add_membership, create_category, create_team, login_as, set_role
from tests.test_raising import desk, raise_it


def classified(client, category, subject, **extra):
    """A ticket an agent raised and classified into the category's team."""
    response = raise_it(
        client, subject=subject, for_email="walkin@example.com", category_id=category.id,
        impact="low", urgency="low", **extra,
    )
    assert response.status_code == 204, response.text


def ticket_named(db_session, subject):
    db_session.expire_all()
    return db_session.exec(select(Ticket).where(Ticket.subject == subject)).one()


def feed(client, **params):
    return client.get("/tickets/activity", params=params)


# ---- What shows ----------------------------------------------------------------------------------


def test_the_feed_shows_events_emails_replies_and_notes_newest_first(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Printer jam")
    ticket = ticket_named(db_session, "Printer jam")
    client.post(f"/tickets/{ticket.number}/email", data={"body": "Have you tried turning it off?"})
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Probably the fuser"})

    page = feed(client).text

    assert ticket.reference in page and "Printer jam" in page
    assert "emailed the requester: Have you tried turning it off?" in page
    assert "added an internal note: Probably the fuser" in page
    assert "message from the requester: It is smoking" in page  # the description logged for them
    assert "triaged" in page and "created" in page
    assert page.index("Probably the fuser") < page.index("Have you tried") < page.index("It is smoking")
    assert "<td>Service Desk</td>" in page
    assert "agent@example.com" in page or "Agent Ann" in page


def test_other_teams_work_is_not_in_the_feed(client, db_session):
    team, category, _ = desk(client, db_session)
    other = create_team(db_session, "Network")
    elsewhere = create_category(db_session, other, "Wireless")
    classified(client, category, "Mine")
    classified(client, elsewhere, "Theirs")
    theirs = ticket_named(db_session, "Theirs")

    page = feed(client).text

    assert "Mine" in page and "Theirs" not in page
    assert theirs.reference not in page
    assert "Theirs" not in feed(client, team_id=other.id).text  # asking for their team gets nothing either


def test_a_viewer_sees_the_teams_activity_and_the_shared_triage_queue_shows_to_all(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Classified work")
    raise_it(client, subject="Waiting in the queue", for_email="walkin@example.com")  # untriaged
    client.post("/auth/logout")
    other = create_team(db_session, "Network")
    watcher = login_as(client, "watcher@example.com", "Wanda Watcher")
    set_role(db_session, watcher, UserRole.AGENT)
    add_membership(db_session, watcher, team.id, MembershipLevel.VIEWER)

    page = feed(client).text
    assert "Classified work" in page and "Waiting in the queue" in page

    client.post("/auth/logout")
    outsider = login_as(client, "outsider@example.com")
    set_role(db_session, outsider, UserRole.AGENT, other.id)
    page = feed(client).text
    assert "Classified work" not in page  # not their team
    assert "Waiting in the queue" in page  # untriaged tickets are visible to every agent


def test_a_ticket_moved_to_another_team_leaves_the_feed_history_and_all(client, db_session):
    team, category, agent_id = desk(client, db_session)
    other = create_team(db_session, "Network")
    classified(client, category, "Moving house")
    ticket = ticket_named(db_session, "Moving house")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "Secret handover detail"})
    assert "Secret handover detail" in feed(client).text

    client.post(f"/tickets/{ticket.number}/reassign", data={"team_id": other.id})

    page = feed(client).text
    assert "Moving house" not in page and "Secret handover detail" not in page
    client.post("/auth/logout")
    new_agent = login_as(client, "net@example.com")
    set_role(db_session, new_agent, UserRole.AGENT, other.id)
    assert "Secret handover detail" in feed(client).text  # the new team sees the history


def test_a_super_admin_sees_everything_and_can_narrow_by_team(client, db_session):
    team, category, _ = desk(client, db_session)
    other = create_team(db_session, "Network")
    elsewhere = create_category(db_session, other, "Wireless")
    classified(client, category, "Desk work")
    classified(client, elsewhere, "Network work")
    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)

    both = feed(client).text
    assert "Desk work" in both and "Network work" in both
    only = feed(client, team_id=other.id).text
    assert "Network work" in only and "Desk work" not in only


def test_only_agents_can_open_it(client, db_session):
    assert client.get("/tickets/activity", follow_redirects=False).status_code in (303, 307, 401, 403)
    login_as(client, "jo@example.com")
    assert feed(client).status_code == 403


# ---- Filters, paging, wording ------------------------------------------------------------------------


def test_the_filter_form_with_blank_choices_means_no_filter(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Anything")

    for params in ({"team_id": "", "kind": ""}, {"team_id": str(team.id), "kind": ""}, {"team_id": "", "kind": "notes"}):
        response = feed(client, **params)
        assert response.status_code == 200, params
    assert "Anything" in feed(client, team_id="", kind="").text
    assert feed(client, team_id="abc").status_code == 400


def test_an_untriaged_ticket_shows_unassigned_as_its_team(client, db_session):
    desk(client, db_session)
    raise_it(client, subject="No team yet", for_email="walkin@example.com")

    page = feed(client).text

    assert "No team yet" in page and "<td>Unassigned</td>" in page


def test_the_kind_filter(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Mixed")
    ticket = ticket_named(db_session, "Mixed")
    client.post(f"/tickets/{ticket.number}/email", data={"body": "An email out"})
    client.post(f"/tickets/{ticket.number}/note", data={"body": "A private note"})

    def content(response):  # the page's own content: the frame's navigation has an "Untriaged" item (9A)
        return response.text.split("<main", 1)[1]

    notes = content(feed(client, kind="notes"))
    assert "A private note" in notes and "An email out" not in notes and "triaged" not in notes
    conversation = content(feed(client, kind="conversation"))
    assert "An email out" in conversation and "It is smoking" in conversation
    assert "A private note" not in conversation and "triaged" not in conversation
    changes = feed(client, kind="events").text
    assert "triaged" in changes and "An email out" not in changes and "A private note" not in changes
    assert feed(client, kind="gossip").status_code == 422


def test_long_messages_are_cut_to_an_excerpt(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Wordy")
    ticket = ticket_named(db_session, "Wordy")
    client.post(f"/tickets/{ticket.number}/note", data={"body": "x" * 500 + "TAILMARKER"})

    page = feed(client, kind="notes").text

    assert "x" * 160 + "..." in page and "x" * 161 not in page and "TAILMARKER" not in page
    client.post(f"/tickets/{ticket.number}/note", data={"body": "y" * 160})
    assert "y" * 160 + "..." not in feed(client, kind="notes").text  # exactly 160 is not cut


def test_the_feed_pages_and_keeps_its_filters(client, db_session):
    team, category, _ = desk(client, db_session)
    classified(client, category, "Busy")
    ticket = ticket_named(db_session, "Busy")
    for i in range(55):
        client.post(f"/tickets/{ticket.number}/note", data={"body": f"Note {i:02d}"})

    first = feed(client, kind="notes").text
    assert "Note 54" in first and "Note 04" not in first
    second = feed(client, kind="notes", page=2).text
    assert "Note 04" in second and "Note 54" not in second
    assert "kind=notes" in first.replace("&amp;", "&")


def test_describe_reads_events_and_messages():
    class Row:
        def __init__(self, **fields):
            self.__dict__.update(
                dict(kind="event", what="", from_value=None, to_value=None, detail=None, actor=None) | fields
            )

    assert describe(Row(what="status_changed", from_value="open", to_value="resolved")) == (
        "status changed: open -> resolved"
    )
    assert describe(Row(what="status_changed", to_value="open", detail="from its sub-tickets")) == (
        "status changed: none -> open (from its sub-tickets)"
    )
    assert describe(Row(kind="message", what=MessageDirection.INBOUND.value, detail="hello")) == (
        "message from the requester: hello"
    )
    assert describe(Row(kind="message", what=MessageDirection.OUTBOUND.value)) == "emailed the requester"
