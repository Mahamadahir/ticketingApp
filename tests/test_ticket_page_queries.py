"""#261 and #263: the ticket page's @ list and Attachments box ask the database a fixed number of questions, however many agents
or files there are, and answer exactly what they did before (same people, same files, same refusals)."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session

from app.attachments import store_attachment, ticket_files
from app.database import engine
from app.main import app
from app.models import MembershipLevel, Message, MessageDirection, Ticket, TicketEscalator, User, UserRole
from app.notifications import mentionable_agents
from app.storage import LocalStorage, get_storage
from app.visibility import access_context
from tests.conftest import add_membership, create_team, login_as
from tests.test_triage import new_ticket


@pytest.fixture
def storage(tmp_path):
    store = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: store
    yield store
    app.dependency_overrides.pop(get_storage, None)


class Counter:
    """Counts the SQL statements sent to the database while it is open."""

    def __enter__(self):
        self.statements = []
        event.listen(engine, "before_cursor_execute", self._record)
        return self

    def _record(self, conn, cursor, statement, *rest):
        self.statements.append(statement)

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._record)

    @property
    def count(self):
        return len(self.statements)


def make_agent(db_session, email, name, team=None, role=UserRole.AGENT, level=MembershipLevel.MEMBER, active=True):
    user_id = login_as(TestClient(app), email, name)
    user = db_session.get(User, user_id)
    user.role = role
    user.is_active = active
    db_session.add(user)
    db_session.commit()
    if team is not None:
        add_membership(db_session, user_id, team.id, level)
    return user_id


def triaged(db_session, team):
    ticket = new_ticket(db_session, team)
    ticket.triaged_at = datetime.now(timezone.utc)
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def offered(ticket, author_id, query="", limit=8):
    """What the @ list offers, read through a fresh session, with the number of statements it took."""
    with Session(engine) as session:
        author = session.get(User, author_id)
        ticket = session.get(Ticket, ticket.id)
        with Counter() as counter:
            found = mentionable_agents(session, ticket, author, query, limit)
        return [(person.email, insert) for person, insert in found], counter.count


# ---- #261 the @ list -------------------------------------------------------------------------------------------


def test_the_at_list_costs_the_same_however_many_agents_there_are(db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = triaged(db_session, desk)
    author = make_agent(db_session, "author@example.com", "Author", desk)
    make_agent(db_session, "a1@example.com", "Agent One", desk)
    _, few = offered(ticket, author)
    for number in range(2, 22):
        make_agent(db_session, f"a{number}@example.com", f"Agent {number:02d}", desk)
    found, many = offered(ticket, author, limit=50)
    assert len(found) == 21
    assert many == few <= 3


def test_each_kind_of_access_is_offered_and_nothing_else(db_session):
    parent = create_team(db_session, "Parent")
    sub = create_team(db_session, "Sub")
    sub.parent_team_id = parent.id
    db_session.add(sub)
    elsewhere = create_team(db_session, "Elsewhere")
    child_team = create_team(db_session, "Child of sub")
    child_team.parent_team_id = sub.id
    db_session.add(child_team)
    db_session.commit()
    ticket = triaged(db_session, sub)
    author = make_agent(db_session, "author@example.com", "Author", sub)
    make_agent(db_session, "member@example.com", "Sub Member", sub)
    make_agent(db_session, "viewer@example.com", "Sub Viewer", sub, level=MembershipLevel.VIEWER)
    make_agent(db_session, "parent@example.com", "Parent Member", parent)
    make_agent(db_session, "parentview@example.com", "Parent Viewer", parent, level=MembershipLevel.VIEWER)
    make_agent(db_session, "boss@example.com", "Big Boss", role=UserRole.SUPER_ADMIN)
    make_agent(db_session, "gone@example.com", "Gone Away", sub, active=False)
    make_agent(db_session, "other@example.com", "Other Team", elsewhere)
    escalator = make_agent(db_session, "esc@example.com", "Escalator", elsewhere)
    db_session.add(TicketEscalator(ticket_id=ticket.id, user_id=escalator))
    db_session.commit()
    make_agent(db_session, "child@example.com", "Child Member", child_team)  # a team below this one gets nothing
    make_agent(db_session, "requester@example.com", "A Requester", role=UserRole.END_USER)

    found, _ = offered(ticket, author, limit=50)

    assert [email for email, _ in found] == [  # in name order
        "boss@example.com", "esc@example.com", "parent@example.com", "parentview@example.com", "member@example.com", "viewer@example.com",
    ]


def test_an_untriaged_ticket_is_open_to_every_agent_and_the_author_is_left_out(db_session):
    desk = create_team(db_session, "Service Desk")
    other = create_team(db_session, "Network")
    ticket = new_ticket(db_session, desk)
    author = make_agent(db_session, "author@example.com", "Author", desk)
    make_agent(db_session, "carol@example.com", "Carol", other)
    make_agent(db_session, "nobody@example.com", "No Team")
    found, _ = offered(ticket, author)
    assert [email for email, _ in found] == ["carol@example.com", "nobody@example.com"]


def test_a_triaged_ticket_with_no_team_is_offered_to_super_admins_only(db_session):
    ticket = triaged(db_session, None)
    author = make_agent(db_session, "author@example.com", "Author", create_team(db_session, "Desk"))
    make_agent(db_session, "boss@example.com", "Boss", role=UserRole.SUPER_ADMIN)
    make_agent(db_session, "plain@example.com", "Plain", create_team(db_session, "Other"))
    found, _ = offered(ticket, author)
    assert [email for email, _ in found] == ["boss@example.com"]


def test_the_typed_text_narrows_in_the_query_and_the_limit_applies(db_session):
    desk = create_team(db_session, "Service Desk")
    ticket = triaged(db_session, desk)
    author = make_agent(db_session, "author@example.com", "Author", desk)
    make_agent(db_session, "bob@example.com", "Bob Rowe", desk)
    make_agent(db_session, "boss@example.com", "Big Boss", desk)
    make_agent(db_session, "sue.smith@example.com", "Sue", desk)
    make_agent(db_session, "zed@example.com", "Zed Quick", desk)

    def emails(q, **kw):
        return [e for e, _ in offered(ticket, author, q, **kw)[0]]

    assert emails("bo") == ["boss@example.com", "bob@example.com"]  # in name order: Big Boss, Bob Rowe
    assert emails("ro") == ["bob@example.com"]  # a word of the name
    assert emails("smith") == ["sue.smith@example.com"]  # a word of the address
    assert emails("quick") == ["zed@example.com"]
    assert emails("example") == []  # the domain does not count
    assert emails("BO") == emails("bo")
    assert emails("zzz") == []
    assert emails("100%") == [] and emails("b_b") == []  # LIKE characters are plain text
    assert len(emails("", limit=2)) == 2


def test_the_inserted_text_is_the_whole_address_when_another_agent_shares_the_name(db_session):
    desk = create_team(db_session, "Service Desk")
    other = create_team(db_session, "Network")
    ticket = triaged(db_session, desk)
    author = make_agent(db_session, "author@example.com", "Author", desk)
    make_agent(db_session, "bob@example.com", "Bob", desk)
    make_agent(db_session, "sam@example.com", "Sam One", desk)
    make_agent(db_session, "sam@elsewhere.org", "Sam Two", other)  # shares the name, though on another team: still ambiguous
    make_agent(db_session, "odd'name@example.com", "Odd", desk)  # cannot be written as a mention: never offered
    found, _ = offered(ticket, author)
    assert dict(found) == {"bob@example.com": "bob", "sam@example.com": "sam@example.com"}


# ---- #263 the Attachments box ----------------------------------------------------------------------------------


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def add_file(db_session, storage, ticket, name, uploader=None, message=None):
    data = PNG if name.endswith(".png") else name.encode() * 4
    attachment = store_attachment(db_session, storage, ticket=ticket, filename=name, data=data, message=message, uploaded_by=uploader)
    db_session.commit()
    return attachment


def add_message(db_session, ticket, sender, direction, deleted=False):
    message = Message(
        ticket_id=ticket.id, sender=sender, body="b", direction=direction, deleted_at=datetime.now(timezone.utc) if deleted else None
    )
    db_session.add(message)
    db_session.commit()
    return message


def files_for(user_id, ticket):
    with Session(engine) as session:
        user = session.get(User, user_id)
        ticket = session.get(Ticket, ticket.id)
        levels = access_context(session, user)
        with Counter() as counter:
            files = ticket_files(session, user, levels, ticket)
        return [(f.attachment.id, f.name, f.who, f.viewable) for f in files], counter.count


def test_the_attachments_box_costs_the_same_however_many_files_there_are(db_session, storage):
    desk = create_team(db_session, "Service Desk")
    ticket = triaged(db_session, desk)
    me = make_agent(db_session, "agent@example.com", "Agent", desk)
    other = db_session.get(User, make_agent(db_session, "other@example.com", "Other", desk))
    mine = db_session.get(User, me)
    add_file(db_session, storage, ticket, "first.txt", mine)
    _, few = files_for(me, ticket)
    for number in range(15):
        direction = MessageDirection.INBOUND if number % 2 else MessageDirection.INTERNAL
        note = add_message(db_session, ticket, f"Sender {number} <s{number}@example.com>", direction)
        add_file(db_session, storage, ticket, f"m{number}.txt", None, note)
        add_file(db_session, storage, ticket, f"u{number}.txt", other if number % 2 else mine)
    files, many = files_for(me, ticket)
    assert len(files) == 31
    assert many == few <= 4


def test_the_box_lists_the_same_files_in_the_same_order_and_hides_a_deleted_notes_files(db_session, storage):
    desk = create_team(db_session, "Service Desk")
    ticket = triaged(db_session, desk)
    me = make_agent(db_session, "agent@example.com", "Agent Smith", desk)
    mine = db_session.get(User, me)
    old = add_file(db_session, storage, ticket, "old.txt", mine)
    mail = add_message(db_session, ticket, "Pat Requester <pat@example.com>", MessageDirection.INBOUND)
    from_mail = add_file(db_session, storage, ticket, "mail.txt", None, mail)
    gone_note = add_message(db_session, ticket, "agent@example.com", MessageDirection.INTERNAL, deleted=True)
    add_file(db_session, storage, ticket, "deleted-note.txt", mine, gone_note)
    orphan = add_file(db_session, storage, ticket, "nobody.txt")
    renamed = add_file(db_session, storage, ticket, "raw.png", mine)
    renamed.display_name = "Shown name.png"
    db_session.add(renamed)
    db_session.commit()
    files, _ = files_for(me, ticket)
    assert files == [
        (renamed.id, "Shown name.png", "Agent Smith", True),
        (orphan.id, "nobody.txt", "Unknown", False),
        (from_mail.id, "mail.txt", "Pat Requester <pat@example.com>", False),
        (old.id, "old.txt", "Agent Smith", False),
    ]


def test_an_agent_without_access_to_the_ticket_is_listed_no_files(db_session, storage):
    desk = create_team(db_session, "Service Desk")
    elsewhere = create_team(db_session, "Network")
    ticket = triaged(db_session, desk)
    inside = make_agent(db_session, "in@example.com", "In", desk)
    outside = make_agent(db_session, "out@example.com", "Out", elsewhere)
    add_file(db_session, storage, ticket, "a.txt", db_session.get(User, inside))
    assert len(files_for(inside, ticket)[0]) == 1
    assert files_for(outside, ticket)[0] == []
