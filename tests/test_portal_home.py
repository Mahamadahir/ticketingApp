"""The portal home (9D): the requester's status wording, the last-update line, the quick-start tiles, the Open and
Finished tabs and the portal frame. The wording lives in app/messages.py and is shared with the other portal pages."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.approval_groups import add_member, create_group
from app.config import settings
from app.messages import (
    REQUESTER_STATUS_WORDING,
    reopen_deadline,
    requester_kind,
    requester_last_update,
    requester_status,
)
from app.models import (
    LinkedIdentity,
    Message,
    MessageDirection,
    MessageVisibility,
    RequestType,
    Ticket,
    TicketFollower,
    TicketStatus,
    TicketType,
    User,
    UserRole,
)
from app.request_types import create_request_type, portal_tiles
from app.settings_store import AUTO_CLOSE_AFTER_DAYS, AUTO_CLOSE_ENABLED, set_setting
from app.time_zones import format_long_date, relative_time
from tests.conftest import login_as, set_role
from tests.test_approvals import answer, person, raised_and_triaged, request_type as approval_type

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def make_ticket(db, email="jo@example.com", subject="Printer is offline", status=TicketStatus.OPEN, **fields):
    ticket = Ticket(subject=subject, requester_email=email, status=status, **fields)
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def say(db, ticket, body, direction, visibility=MessageVisibility.PUBLIC, sender="agent@example.com", ago=timedelta(hours=2), author=None):
    message = Message(
        ticket_id=ticket.id, sender=sender, body=body, direction=direction, visibility=visibility,
        created_at=datetime.now(timezone.utc) - ago, author_user_id=author,
    )
    db.add(message)
    db.commit()
    return message


def kinds(db, *names, featured=(), inactive=()):
    made = []
    for name in names:
        created = create_request_type(
            db, name, f"About {name}", None, 51, is_active=False, is_featured=name in featured
        )
        created.is_active = name not in inactive
        db.add(created)
        made.append(created)
    db.commit()
    return made


# ---- the status wording ---------------------------------------------------------------------------------------


def test_the_wording_covers_every_status():
    assert set(REQUESTER_STATUS_WORDING) == set(TicketStatus)


@pytest.mark.parametrize(
    "status,label,tone",
    [
        (TicketStatus.NEW, "Received", "gray"),
        (TicketStatus.OPEN, "Being worked on", "blue"),
        (TicketStatus.IN_PROGRESS, "Being worked on", "blue"),
        (TicketStatus.AWAITING_USER, "Waiting for your reply", "amber"),
        (TicketStatus.AWAITING_APPROVAL, "Waiting for approval", "blue"),
        (TicketStatus.WITH_SUPPLIER, "Being worked on", "blue"),
        (TicketStatus.WITH_MSP, "Being worked on", "blue"),
        (TicketStatus.SCHEDULED, "Scheduled", "gray"),
        (TicketStatus.RESOLVED, "Resolved", "green"),
        (TicketStatus.CLOSED, "Closed", "gray"),
    ],
)
def test_each_status_in_the_requesters_words(db_session, status, label, tone):
    ticket = make_ticket(db_session, status=status)
    wording = requester_status(db_session, ticket, "UTC")
    assert (wording.label, wording.tone) == (label, tone)
    assert wording.sentence == label  # no date to add without a Due date or a resolution time


def test_the_requester_is_never_told_about_the_partner(db_session):
    for status in (TicketStatus.WITH_MSP, TicketStatus.WITH_SUPPLIER):
        words = requester_status(db_session, make_ticket(db_session, status=status), "UTC").sentence.lower()
        assert "msp" not in words and "supplier" not in words and "partner" not in words


def test_scheduled_says_for_which_day(db_session):
    ticket = make_ticket(db_session, status=TicketStatus.SCHEDULED, due_on=date(2026, 11, 12))
    assert requester_status(db_session, ticket, "UTC").sentence == "Scheduled for Thursday 12 November"
    assert requester_last_update(db_session, ticket, "UTC", set(), now=NOW) == "Scheduled for Thursday 12 November"
    far = make_ticket(db_session, status=TicketStatus.SCHEDULED, due_on=date(2027, 1, 4))
    assert requester_status(db_session, far, "UTC").sentence == "Scheduled for Monday 4 January 2027"


def test_resolved_says_until_when_it_can_be_reopened_and_only_to_the_requester(db_session):
    set_setting(db_session, AUTO_CLOSE_ENABLED, "on")
    set_setting(db_session, AUTO_CLOSE_AFTER_DAYS, "7")
    resolved = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    ticket = make_ticket(db_session, status=TicketStatus.RESOLVED, resolved_at=resolved)
    assert reopen_deadline(db_session, ticket) == resolved + timedelta(days=7)
    assert requester_status(db_session, ticket, "UTC", can_reopen=True).sentence == "Resolved · you can reopen it until Tuesday 10 November"
    assert requester_status(db_session, ticket, "UTC", can_reopen=False).sentence == "Resolved"  # a follower cannot reopen
    assert (
        requester_last_update(db_session, ticket, "UTC", set(), can_reopen=True, now=NOW)
        == "Resolved on Tuesday 3 November · you can reopen it until Tuesday 10 November"
    )


def test_resolved_without_auto_close_has_no_date(db_session):
    set_setting(db_session, AUTO_CLOSE_ENABLED, "off")
    ticket = make_ticket(db_session, status=TicketStatus.RESOLVED, resolved_at=NOW)
    assert reopen_deadline(db_session, ticket) is None
    assert requester_status(db_session, ticket, "UTC", can_reopen=True).sentence == "Resolved · you can reopen it"


def test_the_dates_follow_the_viewers_time_zone(db_session):
    ticket = make_ticket(db_session, status=TicketStatus.RESOLVED, resolved_at=datetime(2026, 11, 3, 23, 30, tzinfo=timezone.utc))
    assert "Tuesday 3 November" in requester_last_update(db_session, ticket, "UTC", set(), now=NOW)
    assert "Wednesday 4 November" in requester_last_update(db_session, ticket, "Pacific/Auckland", set(), now=NOW)


def test_closed_says_when(db_session):
    ticket = make_ticket(db_session, status=TicketStatus.CLOSED, closed_at=datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc))
    assert requester_last_update(db_session, ticket, "UTC", set(), now=NOW) == "Closed on Tuesday 27 October"


def test_the_kind_of_request_in_the_requesters_words(db_session):
    assert requester_kind(make_ticket(db_session), None) == "Problem report"
    assert requester_kind(make_ticket(db_session, type=TicketType.MAJOR_INCIDENT), None) == "Problem report"
    assert requester_kind(make_ticket(db_session, type=TicketType.SERVICE_REQUEST), None) == "Service request"
    assert requester_kind(make_ticket(db_session, type=TicketType.SERVICE_REQUEST), "Equipment") == "Equipment"


def test_relative_time_in_words():
    assert relative_time(NOW - timedelta(seconds=20), NOW) == "just now"
    assert relative_time(NOW - timedelta(minutes=1), NOW) == "1 minute ago"
    assert relative_time(NOW - timedelta(hours=2, minutes=5), NOW) == "2 hours ago"
    assert relative_time(NOW - timedelta(days=3), NOW) == "3 days ago"
    assert relative_time(NOW - timedelta(days=15), NOW) == "2 weeks ago"
    assert relative_time(NOW - timedelta(days=65), NOW) == "2 months ago"
    assert relative_time(NOW - timedelta(days=800), NOW) == "2 years ago"
    assert format_long_date(date(2026, 11, 12), "UTC", now=NOW) == "Thursday 12 November"


# ---- the last-update line ---------------------------------------------------------------------------------------


def test_an_agents_reply_reads_as_it_support_replied(db_session):
    ticket = make_ticket(db_session)
    say(db_session, ticket, "Have you tried turning it off?", MessageDirection.OUTBOUND, ago=timedelta(hours=2, minutes=5))
    assert requester_last_update(db_session, ticket, "UTC", {"jo@example.com"}) == "IT Support replied 2 hours ago"


def test_the_requesters_own_reply_reads_as_you_replied_and_a_followers_does_not(db_session):
    ticket = make_ticket(db_session)
    say(db_session, ticket, "The request itself", MessageDirection.INBOUND, sender="jo@example.com", ago=timedelta(hours=1))
    say(db_session, ticket, "Still broken", MessageDirection.INBOUND, sender="jo@example.com", ago=timedelta(minutes=10))
    assert requester_last_update(db_session, ticket, "UTC", {"jo@example.com"}) == "You replied 10 minutes ago"
    say(db_session, ticket, "Me too", MessageDirection.INBOUND, sender="pat@example.com", ago=timedelta(minutes=3))
    assert requester_last_update(db_session, ticket, "UTC", {"jo@example.com"}) == "A reply was added 3 minutes ago"


def test_a_portal_reply_is_the_viewers_own_even_though_it_is_signed_with_the_display_name(db_session):
    """The portal records a comment with the person's display name as sender, so the author is what says whose it is."""
    jo = User(email="jo@example.com", display_name="Jo Bloggs")
    db_session.add(jo)
    db_session.commit()
    ticket = make_ticket(db_session)
    say(db_session, ticket, "The request itself", MessageDirection.INBOUND, sender="Jo Bloggs", ago=timedelta(hours=1), author=jo.id)
    say(db_session, ticket, "Still broken", MessageDirection.INBOUND, sender="Jo Bloggs", ago=timedelta(minutes=5), author=jo.id)
    assert requester_last_update(db_session, ticket, "UTC", {"jo@example.com"}, viewer_id=jo.id) == "You replied 5 minutes ago"
    assert requester_last_update(db_session, ticket, "UTC", {"pat@example.com"}, viewer_id=jo.id + 1) == "A reply was added 5 minutes ago"


def test_a_request_with_only_its_own_opening_message_is_just_received(db_session):
    ticket = make_ticket(db_session)
    say(db_session, ticket, "Printer is offline", MessageDirection.INBOUND, sender="jo@example.com", ago=timedelta(minutes=30))
    assert requester_last_update(db_session, ticket, "UTC", {"jo@example.com"}, now=ticket.created_at + timedelta(minutes=30)) == "Received 30 minutes ago"


def test_with_no_messages_the_line_says_when_it_was_received(db_session):
    ticket = make_ticket(db_session)
    assert requester_last_update(db_session, ticket, "UTC", set(), now=ticket.created_at + timedelta(hours=1)) == "Received 1 hour ago"


def test_the_line_never_uses_internal_notes_or_unforwarded_partner_mail(client, db_session):
    ticket = make_ticket(db_session, status=TicketStatus.WITH_MSP)
    say(db_session, ticket, "Chat with the customer", MessageDirection.OUTBOUND, ago=timedelta(days=2))
    say(db_session, ticket, "SECRET-NOTE the MSP is useless", MessageDirection.INTERNAL, MessageVisibility.AGENTS_ONLY, ago=timedelta(minutes=5))
    mail = say(db_session, ticket, "SECRET-MSP-MAIL", MessageDirection.OUTBOUND, MessageVisibility.AGENTS_ONLY, sender="desk@msp-partner.example", ago=timedelta(minutes=1))
    mail.msp_forward = "pending"
    db_session.add(mail)
    db_session.commit()
    assert requester_last_update(db_session, ticket, "UTC", {"jo@example.com"}) == "IT Support replied 2 days ago"
    login_as(client, "jo@example.com")
    page = client.get("/portal").text
    assert "IT Support replied 2 days ago" in page and "Being worked on" in page
    for secret in ("SECRET", "msp-partner", "MSP", "supplier"):
        assert secret not in page


def test_mail_from_the_partner_the_requester_was_copied_on_reads_as_it_support(client, db_session):
    ticket = make_ticket(db_session, status=TicketStatus.WITH_MSP)
    mail = say(db_session, ticket, "We have fixed it", MessageDirection.OUTBOUND, sender="desk@msp-partner.example", ago=timedelta(hours=3))
    mail.msp_forward = "not_needed"
    db_session.add(mail)
    db_session.commit()
    login_as(client, "jo@example.com")
    page = client.get("/portal").text
    assert "IT Support replied 3 hours ago" in page
    assert "msp-partner" not in page and "MSP" not in page


def test_an_approval_request_says_how_many_approvals_are_still_wanted(client, db_session):
    for email in ("boss@example.com", "lead@example.com"):
        person(client, db_session, email)
    kind = approval_type(db_session, ["boss@example.com", "lead@example.com"], pass_percent=100)
    ticket = raised_and_triaged(client, db_session, kind)
    assert ticket.status == TicketStatus.AWAITING_APPROVAL
    assert requester_last_update(db_session, ticket, "UTC", {"jo@example.com"}) == "Waiting for 2 of 2 approvals"
    assert answer(client, ticket, "boss@example.com", "approved").status_code < 400
    db_session.expire_all()
    line = requester_last_update(db_session, db_session.get(Ticket, ticket.id), "UTC", {"jo@example.com"})
    assert line == "Waiting for 1 of 2 approvals"
    assert "boss" not in line and "lead" not in line  # never who

    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    page = client.get("/portal").text
    assert "Waiting for 1 of 2 approvals" in page and "Waiting for approval" in page
    assert "boss@example.com" not in page


def test_a_single_approval_says_waiting_for_approval(client, db_session):
    person(client, db_session, "boss@example.com")
    kind = approval_type(db_session, ["boss@example.com"], pass_percent=100)
    ticket = raised_and_triaged(client, db_session, kind)
    assert requester_last_update(db_session, ticket, "UTC", {"jo@example.com"}) == "Waiting for approval"


# ---- the page ----------------------------------------------------------------------------------------------------


def test_the_greeting_uses_the_first_name_and_teams_only_when_the_bot_is_on(client, db_session, monkeypatch):
    login_as(client, "daniel@example.com", "Daniel Okafor")
    page = client.get("/portal").text
    assert "Hi Daniel, how can we help?" in page and "Okafor, how" not in page
    assert "email IT Support" in page and "Teams" not in page
    monkeypatch.setattr(settings, "teams_bot_enabled", True)
    monkeypatch.setattr(settings, "teams_bot_app_id", "app")
    monkeypatch.setattr(settings, "teams_bot_tenant_id", "tenant")
    assert "message the IT Support app in Teams" in client.get("/portal").text


def test_the_tiles_come_from_the_request_types_the_new_request_page_offers(client, db_session):
    kinds(db_session, "Alpha", "Bravo", "Charlie", "Delta", "Echo", inactive=("Alpha",))
    login_as(client, "jo@example.com")
    page = client.get("/portal").text
    # none featured: the first four active ones in the catalogue's order; an inactive kind never shows
    assert [name for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo") if f'<span class="tile-title">{name}</span>' in page] == [
        "Bravo", "Charlie", "Delta", "Echo",
    ]
    offered = client.get("/portal/new").text
    for name in ("Bravo", "Charlie", "Delta", "Echo"):
        assert f">{name} - About {name}</option>" in offered
    bravo = db_session.exec(select(RequestType).where(RequestType.name == "Bravo")).one()
    assert f'href="/portal/new?request_type_id={bravo.id}"' in page


def test_featured_kinds_come_first_and_are_capped_at_four(db_session):
    kinds(db_session, "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", featured=("Echo", "Foxtrot"))
    assert [t.name for t in portal_tiles(db_session)] == ["Echo", "Foxtrot"]
    kinds(db_session, "Golf", "Hotel", "India", featured=("Golf", "Hotel", "India"))
    assert [t.name for t in portal_tiles(db_session)] == ["Echo", "Foxtrot", "Golf", "Hotel"]


def test_no_tiles_when_there_are_no_request_types(client, db_session):
    login_as(client, "jo@example.com")
    assert 'class="tiles"' not in client.get("/portal").text


def test_a_tile_opens_new_request_with_that_kind_chosen(client, db_session):
    first, second = kinds(db_session, "Alpha", "Bravo")
    login_as(client, "jo@example.com")
    page = client.get(f"/portal/new?request_type_id={second.id}").text
    assert f'<option value="{second.id}" selected>' in page and f'<option value="{first.id}" selected>' not in page
    assert "selected" not in client.get("/portal/new").text
    assert client.get("/portal/new?request_type_id=99999").status_code == 200  # an unknown kind is ignored
    assert client.get("/portal/new?request_type_id=abc").status_code == 400  # as the other portal forms read a bad id


def test_an_admin_features_a_kind_from_the_request_type_form(client, db_session):
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    group = create_group(db_session, "Approvers")
    db_session.add(User(email="boss@example.com", display_name="Boss"))
    db_session.flush()
    add_member(db_session, group, "boss@example.com")
    db_session.commit()
    response = client.post(
        "/admin/request-types",
        data={"name": "Equipment", "approval_group_id": group.id, "pass_percent": 51, "is_active": "on", "is_featured": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    kind = db_session.exec(select(RequestType)).one()
    assert kind.is_featured
    client.post(
        f"/admin/request-types/{kind.id}",
        data={"name": "Equipment", "approval_group_id": group.id, "pass_percent": 51, "is_active": "on"},
        follow_redirects=False,
    )
    db_session.refresh(kind)
    assert not kind.is_featured


def test_open_and_finished_tabs(client, db_session):
    make_ticket(db_session, subject="Open one", status=TicketStatus.OPEN)
    make_ticket(db_session, subject="Waiting one", status=TicketStatus.AWAITING_USER)
    make_ticket(db_session, subject="Resolved one", status=TicketStatus.RESOLVED, resolved_at=datetime.now(timezone.utc))
    make_ticket(db_session, subject="Closed one", status=TicketStatus.CLOSED, closed_at=datetime.now(timezone.utc))
    make_ticket(db_session, email="other@example.com", subject="Someone elses", status=TicketStatus.OPEN)
    login_as(client, "jo@example.com")

    opened = client.get("/portal").text
    assert "Open (2)" in opened and "Finished (2)" in opened
    assert "Open one" in opened and "Waiting one" in opened and "Resolved one" not in opened and "Closed one" not in opened
    assert "Someone elses" not in opened
    assert 'aria-current="page">Open (2)' in opened

    finished = client.get("/portal?tab=finished").text
    assert "Resolved one" in finished and "Closed one" in finished and "Open one" not in finished
    assert 'aria-current="page">Finished (2)' in finished
    assert "Reopen" in finished  # the resolved request is reopenable from the list
    assert client.get("/portal?tab=nonsense").text.count("Open one") == 1  # an unknown tab is the Open tab


def test_each_row_has_the_reference_kind_subject_last_update_and_chip(client, db_session):
    ticket = make_ticket(db_session, subject="Outlook keeps asking", status=TicketStatus.AWAITING_USER)
    say(db_session, ticket, "Which version?", MessageDirection.OUTBOUND, ago=timedelta(hours=2, minutes=1))
    login_as(client, "jo@example.com")
    page = client.get("/portal").text
    assert f"{ticket.reference} &middot; Problem report" in page
    assert "Outlook keeps asking" in page and "IT Support replied 2 hours ago" in page
    assert '<span class="chip t-amber">Waiting for your reply</span>' in page
    assert f'href="/portal/{ticket.number}"' in page


def test_linked_addresses_and_followed_requests_are_listed(client, db_session):
    main_id = login_as(client, "jo@example.com")
    db_session.add(LinkedIdentity(main_user_id=main_id, email="jo@other.example", method="email_code", status="active"))
    db_session.commit()
    make_ticket(db_session, email="jo@other.example", subject="From the other address")
    followed = make_ticket(db_session, email="boss@example.com", subject="Raised by my boss")
    db_session.add(TicketFollower(ticket_id=followed.id, email="jo@example.com", added_by_email="boss@example.com"))
    resolved = make_ticket(db_session, email="boss@example.com", subject="Boss resolved", status=TicketStatus.RESOLVED, resolved_at=datetime.now(timezone.utc))
    db_session.add(TicketFollower(ticket_id=resolved.id, email="jo@example.com", added_by_email="boss@example.com"))
    db_session.commit()

    page = client.get("/portal").text
    assert "From the other address" in page and "Raised by my boss" in page
    assert "(following)" in page and "Requests marked as following" in page
    finished = client.get("/portal?tab=finished").text
    assert "Boss resolved" in finished and f"/portal/{resolved.number}/reopen" not in finished  # a follower cannot reopen
    assert "you can reopen it" not in finished


def test_the_empty_states(client, db_session):
    login_as(client, "jo@example.com")
    assert "No open requests right now" in client.get("/portal").text
    assert "Nothing finished yet" in client.get("/portal?tab=finished").text


def test_the_portal_frame_menu_closes_on_escape_and_outside_click(client, db_session):
    login_as(client, "jo@example.com")
    page = client.get("/portal").text
    assert 'querySelector(".person-menu")' in page and "Escape" in page


def test_the_portal_frame(client, db_session):
    login_as(client, "jo@example.com", "Jo Bloggs")
    page = client.get("/portal").text
    assert '<span class="brand-name">IT Support</span>' in page
    for link in ("My requests", "New request", "Approvals"):
        assert link in page
    assert "Agent console" not in page
    assert 'aria-label="Account menu for Jo Bloggs"' in page and ">JB<" in page
    assert 'action="/auth/logout"' in page and 'href="/settings/notifications"' in page
    assert 'name="theme"' in page and "Notifications" in page


def test_an_agent_on_the_portal_keeps_the_agent_console_link(client, db_session):
    set_role(db_session, login_as(client, "agent@example.com", "Ann Agent"), UserRole.AGENT)
    page = client.get("/portal").text
    assert 'href="/tickets"' in page and "Agent console" in page
