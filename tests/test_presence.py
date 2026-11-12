"""2E: out-of-office detection. Graph is simulated: the real presence API is an Azure-only check."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from app import presence as presence_module
from app.config import settings
from app.graph_client import GraphClient
from app.jobs import JOBS
from app.models import Team, User, UserRole
from app.presence import STALE_AFTER, agent_label, is_out_of_office, notifiable_members, refresh_presence
from tests.conftest import add_membership, create_team, login_as, set_role

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def an_agent(db_session, email, name, team=None):
    team = team or db_session.exec(select(Team).where(Team.name == "Support")).first() or create_team(db_session, "Support")
    agent = User(email=email, display_name=name, role=UserRole.AGENT)
    db_session.add(agent)
    db_session.commit()
    add_membership(db_session, agent.id, team.id)
    return agent


class FakeGraph:
    """Stands in for Graph: object ids from a directory, and a presence reading per id."""

    def __init__(self, directory, away=()):
        self.directory = directory  # email -> object id
        self.away = set(away)  # object ids that are out of office
        self.lookups = []
        self.asked = []

    def object_id_for(self, email):
        self.lookups.append(email)
        return self.directory.get(email)

    def presences(self, ids):
        self.asked.append(list(ids))
        return [
            {"id": i, "availability": "Away", "outOfOfficeSettings": {"message": "", "isOutOfOffice": i in self.away}}
            for i in ids
        ]


def refresh(db_session, graph, now=NOW):
    count = refresh_presence(db_session, graph.object_id_for, graph.presences, now=now)
    db_session.commit()
    db_session.expire_all()
    return count


# ---- Reading presence ----------------------------------------------------------------------------------


def test_an_agent_graph_reports_out_of_office_is_marked_and_the_others_are_not(client, db_session):
    away = an_agent(db_session, "away@example.com", "Ann Away")
    here = an_agent(db_session, "here@example.com", "Hal Here")
    graph = FakeGraph({"away@example.com": "oid-away", "here@example.com": "oid-here"}, away={"oid-away"})

    assert refresh(db_session, graph) == 2

    assert is_out_of_office(db_session.get(User, away.id), NOW)
    assert not is_out_of_office(db_session.get(User, here.id), NOW)


def test_the_agent_is_unmarked_when_they_are_back(client, db_session):
    agent = an_agent(db_session, "ann@example.com", "Ann")
    graph = FakeGraph({"ann@example.com": "oid-ann"}, away={"oid-ann"})
    refresh(db_session, graph)
    assert is_out_of_office(db_session.get(User, agent.id), NOW)

    graph.away.clear()
    refresh(db_session, graph, now=NOW + timedelta(minutes=15))

    assert not is_out_of_office(db_session.get(User, agent.id), NOW + timedelta(minutes=15))


def test_an_agents_directory_id_is_looked_up_once_and_everyone_is_read_in_one_request(client, db_session):
    an_agent(db_session, "a@example.com", "A")
    an_agent(db_session, "b@example.com", "B")
    graph = FakeGraph({"a@example.com": "oid-a", "b@example.com": "oid-b"})

    refresh(db_session, graph)
    refresh(db_session, graph, now=NOW + timedelta(minutes=15))

    assert sorted(graph.lookups) == ["a@example.com", "b@example.com"]  # not asked again the second time
    assert [sorted(ids) for ids in graph.asked] == [["oid-a", "oid-b"]] * 2


def test_an_agent_the_directory_does_not_know_is_never_out_of_office(client, db_session):
    known = an_agent(db_session, "known@example.com", "Known")
    unknown = an_agent(db_session, "local@example.com", "Contractor")  # a local account has no Entra user
    graph = FakeGraph({"known@example.com": "oid-known"}, away={"oid-known"})

    assert refresh(db_session, graph) == 1

    assert is_out_of_office(db_session.get(User, known.id), NOW)
    assert not is_out_of_office(db_session.get(User, unknown.id), NOW)
    assert db_session.get(User, unknown.id).out_of_office_checked_at is None


def test_an_agent_graph_stops_reporting_is_no_longer_out_of_office(client, db_session):
    agent = an_agent(db_session, "ann@example.com", "Ann")
    graph = FakeGraph({"ann@example.com": "oid-ann"}, away={"oid-ann"})
    refresh(db_session, graph)
    graph.presences = lambda ids: []  # Graph returns nothing for them now

    refresh(db_session, graph, now=NOW + timedelta(minutes=15))

    assert not db_session.get(User, agent.id).out_of_office


def test_a_differently_cased_id_from_graph_still_matches(client, db_session):
    agent = an_agent(db_session, "ann@example.com", "Ann")
    graph = FakeGraph({"ann@example.com": "OID-Ann"})
    graph.presences = lambda ids: [{"id": "oid-ann", "outOfOfficeSettings": {"isOutOfOffice": True}}]

    refresh(db_session, graph)

    assert is_out_of_office(db_session.get(User, agent.id), NOW)


def test_end_users_are_not_looked_up(client, db_session):
    db_session.add(User(email="req@example.com", display_name="Requester", role=UserRole.END_USER))
    db_session.commit()
    graph = FakeGraph({"req@example.com": "oid-req"})

    assert refresh(db_session, graph) == 0
    assert graph.lookups == [] and graph.asked == []


def test_a_failed_directory_lookup_does_not_stop_the_others(client, db_session):
    first = an_agent(db_session, "first@example.com", "First")
    second = an_agent(db_session, "second@example.com", "Second")
    graph = FakeGraph({"second@example.com": "oid-second"}, away={"oid-second"})
    real = graph.object_id_for

    def flaky(email):
        if email == "first@example.com":
            raise httpx.ConnectError("down")
        return real(email)

    refresh_presence(db_session, flaky, graph.presences, now=NOW)
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(User, first.id).entra_object_id is None  # tried again next time
    assert is_out_of_office(db_session.get(User, second.id), NOW)


def test_an_old_reading_is_ignored_so_a_stopped_job_cannot_leave_someone_out_of_office(client, db_session):
    agent = an_agent(db_session, "ann@example.com", "Ann")
    refresh(db_session, FakeGraph({"ann@example.com": "oid-ann"}, away={"oid-ann"}))
    agent = db_session.get(User, agent.id)

    assert is_out_of_office(agent, NOW + STALE_AFTER - timedelta(minutes=1))
    assert not is_out_of_office(agent, NOW + STALE_AFTER + timedelta(minutes=1))


# ---- Where it is used ----------------------------------------------------------------------------------


def test_team_wide_notifications_skip_agents_who_are_out_of_office(client, db_session):
    away = an_agent(db_session, "away@example.com", "Ann Away")
    here = an_agent(db_session, "here@example.com", "Hal Here")
    refresh(db_session, FakeGraph({"away@example.com": "oid-away", "here@example.com": "oid-here"}, away={"oid-away"}))

    people = [db_session.get(User, away.id), db_session.get(User, here.id)]

    assert [u.email for u in notifiable_members(people, NOW)] == ["here@example.com"]


def test_the_assignee_pickers_label_an_agent_who_is_out_of_office(client, db_session):
    team = create_team(db_session, "Support")
    me = login_as(client, "me@example.com", "Me")
    set_role(db_session, me, UserRole.AGENT, team.id)
    an_agent(db_session, "away@example.com", "Ann Away", team)
    an_agent(db_session, "here@example.com", "Hal Here", team)
    # the reading is as of now, since the picker judges it against the real clock
    refresh(
        db_session,
        FakeGraph({"away@example.com": "oid-away", "here@example.com": "oid-here"}, away={"oid-away"}),
        now=datetime.now(timezone.utc),
    )

    page = client.get(f"/tickets/agents-for-team?team_id={team.id}").text

    assert "Ann Away (out of office)" in page
    assert "Hal Here</option>" in page and "Hal Here (out of office)" not in page


def test_a_label_says_nothing_for_someone_who_is_in(client, db_session):
    agent = an_agent(db_session, "here@example.com", "Hal Here")
    assert agent_label(agent) == "Hal Here"


# ---- The Graph calls (requests simulated; the real API is a staging check) -------------------------------


def graph_with(monkeypatch, handler):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: handler("POST", url, **kw))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: handler("GET", url, **kw))
    graph = GraphClient()
    monkeypatch.setattr(graph, "_get_access_token", lambda: "token")
    return graph


def test_presence_is_asked_for_in_batches_of_650(monkeypatch):
    requests = []

    def handler(method, url, **kw):
        requests.append((url, kw["json"]["ids"], kw["headers"]))
        return httpx.Response(200, json={"value": [{"id": i} for i in kw["json"]["ids"]]}, request=httpx.Request(method, url))

    graph = graph_with(monkeypatch, handler)
    ids = [f"id-{n}" for n in range(650 * 2 + 1)]

    readings = graph.presences(ids)

    assert [len(r[1]) for r in requests] == [650, 650, 1]
    assert requests[0][0].endswith("/communications/getPresencesByUserId")
    assert requests[0][2]["Authorization"] == "Bearer token"
    assert len(readings) == len(ids)


def test_a_graph_error_on_presence_is_raised_not_swallowed(monkeypatch):
    def handler(method, url, **kw):
        return httpx.Response(403, request=httpx.Request(method, url))

    with pytest.raises(httpx.HTTPStatusError):
        graph_with(monkeypatch, handler).presences(["id-1"])


def test_a_slash_in_an_email_cannot_change_the_graph_route(monkeypatch):
    seen = []

    def handler(method, url, **kw):
        seen.append(url)
        return httpx.Response(200, json={"id": "x"}, request=httpx.Request(method, url))

    graph_with(monkeypatch, handler).object_id_for("a/b@example.com")

    assert seen[0].endswith("/users/a%2Fb%40example.com")


def test_a_user_graph_does_not_know_has_no_object_id(monkeypatch):
    def handler(method, url, **kw):
        status = 404 if "nobody" in url else 200
        return httpx.Response(status, json={"id": "oid-1"}, request=httpx.Request(method, url))

    graph = graph_with(monkeypatch, handler)

    assert graph.object_id_for("ann@example.com") == "oid-1"
    assert graph.object_id_for("nobody@example.com") is None


# ---- The worker job ------------------------------------------------------------------------------------


def test_the_worker_runs_the_presence_job(monkeypatch):
    [job] = [j for j in JOBS if j.name == "presence"]
    assert job.every_seconds == 15 * 60 and job.every_seconds < STALE_AFTER.total_seconds()


@pytest.mark.parametrize("enabled, configured, runs", [(False, True, 0), (True, False, 0), (True, True, 1)])
def test_the_job_reads_presence_only_when_switched_on_and_graph_is_set_up(
    db_session, monkeypatch, enabled, configured, runs
):
    from app import jobs

    calls = []
    monkeypatch.setattr(jobs, "refresh_presence", lambda session: calls.append(session))
    monkeypatch.setattr(settings, "presence_enabled", enabled)
    monkeypatch.setattr(settings, "graph_tenant_id", "t" if configured else "")
    monkeypatch.setattr(settings, "graph_client_id", "c" if configured else "")
    monkeypatch.setattr(settings, "graph_client_secret", "s" if configured else "")

    jobs.presence(db_session)

    assert len(calls) == runs


def test_two_users_cannot_share_one_directory_id_but_many_can_have_none(client, db_session):
    from sqlalchemy.exc import IntegrityError

    for n in range(3):  # no id yet: any number of users
        db_session.add(User(email=f"none{n}@example.com", display_name="None", role=UserRole.AGENT))
    db_session.add(User(email="one@example.com", display_name="One", role=UserRole.AGENT, entra_object_id="oid-1"))
    db_session.commit()

    db_session.add(User(email="two@example.com", display_name="Two", role=UserRole.AGENT, entra_object_id="oid-1"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_an_out_of_office_agent_can_still_be_assigned(client, db_session):
    team = create_team(db_session, "Support")
    me = login_as(client, "me@example.com", "Me")
    set_role(db_session, me, UserRole.AGENT, team.id)
    away = an_agent(db_session, "away@example.com", "Ann Away", team)
    refresh(db_session, FakeGraph({"away@example.com": "oid-away"}, away={"oid-away"}), now=datetime.now(timezone.utc))
    assert is_out_of_office(db_session.get(User, away.id))
    # the picker still offers them (a label, never a block)
    page = client.get(f"/tickets/agents-for-team?team_id={team.id}").text
    assert f'value="{away.id}"' in page


def test_the_flag_defaults_to_off_for_everyone(client, db_session):
    person = User(email="new@example.com", display_name="New", role=UserRole.AGENT)
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)
    assert person.out_of_office is False and person.out_of_office_checked_at is None
    assert presence_module.is_out_of_office(person) is False


# ---- Retry bookkeeping and is_active (4B-4) ----------------------------------------------------------------


def test_an_agent_graph_does_not_know_is_not_asked_again_until_the_retry_interval_passes(client, db_session):
    an_agent(db_session, "ghost@example.com", "Gus Ghost")
    graph = FakeGraph({})

    refresh(db_session, graph)
    refresh(db_session, graph, now=NOW + timedelta(minutes=30))
    assert graph.lookups == ["ghost@example.com"]

    refresh(db_session, graph, now=NOW + timedelta(minutes=61))  # the default retry wait is an hour (#122)
    assert graph.lookups == ["ghost@example.com"] * 2


def test_a_directory_error_counts_as_a_failed_lookup(client, db_session):
    an_agent(db_session, "oops@example.com", "Oli Oops")
    calls = []

    def broken(email):
        calls.append(email)
        raise RuntimeError("boom")

    for minutes in (0, 30):
        refresh_presence(db_session, broken, FakeGraph({}).presences, now=NOW + timedelta(minutes=minutes))
        db_session.commit()
    assert calls == ["oops@example.com"]  # an error waits too, as the issue asks


def test_the_retry_interval_is_a_setting(client, db_session):
    from app.settings_store import PRESENCE_RETRY_MINUTES, set_setting

    an_agent(db_session, "ghost@example.com", "Gus Ghost")
    set_setting(db_session, PRESENCE_RETRY_MINUTES, "10")
    db_session.commit()
    graph = FakeGraph({})

    refresh(db_session, graph)
    refresh(db_session, graph, now=NOW + timedelta(minutes=11))

    assert len(graph.lookups) == 2


def test_an_agent_with_no_presence_reading_waits_and_a_later_reading_clears_the_wait(client, db_session):
    agent = an_agent(db_session, "ann@example.com", "Ann")
    an_agent(db_session, "hal@example.com", "Hal")

    class Skips(FakeGraph):
        def presences(self, ids):  # answers for Hal only
            self.asked.append(list(ids))
            return [{"id": "oid-hal", "outOfOfficeSettings": {"isOutOfOffice": False}}]

    silent = Skips({"ann@example.com": "oid-ann", "hal@example.com": "oid-hal"})
    refresh(db_session, silent)
    assert db_session.get(User, agent.id).last_presence_lookup_at is not None
    refresh(db_session, silent, now=NOW + timedelta(minutes=15))
    assert silent.asked[1] == ["oid-hal"]  # Ann is still waiting

    graph = FakeGraph({"ann@example.com": "oid-ann"}, away={"oid-ann"})
    refresh(db_session, graph, now=NOW + timedelta(days=2))
    after = db_session.get(User, agent.id)
    assert after.last_presence_lookup_at is None and is_out_of_office(after, NOW + timedelta(days=2))


def test_an_empty_answer_for_everyone_is_a_graph_gap_and_starts_no_wait(client, db_session):
    agent = an_agent(db_session, "ann@example.com", "Ann")

    class Empty(FakeGraph):
        def presences(self, ids):
            self.asked.append(list(ids))
            return []

    graph = Empty({"ann@example.com": "oid-ann"})
    refresh(db_session, graph)
    refresh(db_session, graph, now=NOW + timedelta(minutes=15))

    assert len(graph.asked) == 2
    assert db_session.get(User, agent.id).last_presence_lookup_at is None


def test_inactive_agents_are_never_polled_and_nothing_else_about_them_changes(client, db_session):
    gone = an_agent(db_session, "gone@example.com", "Gina Gone")
    an_agent(db_session, "here@example.com", "Hal Here")
    gone.is_active = False
    db_session.add(gone)
    db_session.commit()
    graph = FakeGraph({"gone@example.com": "oid-gone", "here@example.com": "oid-here"})

    assert refresh(db_session, graph) == 1

    assert graph.lookups == ["here@example.com"]
    assert graph.asked == [["oid-here"]]
    assert db_session.get(User, gone.id).role == UserRole.AGENT  # phase 4: polling is the only effect


def test_an_agent_reactivated_is_polled_again(client, db_session):
    agent = an_agent(db_session, "ann@example.com", "Ann")
    agent.is_active = False
    db_session.add(agent)
    db_session.commit()
    graph = FakeGraph({"ann@example.com": "oid-ann"}, away={"oid-ann"})
    refresh(db_session, graph)
    assert graph.lookups == []

    agent.is_active = True
    db_session.add(agent)
    db_session.commit()
    refresh(db_session, graph)
    assert is_out_of_office(db_session.get(User, agent.id), NOW)


def test_an_admin_can_deactivate_and_reactivate_a_user_and_it_is_audited(client, db_session):
    from app.models import AuditLog

    agent = an_agent(db_session, "ann@example.com", "Ann")
    login_as(client, "admin@example.com")
    assert "Deactivate" in client.get(f"/admin/users?user={agent.id}").text

    client.post(f"/admin/users/{agent.id}/active", data={"is_active": "false"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(User, agent.id).is_active is False
    assert "Activate" in client.get(f"/admin/users?user={agent.id}").text

    client.post(f"/admin/users/{agent.id}/active", data={"is_active": "true"}, follow_redirects=False)
    db_session.expire_all()
    assert db_session.get(User, agent.id).is_active is True
    actions = [e.action for e in db_session.exec(select(AuditLog).where(AuditLog.entity_id == agent.id))]
    assert actions == ["deactivated", "activated"]


def test_only_a_super_admin_can_change_is_active(client, db_session):
    agent = an_agent(db_session, "ann@example.com", "Ann")
    login_as(client, "ann@example.com")
    assert client.post(f"/admin/users/{agent.id}/active", data={"is_active": "false"}).status_code == 403
