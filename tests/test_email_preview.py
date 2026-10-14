import pytest
from sqlmodel import select

from app.messages import preview_email
from app.models import AuditLog, Message, Ticket, TicketEvent, TicketStatus, UserRole
from app.raising import should_tell_requester
from app.ticket_service import NotTriaged
from tests.conftest import create_category, create_team, login_as, set_role
from tests.test_raising import desk, raise_it
from tests.test_splitting import split, subs_of


def ticket_for_email(client, db_session, subject="Printer jam"):
    team, category, agent_id = desk(client, db_session)
    raise_it(
        client, subject=subject, for_email="walkin@example.com", category_id=category.id,
        impact="low", urgency="low",
    )
    db_session.expire_all()
    return db_session.exec(select(Ticket).where(Ticket.subject == subject)).one()


def follow(client, ticket, *emails):
    for email in emails:
        assert client.post(f"/tickets/{ticket.number}/followers", data={"email": email}).status_code == 200


def preview(client, ticket, body="Please restart it", **extra):
    return client.post(f"/tickets/{ticket.number}/email/preview", data={"body": body, **extra})


def recorded(db_session):
    db_session.expire_all()
    return (
        len(db_session.exec(select(Message)).all()),
        len(db_session.exec(select(AuditLog)).all()),
        len(db_session.exec(select(TicketEvent)).all()),
    )


# ---- The step ---------------------------------------------------------------------------------------


def test_the_page_previews_before_it_sends(client, db_session):
    ticket = ticket_for_email(client, db_session)
    page = client.get(f"/tickets/{ticket.number}").text
    assert f'hx-post="/tickets/{ticket.number}/email/preview"' in page and "Preview email" in page
    assert f'hx-post="/tickets/{ticket.number}/email"' not in page  # nothing on the page sends directly


def test_a_preview_shows_who_gets_it_and_what_it_says_and_records_nothing(client, db_session):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")
    before = recorded(db_session)

    response = preview(client, ticket, body="Please restart it\nthen tell me")

    assert response.status_code == 200
    page = response.text
    assert "To:</b> walkin@example.com" in page and "boss@example.com" in page
    assert "Subject:</b> Printer jam" in page and "Please restart it\nthen tell me" in page
    assert "moves this ticket to awaiting user" in page
    assert recorded(db_session) == before
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN  # not moved by a preview


def test_the_ticked_copies_are_previewed_and_carried_to_the_send(client, db_session):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com", "other@example.com")

    page = preview(client, ticket, cc_form="1", cc=["boss@example.com"]).text
    assert "<b>Copied:</b> boss@example.com" in page and "other@example.com" not in page
    assert 'name="cc" value="boss@example.com"' in page and 'name="cc_form"' in page

    nobody = preview(client, ticket, cc_form="1").text
    assert "<b>Copied:</b> no one" in nobody

    everyone = preview(client, ticket).text  # no cc form at all: every follower, like the send
    assert "boss@example.com" in everyone and "other@example.com" in everyone


def test_an_internal_follower_is_always_shown_as_copied(client, db_session):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com")
    # Escalating makes the escalating agent an internal follower, who is always copied.
    assert client.post(f"/tickets/{ticket.number}/escalate", data={"reason": "Needs a second pair of eyes"}).status_code == 200

    page = preview(client, ticket, cc_form="1").text  # nothing ticked

    assert "agent@example.com" in page.split("Copied:")[1].split("</p>")[0]
    assert "boss@example.com" not in page.split("Copied:")[1].split("</p>")[0]


def test_what_is_previewed_is_what_is_sent(client, db_session):
    ticket = ticket_for_email(client, db_session)
    follow(client, ticket, "boss@example.com", "other@example.com")
    shown = preview(client, ticket, cc_form="1", cc=["other@example.com"]).text
    assert "other@example.com" in shown and "boss@example.com" not in shown

    client.post(
        f"/tickets/{ticket.number}/email",
        data={"body": "Please restart it", "cc_form": "1", "cc": ["other@example.com"]},
    )

    [email] = db_session.exec(select(Message).where(Message.sender == "Agent Ann")).all()
    assert email.cc == "other@example.com"


def test_the_preview_applies_the_sends_own_checks(client, db_session):
    ticket = ticket_for_email(client, db_session)
    assert preview(client, ticket, body="   ").status_code == 400
    assert preview(client, ticket, cc_form="1", cc=["stranger@example.com"]).status_code == 400

    raise_it(client, subject="Not triaged yet", for_email="walkin@example.com")
    untriaged = db_session.exec(select(Ticket).where(Ticket.subject == "Not triaged yet")).one()
    assert preview(client, untriaged).status_code == 403  # read-only until claimed, like the send
    with pytest.raises(NotTriaged):  # and for someone who does work it, the same refusal as the send
        preview_email(db_session, untriaged, "hello")


def test_the_preview_follows_ticket_access(client, db_session):
    ticket = ticket_for_email(client, db_session)
    client.post("/auth/logout")
    other = create_team(db_session, "Network")
    outsider = login_as(client, "outsider@example.com")
    set_role(db_session, outsider, UserRole.AGENT, other.id)
    assert preview(client, ticket).status_code == 403

    client.post("/auth/logout")
    login_as(client, "jo@example.com")
    assert preview(client, ticket).status_code == 403


def test_the_note_says_when_the_status_will_not_change(client, db_session):
    ticket = ticket_for_email(client, db_session)
    client.post(f"/tickets/{ticket.number}/status", data={"status": "resolved"})
    assert "leaves the ticket's status as it is" in preview(client, ticket).text


def test_a_split_originals_status_is_not_moved_and_a_part_is_about_itself(client, db_session):
    original = ticket_for_email(client, db_session, "Two things")
    split(client, original, ("Keyboard", ""), ("Office", ""))
    part = subs_of(db_session, original)[0]

    assert "leaves the ticket's status as it is" in preview(client, original).text
    from_part = preview(client, part).text
    assert "moves this ticket to awaiting user" in from_part
    assert "Subject:</b> Two things" in from_part  # the family's one thread subject
    assert f"About: {part.reference} Keyboard" in from_part


def test_a_leading_blank_line_survives_the_trip_to_the_send(client, db_session):
    ticket = ticket_for_email(client, db_session)
    page = preview(client, ticket, body="\n\nHello").text
    # A browser drops one newline straight after <textarea>, so the template adds one.
    assert '<textarea name="body" hidden>\n\n\nHello</textarea>' in page


def test_editing_the_form_clears_the_preview(client, db_session):
    ticket = ticket_for_email(client, db_session)
    page = client.get(f"/tickets/{ticket.number}").text
    clear = "document.getElementById('email-preview').innerHTML = ''"
    assert f'hx-on:input="{clear}"' in page  # typing, ticking a copy, anything in the form
    assert page.count(clear) == 1  # (the canned-response picker adds its own once any exist)


def test_a_blank_email_is_refused_by_the_send_too(client, db_session):
    ticket = ticket_for_email(client, db_session)
    before = recorded(db_session)

    assert client.post(f"/tickets/{ticket.number}/email", data={"body": "  \n "}).status_code == 400

    assert recorded(db_session) == before
    db_session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN


def test_the_text_is_escaped(client, db_session):
    ticket = ticket_for_email(client, db_session)
    page = preview(client, ticket, body="<script>alert(1)</script>").text
    assert "<script>" not in page and page.count("&lt;script&gt;") == 2  # shown, and carried to the send


# ---- The "tell the requester" setting -------------------------------------------------------------------


def test_the_tell_the_requester_setting_defaults_on_and_is_changed_in_settings(client, db_session):
    key = "tell_requester_when_raised_for_them"
    ticket_for_email(client, db_session)
    assert should_tell_requester(db_session) is True
    assert "set to be told by email" in client.get("/portal/new").text

    client.post("/auth/logout")
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)
    assert client.post("/admin/settings", data={"key": key, "value": "maybe"}).status_code == 400
    assert client.post("/admin/settings", data={"key": key, "value": "off"}, follow_redirects=False).status_code == 303

    db_session.expire_all()
    assert should_tell_requester(db_session) is False
    assert "They will not be emailed" in client.get("/portal/new").text
    assert "Tell the requester when an agent raises" in client.get("/admin/settings").text
    entry = db_session.exec(select(AuditLog).where(AuditLog.entity_type == "setting")).all()[-1]
    assert key in entry.detail and "'off'" in entry.detail
