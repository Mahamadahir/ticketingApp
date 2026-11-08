"""6E (#165): linked identities. One person, two identities: an admin links two accounts and chooses the main one;
a person links a second identity themselves by signing in with it or by an emailed code; an old address after an
Entra email change becomes an alias."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import linked_identities as links
from app.identity import people_for_addresses, person_for_address, requester_emails, resolve_sso_user, SsoRefused
from app.models import (
    ApprovalVote,
    AuditLog,
    LinkCode,
    LinkedIdentity,
    NeverFollowAddress,
    Notification,
    TicketEvent,
    TicketFollower,
    TicketSource,
    User,
    UserRole,
    UserSession,
)
from app.settings_store import LINK_CODE_ATTEMPTS, LINK_CODE_PER_ADDRESS_HOUR, LINK_CODE_PER_USER_HOUR, set_setting
from app.ticket_service import create_ticket
from tests.conftest import login_as
from tests.test_approval_inactive import super_admin
from tests.test_approvals import answer, approvals, person, raised_and_triaged, request_type

OID_A = "aaaaaaaa-0000-0000-0000-000000000001"
OID_B = "bbbbbbbb-0000-0000-0000-000000000002"


def mk(db, email, role=UserRole.END_USER, oid=None, name=None):
    user = User(email=email, display_name=name or email.split("@")[0], role=role, entra_object_id=oid)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def ticket_for(db, requester, subject="Printer", assigned_to=None):
    ticket = create_ticket(db, subject=subject, requester_email=requester, source=TicketSource.EMAIL)
    if assigned_to is not None:
        ticket.assigned_agent_id = assigned_to.id
        db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def audit_actions(db):
    db.expire_all()
    return [row.action for row in db.exec(select(AuditLog).order_by(AuditLog.id)).all()]


@pytest.fixture
def captured(monkeypatch):
    """Every email the code flow sends, as (to, body); nothing leaves the machine."""
    sent = []
    import app.mail as mail

    monkeypatch.setattr(mail, "send_now", lambda m, send=None: sent.append(m))
    return sent


def code_in(sent):
    return re.search(r"\b(\d{6})\b", sent[-1].body).group(1)


# ---- the schema list --------------------------------------------------------------------------------------------


def test_every_column_that_names_a_user_is_classified():
    """The lesson of an enumerated list: a new user column must be decided (moved or kept) before this passes."""
    assert links.user_foreign_keys() == links.classified_keys()


# ---- identity resolution ---------------------------------------------------------------------------------------


def test_requester_emails_and_my_requests_include_a_linked_address(client, db_session):
    main = mk(db_session, "ann@a.example", oid=OID_A)
    other = mk(db_session, "ann@b.example", oid=OID_B)
    under_alias = ticket_for(db_session, "ann@b.example", "Raised as B")
    under_main = ticket_for(db_session, "ann@a.example", "Raised as A")
    admin = mk(db_session, "root@a.example", UserRole.SUPER_ADMIN)
    assert requester_emails(main, db_session) == {"ann@a.example"}

    links.link_accounts(db_session, main=main, secondary=other, admin=admin)
    db_session.commit()

    assert requester_emails(main, db_session) == {"ann@a.example", "ann@b.example"}
    assert person_for_address(db_session, "Ann@B.example").id == main.id
    assert [p.id for p in people_for_addresses(db_session, ["ann@b.example", "ann@a.example"])] == [main.id]
    login_as(client, "ann@a.example")
    page = client.get("/portal").text
    assert "Raised as B" in page and "Raised as A" in page
    assert client.get(f"/portal/{under_alias.number}").status_code == 200  # the portal treats the alias as them
    assert under_main.number


def test_followers_on_a_linked_address_are_the_main_person(client, db_session):
    main = mk(db_session, "ann@a.example")
    other = mk(db_session, "ann@b.example")
    ticket = ticket_for(db_session, "someone@x.example", "Shared")
    db_session.add(TicketFollower(ticket_id=ticket.id, email="ann@b.example", added_by_email="someone@x.example"))
    db_session.commit()
    links.link_accounts(db_session, main=main, secondary=other, admin=mk(db_session, "root@a.example", UserRole.SUPER_ADMIN))
    db_session.commit()
    login_as(client, "ann@a.example")
    assert "Shared" in client.get("/portal").text


def test_you_cannot_approve_a_request_raised_under_your_linked_address(client, db_session):
    from app.approvals import current_approval, eligible_voters

    person(client, db_session, "jo@example.com")
    person(client, db_session, "jo2@example.com")
    mk(db_session, "boss@example.com")
    kind = request_type(db_session, ["boss@example.com", "jo2@example.com"], pass_percent=100)
    ticket = raised_and_triaged(client, db_session, kind)  # raised by jo@example.com, who is not an approver
    assert approvals(db_session, ticket)["jo2@example.com"] == "pending"

    jo = db_session.exec(select(User).where(User.email == "jo@example.com")).one()
    jo2 = db_session.exec(select(User).where(User.email == "jo2@example.com")).one()
    admin = mk(db_session, "root@example.com", UserRole.SUPER_ADMIN)
    links.link_accounts(db_session, main=jo2, secondary=jo, admin=admin)  # jo's address is now an address of the approver jo2
    db_session.commit()

    # Voters chosen from now on leave the approver out, because the request is theirs.
    assert [u.email for u in eligible_voters(db_session, ticket)] == ["boss@example.com"]
    # And a vote already waiting for them is refused: you cannot approve your own request.
    answer(client, ticket, "jo2@example.com", "approved")
    db_session.expire_all()
    assert approvals(db_session, ticket)["jo2@example.com"] == "pending"
    assert current_approval(db_session, ticket).status == "pending"


# ---- admin linking ---------------------------------------------------------------------------------------------


def test_admin_link_moves_tickets_and_records_retires_the_other_account_and_signs_in_as_main(client, db_session):
    admin = super_admin(client, db_session)
    main = mk(db_session, "ann@a.example", UserRole.AGENT, oid=OID_A)
    other = mk(db_session, "ann@b.example", UserRole.AGENT, oid=OID_B)
    mine = ticket_for(db_session, "x@x.example", "Mine", assigned_to=main)
    theirs = ticket_for(db_session, "y@x.example", "Theirs", assigned_to=other)
    db_session.add(TicketFollower(ticket_id=mine.id, email="ann@b.example", user_id=other.id, added_by_email="x@x.example"))
    db_session.commit()
    login_as(client, "ann@b.example")  # a live session of the account being merged away
    client.post("/auth/logout")
    other_client = type(client)(client.app)
    login_as(other_client, "ann@b.example")
    assert db_session.exec(select(UserSession).where(UserSession.user_id == other.id, UserSession.revoked_at.is_(None))).first()

    login_as(client, admin.email)
    preview = client.get("/admin/links/preview", params={"main_email": "ann@a.example", "other_email": "ann@b.example"})
    assert preview.status_code == 200
    assert "ann@a.example" in preview.text and "agent" in preview.text and "Open tickets assigned" in preview.text
    assert client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id}, follow_redirects=False).status_code == 303

    db_session.expire_all()
    assert db_session.get(type(theirs), theirs.id).assigned_agent_id == main.id
    events = db_session.exec(select(TicketEvent).where(TicketEvent.ticket_id == theirs.id, TicketEvent.event_type == "assigned")).all()
    assert events and events[-1].from_value == "ann@b.example" and events[-1].to_value == "ann@a.example"
    assert db_session.exec(select(TicketFollower).where(TicketFollower.email == "ann@b.example")).one().user_id == main.id
    assert db_session.get(User, other.id).is_active is False
    assert db_session.exec(select(UserSession).where(UserSession.user_id == other.id, UserSession.revoked_at.is_(None))).first() is None
    assert other_client.get("/portal", follow_redirects=False).status_code != 200  # signed out
    link = db_session.exec(select(LinkedIdentity)).one()
    assert (link.status, link.method, link.approved_by_id) == ("active", "admin", admin.id)
    assert "1 assigned tickets" in link.moved_summary
    assert {"identity_linked", "identity_merged_away"} <= set(audit_actions(db_session))

    # Signing in as the second account (here by the dev login; Entra is tested below) is the main account.
    client.post("/auth/logout")
    login_as(client, "ann@b.example")
    assert client.get("/portal", follow_redirects=False).status_code == 200
    row = db_session.exec(select(UserSession).order_by(UserSession.created_at.desc())).first()
    assert row.user_id == main.id


def test_the_admin_chooses_the_main_account_either_way_round(client, db_session):
    admin = super_admin(client, db_session)
    first, second = mk(db_session, "one@a.example"), mk(db_session, "two@b.example")
    login_as(client, admin.email)
    client.post("/admin/links", data={"main_id": second.id, "secondary_id": first.id})
    db_session.expire_all()
    link = db_session.exec(select(LinkedIdentity)).one()
    assert link.main_user_id == second.id and link.secondary_user_id == first.id
    assert requester_emails(db_session.get(User, second.id), db_session) == {"two@b.example", "one@a.example"}


def test_the_other_accounts_teams_and_permissions_are_not_merged_but_are_shown(client, db_session):
    from tests.conftest import add_membership, create_team
    from app.models import UserPermission

    admin = super_admin(client, db_session)
    main = mk(db_session, "ann@a.example", UserRole.AGENT)
    other = mk(db_session, "ann@b.example", UserRole.AGENT)
    team = create_team(db_session, "Network")
    add_membership(db_session, other.id, team.id)
    db_session.add(UserPermission(user_id=other.id, permission="manage_request_templates"))
    db_session.commit()
    login_as(client, admin.email)
    page = client.get("/admin/links/preview", params={"main_email": main.email, "other_email": other.email}).text
    assert "Not carried over automatically" in page and "Network" in page and "manage_request_templates" in page
    client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id})
    db_session.expire_all()
    from app.models import TeamMembership

    assert db_session.exec(select(TeamMembership).where(TeamMembership.user_id == main.id)).first() is None
    assert db_session.exec(select(UserPermission).where(UserPermission.user_id == main.id)).first() is None


def test_only_a_super_admin_links_and_a_bad_pair_is_refused(client, db_session):
    a, b = mk(db_session, "a@a.example"), mk(db_session, "b@b.example")
    login_as(client, "plain@a.example")
    assert client.post("/admin/links", data={"main_id": a.id, "secondary_id": b.id}).status_code == 403
    admin = super_admin(client, db_session)
    login_as(client, admin.email)
    assert "same account" in client.post("/admin/links", data={"main_id": a.id, "secondary_id": a.id}, follow_redirects=True).text
    bg = mk(db_session, "bg@local.invalid", UserRole.SUPER_ADMIN)
    bg.is_break_glass = True
    db_session.add(bg)
    db_session.commit()
    assert "break-glass" in client.post("/admin/links", data={"main_id": a.id, "secondary_id": bg.id}, follow_redirects=True).text
    assert db_session.exec(select(LinkedIdentity)).first() is None


def test_unlink_restores_the_account_and_leaves_moved_tickets_where_they_are(client, db_session):
    admin = super_admin(client, db_session)
    main, other = mk(db_session, "ann@a.example", UserRole.AGENT), mk(db_session, "ann@b.example", UserRole.AGENT)
    theirs = ticket_for(db_session, "y@x.example", "Theirs", assigned_to=other)
    login_as(client, admin.email)
    client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id})
    link = db_session.exec(select(LinkedIdentity)).one()

    assert client.post(f"/admin/links/{link.id}/unlink", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(User, other.id).is_active is True
    assert db_session.get(type(theirs), theirs.id).assigned_agent_id == main.id
    assert db_session.get(LinkedIdentity, link.id).status == "unlinked"
    assert requester_emails(db_session.get(User, main.id), db_session) == {"ann@a.example"}
    assert "identity_unlinked" in audit_actions(db_session)
    client.post("/auth/logout")
    login_as(client, "ann@b.example")
    row = db_session.exec(select(UserSession).order_by(UserSession.created_at.desc())).first()
    assert row.user_id == other.id  # its own account again


def test_an_inactive_account_stays_inactive_after_unlinking(db_session):
    admin = mk(db_session, "root@a.example", UserRole.SUPER_ADMIN)
    main, other = mk(db_session, "a@a.example"), mk(db_session, "b@b.example")
    other.is_active = False
    db_session.add(other)
    db_session.commit()
    link = links.link_accounts(db_session, main=main, secondary=other, admin=admin)
    links.unlink(db_session, link, admin)
    db_session.commit()
    assert db_session.get(User, other.id).is_active is False


def test_a_pending_approval_vote_of_the_merged_away_account_is_dropped(client, db_session):
    person(client, db_session, "jo@example.com")
    person(client, db_session, "a1@example.com")
    person(client, db_session, "a2@example.com")
    kind = request_type(db_session, ["a1@example.com", "a2@example.com"], pass_percent=100)
    ticket = raised_and_triaged(client, db_session, kind)
    a1 = db_session.exec(select(User).where(User.email == "a1@example.com")).one()
    a2 = db_session.exec(select(User).where(User.email == "a2@example.com")).one()
    admin = mk(db_session, "root@example.com", UserRole.SUPER_ADMIN)
    links.link_accounts(db_session, main=a2, secondary=a1, admin=admin)
    db_session.commit()
    db_session.expire_all()
    votes = {db_session.get(User, v.voter_id).email: v.decision for v in db_session.exec(select(ApprovalVote)).all()}
    assert votes["a1@example.com"] == "not_needed" and votes["a2@example.com"] == "pending"
    assert ticket.number


def test_records_that_name_the_other_account_move_and_history_keeps_its_id(db_session):
    from app.models import Message, MessageDirection, MessageVisibility, Notification

    admin = mk(db_session, "root@a.example", UserRole.SUPER_ADMIN)
    main, other = mk(db_session, "a@a.example", UserRole.AGENT), mk(db_session, "b@b.example", UserRole.AGENT)
    ticket = ticket_for(db_session, "x@x.example")
    ticket.created_by_id = ticket.resolved_by_id = other.id
    db_session.add(ticket)
    db_session.add(Message(ticket_id=ticket.id, direction=MessageDirection.OUTBOUND, visibility=MessageVisibility.PUBLIC, sender="b@b.example", body="hi", author_user_id=other.id))
    db_session.add(Notification(user_id=other.id, kind="assigned", text="x", ticket_id=ticket.id))
    db_session.commit()
    db_session.add(AuditLog(entity_type="ticket", entity_id=ticket.id, actor_email="b@b.example", action="x"))
    db_session.commit()
    links.link_accounts(db_session, main=main, secondary=other, admin=admin)
    db_session.commit()
    db_session.expire_all()
    t = db_session.get(type(ticket), ticket.id)
    assert (t.created_by_id, t.resolved_by_id) == (main.id, main.id)
    assert db_session.exec(select(Message)).one().author_user_id == main.id
    assert db_session.exec(select(Notification)).one().user_id == main.id
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "x")).one().actor_email == "b@b.example"


def test_addresses_linked_to_the_merged_away_account_follow_to_the_main_one(db_session):
    admin = mk(db_session, "root@a.example", UserRole.SUPER_ADMIN)
    main, other = mk(db_session, "a@a.example"), mk(db_session, "b@b.example")
    third = mk(db_session, "c@c.example")
    links.link_accounts(db_session, main=other, secondary=third, admin=admin)  # c is an address of b
    links.link_accounts(db_session, main=main, secondary=other, admin=admin)
    db_session.commit()
    assert requester_emails(main, db_session) == {"a@a.example", "b@b.example", "c@c.example"}


# ---- Entra sign-in ---------------------------------------------------------------------------------------------


def test_entra_sign_in_with_the_second_accounts_oid_or_address_is_the_main_person(db_session):
    admin = mk(db_session, "root@a.example", UserRole.SUPER_ADMIN)
    main, other = mk(db_session, "ann@a.example", oid=OID_A), mk(db_session, "ann@b.example", oid=OID_B)
    links.link_accounts(db_session, main=main, secondary=other, admin=admin)
    db_session.commit()

    assert resolve_sso_user(db_session, OID_B, "ann@b.example", "Ann").id == main.id
    assert resolve_sso_user(db_session, OID_B.upper(), "renamed@b.example", "Ann").id == main.id  # by oid, not email
    db_session.expire_all()
    assert db_session.get(User, main.id).email == "ann@a.example"  # the main account is not rewritten from these claims
    assert resolve_sso_user(db_session, OID_A, "ann@a.example", "Ann").id == main.id


def test_an_address_linked_without_an_oid_takes_the_first_sign_in_and_refuses_another_directory_account(db_session):
    main = mk(db_session, "ann@a.example", oid=OID_A)
    db_session.add(LinkedIdentity(main_user_id=main.id, email="ann@b.example", method="email_code", status="active"))
    db_session.commit()
    assert resolve_sso_user(db_session, OID_B, "ann@b.example", "Ann").id == main.id
    link = db_session.exec(select(LinkedIdentity)).one()
    assert link.entra_object_id == OID_B
    with pytest.raises(SsoRefused) as refused:
        resolve_sso_user(db_session, "cccccccc-0000-0000-0000-000000000003", "ann@b.example", "Someone Else")
    assert refused.value.reason == "linked_oid_mismatch"
    assert "sso_linked_oid_mismatch" in audit_actions(db_session)


def test_an_entra_email_change_keeps_the_old_address_as_an_alias(db_session):
    user = mk(db_session, "old@a.example", oid=OID_A)
    ticket = ticket_for(db_session, "old@a.example", "Old ticket")
    resolve_sso_user(db_session, OID_A, "new@a.example", "Ann")
    db_session.commit()
    db_session.expire_all()
    user = db_session.get(User, user.id)
    assert user.email == "new@a.example"
    assert requester_emails(user, db_session) == {"new@a.example", "old@a.example"}
    link = db_session.exec(select(LinkedIdentity)).one()
    assert (link.method, link.status, link.entra_object_id) == ("email_changed", "active", OID_A)
    assert "alias_added" in audit_actions(db_session)
    from app.identity import my_tickets_query

    assert [t.id for t in db_session.exec(my_tickets_query(user)).all()] == [ticket.id]
    # Later sign-ins still refresh the person: a second change is noticed, and changing back retires the alias.
    resolve_sso_user(db_session, OID_A, "newer@a.example", "Ann")
    db_session.commit()
    assert db_session.get(User, user.id).email == "newer@a.example"
    assert requester_emails(db_session.get(User, user.id), db_session) == {"newer@a.example", "new@a.example", "old@a.example"}
    resolve_sso_user(db_session, OID_A, "old@a.example", "Ann")
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(User, user.id).email == "old@a.example"
    active = db_session.exec(select(LinkedIdentity).where(LinkedIdentity.status == "active")).all()
    assert sorted(l.email for l in active) == ["new@a.example", "newer@a.example"]  # old@ is the main address again, not also an alias


def test_an_email_change_to_another_persons_linked_address_is_refused(db_session):
    main = mk(db_session, "ann@a.example", oid=OID_A)
    mk(db_session, "bob@a.example", oid=OID_B)
    db_session.add(LinkedIdentity(main_user_id=main.id, email="spare@a.example", method="email_code", status="active"))
    db_session.commit()
    with pytest.raises(SsoRefused) as refused:
        resolve_sso_user(db_session, OID_B, "spare@a.example", "Bob")
    assert refused.value.reason == "email_conflict"


# ---- self-service by emailed code ---------------------------------------------------------------------------------


def test_a_code_links_an_address_with_no_account_and_is_stored_hashed(client, db_session, captured):
    me = mk(db_session, "ann@a.example")
    login_as(client, "ann@a.example")
    response = client.post("/settings/link-account/code", data={"address": "Ann@B.example"}, follow_redirects=False)
    assert response.status_code == 303 and "sent=1" in response.headers["location"]
    assert captured[-1].to == "ann@b.example"
    code = code_in(captured)
    row = db_session.exec(select(LinkCode)).one()
    assert code not in row.code_hash and len(row.code_hash) == 64
    assert code not in " ".join(a.detail or "" for a in db_session.exec(select(AuditLog)).all())

    done = client.post("/settings/link-account/verify", data={"address": "ann@b.example", "code": code}, follow_redirects=False)
    assert "done=applied" in done.headers["location"]
    db_session.expire_all()
    assert requester_emails(db_session.get(User, me.id), db_session) == {"ann@a.example", "ann@b.example"}
    link = db_session.exec(select(LinkedIdentity)).one()
    assert (link.method, link.status, link.secondary_user_id) == ("email_code", "active", None)
    # Single use.
    again = client.post("/settings/link-account/verify", data={"address": "ann@b.example", "code": code}, follow_redirects=True)
    assert "no code waiting" in again.text


def test_a_code_expires(client, db_session, captured):
    me = mk(db_session, "ann@a.example")
    links.request_code(db_session, me, "ann@b.example", False)
    code = code_in(captured)
    later = datetime.now(timezone.utc) + timedelta(minutes=16)
    with pytest.raises(links.LinkError, match="expired"):
        links.check_code(db_session, me, "ann@b.example", code, now=later)
    row = db_session.exec(select(LinkCode)).one()
    assert row.used_at is None


def test_the_expiry_is_a_setting(db_session, captured):
    from app.settings_store import LINK_CODE_MINUTES

    set_setting(db_session, LINK_CODE_MINUTES, "30")
    db_session.commit()
    me = mk(db_session, "ann@a.example")
    links.request_code(db_session, me, "ann@b.example", False)
    code = code_in(captured)
    links.check_code(db_session, me, "ann@b.example", code, now=datetime.now(timezone.utc) + timedelta(minutes=20))


def test_five_wrong_tries_kill_the_code_even_for_the_right_one(db_session, captured):
    me = mk(db_session, "ann@a.example")
    links.request_code(db_session, me, "ann@b.example", False)
    code = code_in(captured)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        with pytest.raises(links.LinkError, match="not right"):
            links.check_code(db_session, me, "ann@b.example", wrong)
        db_session.rollback()  # as the route does; the count was committed by check_code
    with pytest.raises(links.LinkError, match="Too many wrong tries"):
        links.check_code(db_session, me, "ann@b.example", code)
    assert db_session.exec(select(LinkCode)).one().attempts == 5


def test_the_attempt_limit_is_a_setting(db_session, captured):
    set_setting(db_session, LINK_CODE_ATTEMPTS, "1")
    db_session.commit()
    me = mk(db_session, "ann@a.example")
    links.request_code(db_session, me, "ann@b.example", False)
    code = code_in(captured)
    with pytest.raises(links.LinkError):
        links.check_code(db_session, me, "ann@b.example", "x")
    with pytest.raises(links.LinkError, match="Too many wrong tries"):
        links.check_code(db_session, me, "ann@b.example", code)


def test_a_code_works_only_for_the_person_and_address_it_was_sent_for(db_session, captured):
    me, other = mk(db_session, "ann@a.example"), mk(db_session, "bob@a.example")
    links.request_code(db_session, me, "ann@b.example", False)
    code = code_in(captured)
    with pytest.raises(links.LinkError, match="no code waiting"):
        links.check_code(db_session, other, "ann@b.example", code)
    with pytest.raises(links.LinkError, match="no code waiting"):
        links.check_code(db_session, me, "other@b.example", code)


def test_code_requests_are_limited_per_person_and_per_address(db_session, captured):
    set_setting(db_session, LINK_CODE_PER_USER_HOUR, "2")
    set_setting(db_session, LINK_CODE_PER_ADDRESS_HOUR, "2")
    db_session.commit()
    me, other = mk(db_session, "ann@a.example"), mk(db_session, "bob@a.example")
    third = mk(db_session, "cy@a.example")
    links.request_code(db_session, me, "x@b.example", False)
    links.request_code(db_session, me, "y@b.example", False)
    with pytest.raises(links.LinkError, match="Too many codes"):
        links.request_code(db_session, me, "z@b.example", False)  # per person
    links.request_code(db_session, other, "x@b.example", False)
    with pytest.raises(links.LinkError, match="Too many codes"):
        links.request_code(db_session, third, "x@b.example", False)  # per address, across people
    assert len(captured) == 3


@pytest.mark.parametrize(
    "address, setup, message",
    [
        ("not-an-address", None, "not an email"),
        ("support@shared.example", "mailbox", "shared support mailbox"),
        ("list@corp.example", "never_follow", "never-follow"),
        ("ann@a.example", None, "already one of your addresses"),
        ("agent@b.example", "agent", "emailed code"),
        ("boss@b.example", "admin", "emailed code"),
        ("bg@local.invalid", None, "not a real address"),
    ],
)
def test_addresses_that_can_never_be_linked_by_code(db_session, captured, monkeypatch, address, setup, message):
    from app.config import settings

    me = mk(db_session, "ann@a.example")
    if setup == "mailbox":
        monkeypatch.setattr(settings, "graph_shared_mailbox", "support@shared.example")
    if setup == "never_follow":
        db_session.add(NeverFollowAddress(email=address, reason="list", added_by="x"))
    if setup == "agent":
        mk(db_session, address, UserRole.AGENT)
    if setup == "admin":
        mk(db_session, address, UserRole.SUPER_ADMIN)
    db_session.commit()
    with pytest.raises(links.LinkError, match=message):
        links.request_code(db_session, me, address, False)
    assert captured == [] and db_session.exec(select(LinkCode)).first() is None  # nothing was emailed


def test_an_address_already_linked_to_someone_cannot_be_linked_again(db_session, captured):
    first, second = mk(db_session, "ann@a.example"), mk(db_session, "bob@a.example")
    db_session.add(LinkedIdentity(main_user_id=first.id, email="shared@b.example", method="email_code", status="active"))
    db_session.commit()
    with pytest.raises(links.LinkError, match="cannot be linked with an emailed code") as same:
        links.request_code(db_session, second, "shared@b.example", False)
    # Worded exactly as for an agent account, so the form cannot be used to tell which addresses are which.
    mk(db_session, "agent@b.example", UserRole.AGENT)
    with pytest.raises(links.LinkError) as agent:
        links.request_code(db_session, second, "agent@b.example", False)
    assert str(agent.value) == str(same.value)


def test_a_failed_send_gives_a_clear_error_and_leaves_no_usable_code(db_session, monkeypatch):
    import app.mail as mail
    from app.teams import Unsendable

    def broken(m, send=None):
        raise Unsendable("Email sending is switched off")

    monkeypatch.setattr(mail, "send_now", broken)
    me = mk(db_session, "ann@a.example")
    with pytest.raises(links.LinkError, match="could not be sent"):
        links.request_code(db_session, me, "ann@b.example", False)
    assert db_session.exec(select(LinkCode)).one().used_at is not None


def test_the_code_email_uses_the_admin_template(db_session, captured):
    from app.models import EmailTemplate

    db_session.add(EmailTemplate(key="link_code", subject="Your link code", body="Hi {name}: {code} for {minutes} minutes", updated_by="admin@x.example"))
    db_session.commit()
    me = mk(db_session, "ann@a.example", name="Ann")
    links.request_code(db_session, me, "ann@b.example", False)
    assert captured[-1].subject == "Your link code" and "for 15 minutes" in captured[-1].body
    assert captured[-1].record == "(link code, not kept)"


def test_by_code_with_an_existing_end_user_account_the_user_chooses_the_main(db_session, captured):
    me, other = mk(db_session, "ann@a.example"), mk(db_session, "ann@b.example")
    mine = ticket_for(db_session, "ann@a.example", "Mine")
    links.request_code(db_session, me, "ann@b.example", True)  # the other account is to be the main one
    outcome = links.link_by_code(db_session, me, "ann@b.example", code_in(captured))
    db_session.commit()
    assert outcome.applied and outcome.signed_out  # the signed-in account was merged away
    link = db_session.exec(select(LinkedIdentity)).one()
    assert (link.main_user_id, link.secondary_user_id) == (other.id, me.id)
    assert db_session.get(User, me.id).is_active is False
    assert requester_emails(db_session.get(User, other.id), db_session) == {"ann@b.example", "ann@a.example"}
    assert mine.number


def test_the_address_with_no_account_cannot_be_the_main_one(db_session, captured):
    me = mk(db_session, "ann@a.example")
    with pytest.raises(links.LinkError, match="no account"):
        links.request_code(db_session, me, "nobody@b.example", True)


def test_by_code_through_the_route_signs_out_a_merged_away_requester(client, db_session, captured):
    me, other = mk(db_session, "ann@a.example"), mk(db_session, "ann@b.example")
    login_as(client, "ann@a.example")
    client.post("/settings/link-account/code", data={"address": "ann@b.example", "other_is_main": "true"})
    response = client.post("/settings/link-account/verify", data={"address": "ann@b.example", "code": code_in(captured)}, follow_redirects=False)
    assert response.headers["location"].startswith("/auth/login")
    assert client.get("/portal", follow_redirects=False).status_code != 200
    assert me.id != other.id


# ---- an agent or admin on either side needs a super admin -------------------------------------------------------------


def test_an_agent_linking_an_address_by_code_becomes_a_request_a_super_admin_decides(client, db_session, captured):
    admin = super_admin(client, db_session)
    agent = mk(db_session, "ann@a.example", UserRole.AGENT)
    login_as(client, "ann@a.example")
    client.post("/settings/link-account/code", data={"address": "ann@b.example"})
    response = client.post("/settings/link-account/verify", data={"address": "ann@b.example", "code": code_in(captured)}, follow_redirects=False)
    assert "done=requested" in response.headers["location"]
    db_session.expire_all()
    link = db_session.exec(select(LinkedIdentity)).one()
    assert link.status == "pending"
    assert requester_emails(db_session.get(User, agent.id), db_session) == {"ann@a.example"}  # not in force yet
    bell = db_session.exec(select(Notification).where(Notification.kind == "link_requested")).one()
    assert bell.user_id == admin.id and "ann@b.example" in bell.text

    client.post("/auth/logout")
    login_as(client, admin.email)
    assert "Link requests waiting for you" in client.get("/admin/users").text
    client.post(f"/admin/links/{link.id}/approve")
    db_session.expire_all()
    assert db_session.get(LinkedIdentity, link.id).status == "active"
    assert db_session.get(LinkedIdentity, link.id).approved_by_id == admin.id
    assert requester_emails(db_session.get(User, agent.id), db_session) == {"ann@a.example", "ann@b.example"}


def test_a_super_admin_can_decline_and_a_person_can_withdraw_a_request(client, db_session, captured):
    admin = super_admin(client, db_session)
    mk(db_session, "ann@a.example", UserRole.AGENT)
    login_as(client, "ann@a.example")
    client.post("/settings/link-account/code", data={"address": "ann@b.example"})
    client.post("/settings/link-account/verify", data={"address": "ann@b.example", "code": code_in(captured)})
    link = db_session.exec(select(LinkedIdentity)).one()
    assert client.post(f"/settings/link-account/{link.id}/cancel", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(LinkedIdentity, link.id).status == "declined"
    assert "identity_link_declined" in audit_actions(db_session)
    assert admin.id


def test_an_ordinary_user_cannot_approve_or_unlink_what_is_not_theirs(client, db_session):
    a, b = mk(db_session, "a@a.example"), mk(db_session, "b@b.example")
    link = LinkedIdentity(main_user_id=a.id, email="x@b.example", method="email_code", status="pending", requested_by_id=a.id)
    db_session.add(link)
    db_session.commit()
    login_as(client, "b@b.example")
    assert client.post(f"/admin/links/{link.id}/approve").status_code == 403
    assert client.post(f"/settings/link-account/{link.id}/cancel").status_code == 404
    assert b.id


def test_a_request_can_no_longer_be_applied_once_the_main_account_became_inactive(db_session):
    admin = mk(db_session, "root@a.example", UserRole.SUPER_ADMIN)
    main = mk(db_session, "a@a.example", UserRole.AGENT)
    link = links.link_address(db_session, main=main, address="b@b.example", oid=None, method="email_code", requester=main)
    main.is_active = False
    db_session.add(main)
    db_session.commit()
    with pytest.raises(links.LinkError, match="main account"):
        links.approve(db_session, link, admin)


def test_unlinking_a_privileged_link_needs_a_super_admin(db_session):
    admin = mk(db_session, "root@a.example", UserRole.SUPER_ADMIN)
    main, other = mk(db_session, "a@a.example", UserRole.AGENT), mk(db_session, "b@b.example", UserRole.AGENT)
    link = links.link_accounts(db_session, main=main, secondary=other, admin=admin)
    with pytest.raises(links.LinkError, match="super admin"):
        links.unlink(db_session, link, main)
    plain_main, plain_other = mk(db_session, "c@a.example"), mk(db_session, "d@b.example")
    plain = links.link_accounts(db_session, main=plain_main, secondary=plain_other, admin=admin)
    links.unlink(db_session, plain, plain_main)  # ordinary accounts: the person may


# ---- self-service by Microsoft sign-in ---------------------------------------------------------------------------------


def sso_for_linking(monkeypatch):
    from app.config import settings
    from app.routers import link_account

    seen = {"claims": {}}
    monkeypatch.setattr(settings, "sso_tenant_id", "t")
    monkeypatch.setattr(settings, "sso_client_id", "c")
    monkeypatch.setattr(settings, "sso_client_secret", "s")
    monkeypatch.setattr(link_account, "build_auth_url", lambda state, prompt=None: seen.update(state=state, prompt=prompt) or "https://login.example/authorize")
    monkeypatch.setattr(link_account, "acquire_token_by_auth_code", lambda code: {"id_token_claims": seen["claims"]})
    return seen


def test_linking_by_signing_in_with_the_second_account(client, db_session, monkeypatch):
    seen = sso_for_linking(monkeypatch)
    me = mk(db_session, "ann@a.example")
    login_as(client, "ann@a.example")
    start = client.post("/settings/link-account/sign-in", follow_redirects=False)
    assert start.status_code == 303 and seen["prompt"] == "select_account"
    seen["claims"] = {"preferred_username": "Ann@B.example", "oid": OID_B}
    done = client.get(f"/auth/callback?code=c&state={seen['state']}", follow_redirects=False)
    assert "done=applied" in done.headers["location"]
    db_session.expire_all()
    link = db_session.exec(select(LinkedIdentity)).one()
    assert (link.method, link.email, link.entra_object_id, link.status) == ("sign_in", "ann@b.example", OID_B, "active")
    assert requester_emails(db_session.get(User, me.id), db_session) == {"ann@a.example", "ann@b.example"}
    # Later Entra sign-ins with that oid are Ann's account.
    assert resolve_sso_user(db_session, OID_B, "ann@b.example", "Ann").id == me.id
    # It started no session of its own.
    assert db_session.exec(select(UserSession).where(UserSession.user_id != me.id)).first() is None


def test_the_sign_in_state_is_tied_to_the_person_who_started_it(client, db_session, monkeypatch):
    seen = sso_for_linking(monkeypatch)
    mk(db_session, "ann@a.example")
    login_as(client, "ann@a.example")
    client.post("/settings/link-account/sign-in")
    seen["claims"] = {"preferred_username": "ann@b.example", "oid": OID_B}
    # Another browser (no cookie holding the flow) with the same state value completes nothing.
    stranger = type(client)(client.app)
    response = stranger.get(f"/auth/callback?code=c&state={seen['state']}", follow_redirects=False)
    assert response.status_code == 400
    assert db_session.exec(select(LinkedIdentity)).first() is None
    # Someone else signed in on the browser that holds the flow: refused.
    client.post("/auth/logout")
    other_client = type(client)(client.app)
    login_as(other_client, "bob@a.example")
    other_client.post("/settings/link-account/sign-in")
    login_as(client, "ann@a.example")
    assert db_session.exec(select(LinkedIdentity)).first() is None


def test_signing_in_as_an_agent_account_links_after_approval_and_the_chosen_main_wins(client, db_session, monkeypatch):
    seen = sso_for_linking(monkeypatch)
    admin = super_admin(client, db_session)
    me = mk(db_session, "ann@a.example")
    agent = mk(db_session, "ann@b.example", UserRole.AGENT, oid=OID_B)
    login_as(client, "ann@a.example")
    client.post("/settings/link-account/sign-in", data={"other_is_main": "true"})
    seen["claims"] = {"preferred_username": "ann@b.example", "oid": OID_B}
    done = client.get(f"/auth/callback?code=c&state={seen['state']}", follow_redirects=False)
    assert "done=requested" in done.headers["location"]
    link = db_session.exec(select(LinkedIdentity)).one()
    assert (link.main_user_id, link.secondary_user_id, link.status) == (agent.id, me.id, "pending")
    login_as(client, admin.email)
    client.post(f"/admin/links/{link.id}/approve")
    db_session.expire_all()
    assert db_session.get(User, me.id).is_active is False
    assert requester_emails(db_session.get(User, agent.id), db_session) == {"ann@b.example", "ann@a.example"}


def test_a_microsoft_account_that_is_already_linked_or_is_my_own_is_refused(client, db_session, monkeypatch):
    seen = sso_for_linking(monkeypatch)
    me = mk(db_session, "ann@a.example", oid=OID_A)
    login_as(client, "ann@a.example")
    client.post("/settings/link-account/sign-in")
    seen["claims"] = {"preferred_username": "ann@a.example", "oid": OID_A}
    own = client.get(f"/auth/callback?code=c&state={seen['state']}", follow_redirects=False)
    assert "error=" in own.headers["location"]
    assert db_session.exec(select(LinkedIdentity)).first() is None
    assert me.id


def test_the_link_page_renders_and_is_reachable_from_my_settings(client, db_session):
    mk(db_session, "ann@a.example")
    login_as(client, "ann@a.example")
    assert "/settings/link-account" in client.get("/settings/notifications").text
    page = client.get("/settings/link-account")
    assert page.status_code == 200 and "Link another account" in page.text
    assert "6-digit code" in client.get("/settings/link-account", params={"sent": 1, "address": "x@b.example"}).text


def test_the_admin_users_page_lists_links_and_marks_the_merged_away_account(client, db_session):
    admin = super_admin(client, db_session)
    main, other = mk(db_session, "ann@a.example"), mk(db_session, "ann@b.example")
    login_as(client, admin.email)
    client.post("/admin/links", data={"main_id": main.id, "secondary_id": other.id})
    page = client.get("/admin/users").text
    assert "Linked identities" in page and "merged into account" in page
    assert client.post(f"/admin/users/{other.id}/active", data={"is_active": "true"}).status_code == 400


def test_an_account_that_became_an_agent_after_the_code_was_sent_is_still_refused(db_session, captured):
    me, other = mk(db_session, "ann@a.example"), mk(db_session, "ann@b.example")
    links.request_code(db_session, me, "ann@b.example", False)
    other.role = UserRole.AGENT
    db_session.add(other)
    db_session.commit()
    with pytest.raises(links.LinkError, match="emailed code"):
        links.link_by_code(db_session, me, "ann@b.example", code_in(captured))


def test_asking_for_a_new_code_does_not_reset_the_guess_budget(db_session, captured):
    me = mk(db_session, "ann@a.example")
    links.request_code(db_session, me, "ann@b.example", False)
    for _ in range(5):
        with pytest.raises(links.LinkError):
            links.check_code(db_session, me, "ann@b.example", "000000" if code_in(captured) != "000000" else "111111")
        db_session.rollback()
    with pytest.raises(links.LinkError, match="Too many wrong tries"):
        links.request_code(db_session, me, "ann@b.example", False)  # no new code, no new guesses
    assert len(captured) == 1
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    links.request_code(db_session, me, "ann@b.example", False, now=later)  # an hour later it is allowed again
    assert len(captured) == 2


def test_merging_an_account_that_changed_its_email_before_keeps_everything_working(db_session):
    admin = mk(db_session, "root@a.example", UserRole.SUPER_ADMIN)
    main = mk(db_session, "ann@a.example", oid=OID_A)
    other = mk(db_session, "ann@b.example", oid=OID_B)
    resolve_sso_user(db_session, OID_B, "annie@b.example", "Ann")  # other changed its address: an alias of old ann@b
    db_session.commit()
    links.link_accounts(db_session, main=main, secondary=other, admin=admin)  # used to fail on the unique object id
    db_session.commit()
    assert requester_emails(main, db_session) == {"ann@a.example", "annie@b.example", "ann@b.example"}
    assert resolve_sso_user(db_session, OID_B, "annie@b.example", "Ann").id == main.id


def test_approving_a_request_whose_address_was_linked_elsewhere_meanwhile_is_a_clear_refusal(db_session):
    admin = mk(db_session, "root@a.example", UserRole.SUPER_ADMIN)
    one, two = mk(db_session, "one@a.example", UserRole.AGENT), mk(db_session, "two@a.example")
    waiting = links.link_address(db_session, main=one, address="x@b.example", oid=OID_B, method="sign_in", requester=one)
    db_session.add(LinkedIdentity(main_user_id=two.id, email="other@b.example", entra_object_id=OID_B, method="sign_in", status="active"))
    db_session.commit()
    with pytest.raises(links.LinkError, match="already linked"):
        links.approve(db_session, waiting, admin)
