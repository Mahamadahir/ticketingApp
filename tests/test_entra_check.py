"""6B (#158): the nightly Entra accountEnabled check, and what being inactive means everywhere.
Graph is simulated; a real disabled account being picked up overnight is an Azure-only check."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import entra_check
from app.approval_groups import ApprovalGroupError, add_member, create_group
from app.config import settings
from app.entra_check import check_accounts, is_due, run_entra_account_check
from app.graph_client import GraphClient
from app.main import app
from app.models import (
    AuditLog,
    Notification,
    Setting,
    Team,
    Ticket,
    TicketStatus,
    User,
    UserRole,
    UserSession,
)
from app.scheduled_jobs import SCHEDULED_JOBS
from app.settings_store import ENTRA_CHECK_TIME, set_setting
from app.triage import TriageError, reassign_ticket
from app.visibility import assignable_agents, team_members
from tests.conftest import add_membership, create_category, create_team, login_as, set_role


def an_agent(db_session, email, oid=None, role=UserRole.AGENT, team=None, **fields):
    agent = User(email=email, display_name=email.split("@")[0], role=role, entra_object_id=oid, **fields)
    db_session.add(agent)
    db_session.commit()
    if team is not None:
        add_membership(db_session, agent.id, team.id)
    return agent


class FakeEntra:
    """oid -> True (enabled), False (disabled), None (404) or an exception to raise."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def __call__(self, oid):
        self.asked.append(oid)
        answer = self.answers[oid]
        if isinstance(answer, Exception):
            raise answer
        return answer


def audits(db_session, action):
    db_session.expire_all()
    return db_session.exec(select(AuditLog).where(AuditLog.action == action)).all()


def run(db_session, answers):
    fake = FakeEntra(answers)
    result = check_accounts(db_session, fake)
    db_session.commit()
    db_session.expire_all()
    return result, fake


# ---- The check ------------------------------------------------------------------------------------------


def test_a_disabled_account_becomes_inactive_with_an_audit_entry(db_session):
    gone = an_agent(db_session, "gone@example.com", "oid-gone")
    kept = an_agent(db_session, "kept@example.com", "oid-kept")
    result, _ = run(db_session, {"oid-gone": False, "oid-kept": True})
    assert (result.looked_at, result.deactivated, result.failed) == (2, 1, 0)
    assert not db_session.get(User, gone.id).is_active and db_session.get(User, kept.id).is_active
    [entry] = audits(db_session, "deactivated_by_entra")
    assert entry.entity_id == gone.id and entry.actor_email == "system" and "disabled" in entry.detail


def test_an_account_entra_does_not_have_is_deactivated_too(db_session):
    gone = an_agent(db_session, "gone@example.com", "oid-gone")
    run(db_session, {"oid-gone": None})
    assert not db_session.get(User, gone.id).is_active
    assert "not found" in audits(db_session, "deactivated_by_entra")[0].detail


def test_an_enabled_account_is_left_alone_and_a_re_enabled_one_is_not_reactivated(db_session):
    person = an_agent(db_session, "back@example.com", "oid-back", is_active=False)
    result, fake = run(db_session, {"oid-back": True})
    assert fake.asked == [] and result.looked_at == 0  # inactive people are not even asked about
    assert not db_session.get(User, person.id).is_active


def test_a_transient_failure_changes_nothing_and_the_rest_are_still_checked(db_session):
    shaky = an_agent(db_session, "shaky@example.com", "oid-shaky")
    gone = an_agent(db_session, "gone@example.com", "oid-gone")
    boom = httpx.HTTPStatusError("503", request=httpx.Request("GET", "https://x"), response=httpx.Response(503))
    result, _ = run(db_session, {"oid-shaky": boom, "oid-gone": False})
    assert (result.failed, result.deactivated) == (1, 1)
    assert db_session.get(User, shaky.id).is_active and not db_session.get(User, gone.id).is_active


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("slow"), httpx.HTTPStatusError(
    "429", request=httpx.Request("GET", "https://x"), response=httpx.Response(429))])
def test_timeouts_and_throttling_deactivate_nobody(db_session, error):
    person = an_agent(db_session, "a@example.com", "oid-a")
    result, _ = run(db_session, {"oid-a": error})
    assert result.failed == 1 and db_session.get(User, person.id).is_active


def test_who_is_skipped(db_session):
    an_agent(db_session, "requester@example.com", "oid-r", role=UserRole.END_USER)
    an_agent(db_session, "noid@example.com", None)  # break-glass, dev login
    an_agent(db_session, "local@example.com", "oid-l", password_hash="x")  # a local account
    checked = an_agent(db_session, "agent@example.com", "oid-a")
    admin = an_agent(db_session, "admin@example.com", "oid-s", role=UserRole.SUPER_ADMIN)
    result, fake = run(db_session, {"oid-a": True, "oid-s": True})
    assert sorted(fake.asked) == ["oid-a", "oid-s"] and result.looked_at == 2
    assert checked.id and admin.id


def test_their_sessions_end_and_the_super_admins_are_told_once(client, db_session):
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    leaver = TestClient(app)
    leaver_id = login_as(leaver, "leaver@example.com")
    set_role(db_session, leaver_id, UserRole.AGENT)
    db_session.get(User, leaver_id).entra_object_id = "oid-leaver"
    db_session.commit()
    assert leaver.get("/portal", follow_redirects=False).status_code == 200

    run(db_session, {"oid-leaver": False})
    rows = db_session.exec(select(UserSession).where(UserSession.user_id == leaver_id)).all()
    assert rows and all(row.revoked_at is not None for row in rows)
    assert audits(db_session, "sessions_revoked")
    assert leaver.get("/portal", follow_redirects=False).status_code != 200
    told = db_session.exec(select(Notification).where(Notification.kind == "account_deactivated")).all()
    assert [n.user_id for n in told] == [admin_id] and "leaver@example.com" in told[0].text and told[0].bell

    run(db_session, {})  # the next night: nothing more to say
    assert len(db_session.exec(select(Notification).where(Notification.kind == "account_deactivated")).all()) == 1
    assert "leaver@example.com" in client.get("/notifications").text


# ---- The job: switch, schedule, failure --------------------------------------------------------------------


def configure(monkeypatch, enabled=True, configured=True):
    monkeypatch.setattr(settings, "entra_account_check_enabled", enabled)
    monkeypatch.setattr(settings, "graph_tenant_id", "t" if configured else "")
    monkeypatch.setattr(settings, "graph_client_id", "c" if configured else "")
    monkeypatch.setattr(settings, "graph_client_secret", "s" if configured else "")


def test_the_switch_defaults_off_and_with_it_off_nothing_is_looked_up(db_session, monkeypatch):
    assert type(settings).model_fields["entra_account_check_enabled"].default is False
    asked = []
    monkeypatch.setattr(GraphClient, "account_enabled", lambda self, oid: asked.append(oid))
    an_agent(db_session, "a@example.com", "oid-a")
    for enabled, configured in ((False, True), (True, False)):
        configure(monkeypatch, enabled, configured)
        run_entra_account_check(db_session, datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc))
    assert asked == []


def test_it_runs_once_a_night_after_the_set_time_in_the_organisation_zone(db_session):
    # Europe/London, 02:00. 6 Nov 2026 is GMT, so UTC and local agree.
    day = lambda hour, minute=0: datetime(2026, 11, 6, hour, minute, tzinfo=timezone.utc)
    assert not is_due(db_session, day(1, 59))
    assert is_due(db_session, day(2, 0))
    db_session.add(Setting(key=entra_check.LAST_RUN_KEY, value=day(2, 5).isoformat()))
    db_session.commit()
    assert not is_due(db_session, day(9, 0))  # done tonight
    assert is_due(db_session, day(2, 0) + timedelta(days=1))
    set_setting(db_session, ENTRA_CHECK_TIME, "04:30")
    assert not is_due(db_session, day(3, 0) + timedelta(days=1))
    assert is_due(db_session, day(4, 30) + timedelta(days=1))


def test_the_run_time_follows_the_organisation_time_zone_in_summer(db_session):
    # 2 Jul 2026 01:30 UTC is 02:30 in London: due. 00:30 UTC is 01:30 London: not yet.
    assert is_due(db_session, datetime(2026, 7, 2, 1, 30, tzinfo=timezone.utc))
    assert not is_due(db_session, datetime(2026, 7, 2, 0, 30, tzinfo=timezone.utc))


def test_the_job_deactivates_through_graph_when_on_and_due(db_session, monkeypatch):
    configure(monkeypatch)
    person = an_agent(db_session, "a@example.com", "oid-a")
    monkeypatch.setattr(GraphClient, "account_enabled", lambda self, oid: False)
    run_entra_account_check(db_session, datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc))
    db_session.commit()
    db_session.expire_all()
    assert not db_session.get(User, person.id).is_active


def test_the_job_is_not_run_before_its_time(db_session, monkeypatch):
    configure(monkeypatch)
    an_agent(db_session, "a@example.com", "oid-a")
    monkeypatch.setattr(GraphClient, "account_enabled", lambda self, oid: pytest.fail("asked too early"))
    run_entra_account_check(db_session, datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc))


def test_when_every_lookup_fails_the_job_fails_visibly_and_changes_nothing(db_session, monkeypatch):
    configure(monkeypatch)
    people = [an_agent(db_session, f"a{n}@example.com", f"oid-{n}") for n in range(3)]

    def refused(self, oid):
        raise httpx.HTTPStatusError("403", request=httpx.Request("GET", "https://x"), response=httpx.Response(403))

    monkeypatch.setattr(GraphClient, "account_enabled", refused)
    with pytest.raises(RuntimeError, match="every one of 3"):
        run_entra_account_check(db_session, datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc))
    db_session.rollback()
    assert all(db_session.get(User, p.id).is_active for p in people)


def test_the_job_is_registered_hourly():
    [job] = [j for j in SCHEDULED_JOBS if j.name == "entra-account-check"]
    assert job.every_seconds == 3600


# ---- The Graph call -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, body, expected", [(200, {"accountEnabled": False}, False), (200, {"accountEnabled": True}, True), (404, {}, None)]
)
def test_the_graph_call_reads_account_enabled_and_treats_404_as_not_found(monkeypatch, status, body, expected):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params)
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    client = GraphClient()
    monkeypatch.setattr(client, "_get_access_token", lambda: "token")
    assert client.account_enabled("oid-1") is expected
    assert seen["url"].endswith("/users/oid-1") and seen["params"] == {"$select": "accountEnabled"}


def test_the_graph_call_raises_on_a_5xx_or_an_unreadable_answer(monkeypatch):
    client = GraphClient()
    monkeypatch.setattr(client, "_get_access_token", lambda: "token")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(503, request=httpx.Request("GET", url)))
    with pytest.raises(httpx.HTTPStatusError):
        client.account_enabled("oid-1")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json={}, request=httpx.Request("GET", url)))
    with pytest.raises(ValueError):
        client.account_enabled("oid-1")


# ---- Being inactive: pickers, assignment, voters, permissions, the ticket page -------------------------------------


def world(client, db_session):
    team = create_team(db_session, "Support")
    admin_id = login_as(client, "admin@example.com")
    set_role(db_session, admin_id, UserRole.SUPER_ADMIN)
    active = an_agent(db_session, "active@example.com", team=team)
    inactive = an_agent(db_session, "gone@example.com", team=team, is_active=False)
    return team, active, inactive


def test_inactive_agents_are_left_out_of_every_team_and_assign_list(client, db_session):
    team, active, inactive = world(client, db_session)
    assert [a.id for a in team_members(db_session, team.id)] == [active.id]
    assert {a.id for a in team_members(db_session, team.id, include_inactive=True)} == {active.id, inactive.id}
    ticket = Ticket(subject="x", requester_email="r@example.com", team_id=None, status=TicketStatus.NEW)
    db_session.add(ticket)
    db_session.commit()
    assert inactive.id not in {a.id for a in assignable_agents(db_session, ticket)}
    page = client.get(f"/tickets/agents-for-team?team_id={team.id}").text
    assert "active" in page and "gone" not in page


def test_an_inactive_agent_cannot_be_chosen_as_the_assignee(client, db_session):
    team, active, inactive = world(client, db_session)
    ticket = Ticket(subject="x", requester_email="r@example.com", team_id=team.id, status=TicketStatus.OPEN,
                    triaged_at=datetime.now(timezone.utc))
    db_session.add(ticket)
    db_session.commit()
    admin = db_session.exec(select(User).where(User.email == "admin@example.com")).one()
    with pytest.raises(TriageError, match="inactive"):
        reassign_ticket(db_session, ticket, admin, team=team, assignee=inactive)


def test_a_ticket_stays_with_its_inactive_assignee_and_says_so(client, db_session):
    team, active, inactive = world(client, db_session)
    ticket = Ticket(subject="Kept", requester_email="r@example.com", team_id=team.id, status=TicketStatus.OPEN,
                    triaged_at=datetime.now(timezone.utc), assigned_agent_id=inactive.id)
    db_session.add(ticket)
    db_session.commit()
    page = client.get(f"/tickets/{ticket.number}").text
    assert "<dt>Assigned</dt><dd>gone (inactive)</dd>" in page and "assigned to gone (inactive)" in page
    rest = client.get(f"/tickets/{ticket.number}").text.replace("<dd>gone (inactive)</dd>", "").replace("assigned to gone (inactive)", "")
    assert "(inactive)" not in rest
    # moving it within the team keeps the inactive assignee rather than silently dropping them
    other = create_team(db_session, "Other")
    add_membership(db_session, inactive.id, other.id)
    admin = db_session.exec(select(User).where(User.email == "admin@example.com")).one()
    reassign_ticket(db_session, ticket, admin, team=other)
    db_session.refresh(ticket)
    assert ticket.assigned_agent_id == inactive.id


def test_admin_users_lists_inactive_agents_who_still_hold_open_tickets(client, db_session):
    team, active, inactive = world(client, db_session)
    quiet = an_agent(db_session, "quiet@example.com", team=team, is_active=False)
    for status, who in ((TicketStatus.OPEN, inactive), (TicketStatus.CLOSED, quiet), (TicketStatus.OPEN, active)):
        db_session.add(Ticket(subject="t", requester_email="r@example.com", team_id=team.id, status=status,
                              triaged_at=datetime.now(timezone.utc), assigned_agent_id=who.id))
    db_session.commit()
    page = client.get("/admin/users?inactive=open_tickets").text
    assert "gone@example.com" in page and "quiet@example.com" not in page and "active@example.com" not in page
    everyone = client.get("/admin/users").text
    assert "quiet@example.com" in everyone and "Inactive agents with open tickets" in everyone


def test_inactive_people_cannot_be_approvers_or_get_permissions(client, db_session):
    team, active, inactive = world(client, db_session)
    group = create_group(db_session, "Approvers")
    with pytest.raises(ApprovalGroupError, match="inactive"):
        add_member(db_session, group, inactive.email)
    add_member(db_session, group, active.email)
    active.is_active = False  # left later: still listed on the admin page but never a voter
    db_session.add(active)
    db_session.commit()
    from app.approval_groups import members_of

    assert [u.id for u in members_of(db_session, group.id)] == [active.id]
    assert members_of(db_session, group.id, active_only=True) == []
    assert "inactive: not counted as a voter" in client.get(f"/admin/approval-groups?group={group.id}").text
    refused = client.post(f"/admin/users/{inactive.id}/permissions", data={"permissions": "manage_request_templates"})
    assert refused.status_code == 400


def test_a_template_cannot_name_an_inactive_agent(client, db_session):
    from app.subrequests import TemplateError, _check_template
    from app.models import RequestType

    team, active, inactive = world(client, db_session)
    request_type = RequestType(name="Starter")
    db_session.add(request_type)
    db_session.commit()
    category = create_category(db_session, team)
    with pytest.raises(TemplateError, match="active agent"):
        _check_template(db_session, request_type, "Laptop", team.id, category.id, None, None, None, inactive.id, False, None)
    _check_template(db_session, request_type, "Laptop", team.id, category.id, None, None, None, active.id, False, None)


def test_a_mention_of_an_inactive_agent_tells_nobody(client, db_session):
    from app.notifications import mentioned_in

    team, active, inactive = world(client, db_session)
    ticket = Ticket(subject="x", requester_email="r@example.com", team_id=team.id, status=TicketStatus.OPEN,
                    triaged_at=datetime.now(timezone.utc))
    db_session.add(ticket)
    db_session.commit()
    author = db_session.exec(select(User).where(User.email == "admin@example.com")).one()
    assert [p.id for p in mentioned_in(db_session, ticket, "hi @gone and @active", author)] == [active.id]


def test_switching_it_on_in_the_afternoon_still_runs_it_that_night(db_session, monkeypatch):
    from app.models import ScheduledJob

    an_agent(db_session, "a@example.com", "oid-a")
    # the runner recorded the hourly wakes of the day on which the switch was off
    db_session.add(ScheduledJob(name=entra_check.JOB_NAME, last_success_at=datetime(2026, 11, 7, 13, 0, tzinfo=timezone.utc)))
    db_session.commit()
    configure(monkeypatch)
    assert is_due(db_session, datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(GraphClient, "account_enabled", lambda self, oid: True)
    run_entra_account_check(db_session, datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc))
    db_session.commit()
    assert not is_due(db_session, datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc))  # done for tonight
    assert is_due(db_session, datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc))


def test_a_failed_run_is_not_marked_done_so_it_is_tried_again(db_session, monkeypatch):
    configure(monkeypatch)
    an_agent(db_session, "a@example.com", "oid-a")
    monkeypatch.setattr(GraphClient, "account_enabled", lambda self, oid: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(RuntimeError):
        run_entra_account_check(db_session, datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc))
    db_session.rollback()
    assert is_due(db_session, datetime(2026, 11, 7, 4, 0, tzinfo=timezone.utc))


def test_the_last_active_super_admin_is_never_deactivated(db_session):
    only = an_agent(db_session, "boss@example.com", "oid-boss", role=UserRole.SUPER_ADMIN)
    result, _ = run(db_session, {"oid-boss": False})
    assert db_session.get(User, only.id).is_active and result.skipped == 1
    assert audits(db_session, "entra_deactivation_skipped")
    assert not audits(db_session, "deactivated_by_entra")


def test_two_super_admins_both_disabled_leave_one_active(db_session):
    first = an_agent(db_session, "a@example.com", "oid-a", role=UserRole.SUPER_ADMIN)
    second = an_agent(db_session, "b@example.com", "oid-b", role=UserRole.SUPER_ADMIN)
    run(db_session, {"oid-a": False, "oid-b": False})
    assert [db_session.get(User, first.id).is_active, db_session.get(User, second.id).is_active] == [False, True]


def test_everyone_not_found_looks_like_the_wrong_tenant_and_deactivates_nobody(db_session):
    people = [an_agent(db_session, f"a{n}@example.com", f"oid-{n}") for n in range(3)]
    result, _ = run(db_session, {f"oid-{n}": None for n in range(3)})
    assert all(db_session.get(User, p.id).is_active for p in people) and result.deactivated == 0
    assert audits(db_session, "entra_check_aborted")
    # a couple of leavers among people who are found is the normal case
    run(db_session, {"oid-0": None, "oid-1": True, "oid-2": True})
    assert not db_session.get(User, people[0].id).is_active


def test_a_run_gives_up_after_a_few_failures_with_no_answer_instead_of_waiting_on_every_timeout(db_session):
    for n in range(9):
        an_agent(db_session, f"a{n}@example.com", f"oid-{n}")
    result, fake = run(db_session, {f"oid-{n}": TimeoutError() for n in range(9)})
    assert result.gave_up and len(fake.asked) == 5


def test_an_inactive_person_is_sent_no_notifications(client, db_session):
    from app.notifications import notify

    team, active, inactive = world(client, db_session)
    assert notify(db_session, inactive, "assigned", None, "x", "someone@example.com") is None
    assert notify(db_session, active, "assigned", None, "x", "someone@example.com") is not None


def test_a_template_naming_an_agent_who_left_can_still_be_edited_otherwise(client, db_session):
    from app.models import RequestType
    from app.subrequests import save_template

    team, active, inactive = world(client, db_session)
    category = create_category(db_session, team)
    request_type = RequestType(name="Starter")
    db_session.add(request_type)
    db_session.commit()
    template = save_template(db_session, request_type, None, subject_pattern="Laptop", team_id=team.id,
                             category_id=category.id, assignee_id=active.id)
    active.is_active = False
    db_session.add(active)
    db_session.commit()
    save_template(db_session, request_type, template, subject_pattern="Laptop v2", team_id=team.id,
                  category_id=category.id, assignee_id=active.id)
    assert template.subject_pattern == "Laptop v2"


def test_break_glass_accounts_are_never_looked_up_or_deactivated(db_session):
    from app.breakglass import create_account

    create_account(db_session, "breakglass1")
    an_agent(db_session, "agent@example.com", "oid-a")
    result, fake = run(db_session, {"oid-a": True})
    assert fake.asked == ["oid-a"] and result.looked_at == 1
    assert db_session.exec(select(User).where(User.is_break_glass)).one().is_active
