import pytest
from sqlmodel import select

from app.approval_groups import (
    ApprovalGroupError,
    add_member,
    create_group,
    group_is_used_by_active_type,
    members_of,
    remove_member,
)
from app.models import ApprovalGroup, ApprovalGroupMember, AuditLog, User, UserRole
from tests.conftest import login_as, set_role
from tests.test_request_types import admin, group_of, make, person


def actions(db_session):
    return [(a.action, a.detail) for a in db_session.exec(select(AuditLog).where(AuditLog.entity_type == "approval_group")).all()]


def test_an_admin_creates_a_group_adds_and_removes_members_with_audit(client, db_session):
    person(client, db_session, "boss@example.com", "Big Boss")
    person(client, db_session, "lead@example.com", "Team Lead")
    admin(client, db_session)

    assert client.post("/admin/approval-groups", data={"name": " Managers "}, follow_redirects=False).status_code == 303
    [group] = db_session.exec(select(ApprovalGroup)).all()
    assert group.name == "Managers"

    for email in ("BOSS@example.com", "lead@example.com"):
        assert client.post(f"/admin/approval-groups/{group.id}/members", data={"email": email}, follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert [u.email for u in members_of(db_session, group.id)] == ["boss@example.com", "lead@example.com"]
    page = client.get("/admin/approval-groups").text
    assert "Managers" in page and "Big Boss" in page and "lead@example.com" in page

    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    client.post(f"/admin/approval-groups/{group.id}/members/{boss.id}/remove")
    client.post(f"/admin/approval-groups/{group.id}/rename", data={"name": "Heads"})
    db_session.expire_all()
    assert [u.email for u in members_of(db_session, group.id)] == ["lead@example.com"]
    assert actions(db_session) == [
        ("created", "Managers"),
        ("member_added", "boss@example.com"),
        ("member_added", "lead@example.com"),
        ("member_removed", "boss@example.com"),
        ("renamed", "'Managers' -> 'Heads'"),
    ]


@pytest.mark.parametrize("name", ["  ", "x" * 101])
def test_a_bad_group_name_is_refused(client, db_session, name):
    admin(client, db_session)
    assert client.post("/admin/approval-groups", data={"name": name}).status_code == 400
    assert db_session.exec(select(ApprovalGroup)).all() == []


def test_group_names_are_unique_whatever_the_case(client, db_session):
    admin(client, db_session)
    group_of(db_session, "Managers")
    other = group_of(db_session, "Leads")

    assert client.post("/admin/approval-groups", data={"name": "MANAGERS"}).status_code == 400
    assert client.post(f"/admin/approval-groups/{other.id}/rename", data={"name": "managers"}).status_code == 400
    assert client.post(f"/admin/approval-groups/{other.id}/rename", data={"name": "LEADS"}, follow_redirects=False).status_code == 303  # its own name, new case


def test_a_member_needs_an_account_and_is_added_once(client, db_session):
    group = group_of(db_session, "Managers", ())
    person(client, db_session, "boss@example.com")
    admin(client, db_session)

    for bad, text in (("ghost@example.com", "no account yet"), ("not-an-address", "not a valid")):
        response = client.post(f"/admin/approval-groups/{group.id}/members", data={"email": bad})
        assert response.status_code == 400 and text in response.text
    assert client.post(f"/admin/approval-groups/{group.id}/members", data={"email": "boss@example.com"}).status_code == 200
    twice = client.post(f"/admin/approval-groups/{group.id}/members", data={"email": "boss@example.com"})
    assert twice.status_code == 400 and "already in this group" in twice.text
    assert len(db_session.exec(select(ApprovalGroupMember)).all()) == 1


def test_removing_someone_who_is_not_a_member_or_from_an_unknown_group_fails(client, db_session):
    group = group_of(db_session, "Managers")
    admin(client, db_session)
    admin_user = db_session.exec(select(User).where(User.email == "admin@example.com")).one()

    assert client.post(f"/admin/approval-groups/{group.id}/members/{admin_user.id}/remove").status_code == 400
    assert client.post("/admin/approval-groups/9999/members/1/remove").status_code == 404
    assert client.post("/admin/approval-groups/9999/rename", data={"name": "x"}).status_code == 404
    assert client.post("/admin/approval-groups/9999/members", data={"email": "a@example.com"}).status_code == 404


def test_the_last_member_of_a_group_an_active_type_uses_cannot_be_removed(client, db_session):
    admin(client, db_session)
    kind = make(db_session, "Access", members=("boss@example.com", "lead@example.com"))
    group = db_session.get(ApprovalGroup, kind.approval_group_id)
    boss, lead = (db_session.exec(select(User).where(User.email == e)).one() for e in ("boss@example.com", "lead@example.com"))
    assert group_is_used_by_active_type(db_session, group.id) == "Access"

    assert client.post(f"/admin/approval-groups/{group.id}/members/{boss.id}/remove", follow_redirects=False).status_code == 303
    refused = client.post(f"/admin/approval-groups/{group.id}/members/{lead.id}/remove")
    assert refused.status_code == 400 and "cannot be left empty" in refused.text
    assert [u.email for u in members_of(db_session, group.id)] == ["lead@example.com"]

    kind.is_active = False  # a dormant type may use an empty group
    db_session.add(kind)
    db_session.commit()
    assert client.post(f"/admin/approval-groups/{group.id}/members/{lead.id}/remove", follow_redirects=False).status_code == 303


def test_the_page_says_which_types_use_a_group(client, db_session):
    admin(client, db_session)
    make(db_session, "Access")
    assert "Used by: Access" in client.get("/admin/approval-groups").text


def test_only_super_admins_manage_groups(client, db_session):
    group = group_of(db_session, "Managers")
    login_as(client, "agent@example.com")
    set_role(db_session, login_as(client, "agent@example.com"), UserRole.AGENT)
    assert client.get("/admin/approval-groups").status_code == 403
    assert client.post("/admin/approval-groups", data={"name": "x"}).status_code == 403
    assert client.post(f"/admin/approval-groups/{group.id}/members", data={"email": "a@example.com"}).status_code == 403
    assert client.post(f"/admin/approval-groups/{group.id}/members/1/remove").status_code == 403
    assert client.post(f"/admin/approval-groups/{group.id}/rename", data={"name": "x"}).status_code == 403


def test_the_service_functions_raise_friendly_errors(db_session):
    group = create_group(db_session, "Managers")
    with pytest.raises(ApprovalGroupError):
        create_group(db_session, "managers")
    with pytest.raises(ApprovalGroupError):
        add_member(db_session, group, "ghost@example.com")
    with pytest.raises(ApprovalGroupError):
        remove_member(db_session, group, 12345)


def test_a_name_clash_that_slips_past_the_check_is_a_friendly_error(client, db_session, monkeypatch):
    admin(client, db_session)
    group_of(db_session, "Taken")
    monkeypatch.setattr("app.approval_groups._name_taken", lambda *args, **kwargs: False)  # two admins at once

    response = client.post("/admin/approval-groups", data={"name": "Taken"})

    assert response.status_code == 400 and "already done" in response.text


def test_the_admin_tab_is_there(client, db_session):
    admin(client, db_session)
    assert 'href="/admin/approval-groups"' in client.get("/admin/request-types").text


def test_two_admins_removing_the_last_two_members_at_once_cannot_empty_a_live_group(client, db_session):
    """The guard counts members under a lock on the group, so the second remover sees the first's removal."""
    import threading

    from sqlmodel import Session

    from app.database import engine

    kind = make(db_session, "Access", members=("boss@example.com", "lead@example.com"))
    group_id = kind.approval_group_id
    ids = [u.id for u in members_of(db_session, group_id)]
    outcomes = []

    def remove(user_id):
        with Session(engine) as session:
            try:
                remove_member(session, session.get(ApprovalGroup, group_id), user_id)
                session.commit()
                outcomes.append("removed")
            except ApprovalGroupError:
                session.rollback()
                outcomes.append("refused")

    threads = [threading.Thread(target=remove, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    db_session.expire_all()
    assert sorted(outcomes) == ["refused", "removed"]
    assert len(members_of(db_session, group_id)) == 1
