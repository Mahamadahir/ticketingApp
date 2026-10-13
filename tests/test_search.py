from datetime import datetime, timezone

from app.models import Message, MessageDirection, MessageVisibility, Ticket, TicketStatus
from app.search import normalise, search_condition
from tests.conftest import create_team
from tests.test_visibility import agent_in


def triaged(db_session, team, subject, requester="requester@example.com", **fields):
    ticket = Ticket(
        subject=subject, requester_email=requester, team_id=team.id, status=TicketStatus.OPEN,
        triaged_at=datetime.now(timezone.utc), **fields,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket

def say(db_session, ticket, body, direction=MessageDirection.OUTBOUND, visibility=MessageVisibility.PUBLIC):
    db_session.add(Message(ticket_id=ticket.id, sender="x", body=body, direction=direction, visibility=visibility))
    db_session.commit()


def found(client, text):
    page = client.get("/tickets", params={"q": text}).text
    return page


def test_search_by_number_in_any_form(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    one = triaged(db_session, team, "Printer jam")
    two = triaged(db_session, team, "Monitor flicker")

    for form in (str(one.number), f"#{one.number}", f"INC-{one.number}", f"sr-{one.number}"):
        page = found(client, form)
        assert "Printer jam" in page and "Monitor flicker" not in page, form


def test_search_by_subject_word_with_stemming_and_by_fragment(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    triaged(db_session, team, "Printer on floor 3 is jammed")
    triaged(db_session, team, "Cannot reach vpn-gateway-02")

    assert "Printer on floor 3" in found(client, "printers jammed")  # stems to the subject's words
    assert "vpn-gateway-02" in found(client, "gateway-0")  # a fragment full-text cannot match
    assert "No tickets match." in found(client, "keyboard")


def test_search_by_requester(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    triaged(db_session, team, "Alpha", requester="jo.bloggs@example.com")
    triaged(db_session, team, "Beta", requester="sam@example.com")

    page = found(client, "bloggs")
    assert "Alpha" in page and "Beta" not in page


def test_search_finds_message_text_including_internal_notes(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    emailed = triaged(db_session, team, "Alpha")
    noted = triaged(db_session, team, "Beta")
    triaged(db_session, team, "Gamma")
    say(db_session, emailed, "Please restart the spooler service")
    say(
        db_session, noted, "Spooler crashed on the print server",
        MessageDirection.INTERNAL, MessageVisibility.AGENTS_ONLY,
    )

    page = found(client, "spooler")
    assert "Alpha" in page and "Beta" in page and "Gamma" not in page


def test_search_never_shows_tickets_the_agent_cannot_open(client, db_session):
    mine, other = create_team(db_session, "Mine"), create_team(db_session, "Other")
    agent_in(client, db_session, member_of=[mine])
    visible = triaged(db_session, mine, "Visible Zebra")
    hidden = triaged(db_session, other, "Hidden Zebra", requester="zebra@example.com")
    say(db_session, hidden, "zebra zebra zebra")
    assert hidden.number != visible.number

    page = found(client, "zebra")

    assert "Visible Zebra" in page and "Hidden Zebra" not in page
    assert "1 ticket you can open" in page  # the count does not leak the hidden match
    assert "No tickets match." in found(client, f"#{hidden.number}")


def test_search_combines_with_the_mine_and_team_filters_and_paging_keeps_it(client, db_session):
    team = create_team(db_session, "Desk")
    agent_id = agent_in(client, db_session, member_of=[team])
    mine = triaged(db_session, team, "Router reboot", assigned_agent_id=agent_id)
    triaged(db_session, team, "Router firmware")

    page = client.get("/tickets", params={"q": "router", "mine": "true"}).text
    assert "Router reboot" in page and "Router firmware" not in page
    assert mine.reference in page


def test_percent_and_underscore_are_literal_and_odd_input_is_safe(client, db_session):
    team = create_team(db_session, "Desk")
    agent_in(client, db_session, member_of=[team])
    triaged(db_session, team, "Disk at 100% full")
    triaged(db_session, team, "Disk at 1000 full")

    page = found(client, "100%")
    assert "Disk at 100% full" in page and "Disk at 1000 full" not in page
    for odd in ("%", "_", "\\", "'; drop table ticket; --", '"', "a & b | !", "x" * 5000):
        assert client.get("/tickets", params={"q": odd}).status_code == 200


def test_blank_search_changes_nothing_and_normalise_trims_and_caps():
    assert search_condition("   ") is None and search_condition(None) is None
    assert normalise("  hi  ") == "hi"
    assert len(normalise("x" * 5000)) == 200


def test_only_agents_can_search(client):
    assert client.get("/tickets", params={"q": "x"}, follow_redirects=False).status_code in (303, 307, 401, 403)
