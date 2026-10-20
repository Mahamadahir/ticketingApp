"""3C: keeping the Graph mail subscription alive and catching up on missed mail. Graph is a fake
(real subscriptions need a public HTTPS address, so staging proves those); what is tested here is our
side of every rule: what is created, when it is renewed, that two runs never make two subscriptions,
that the delta link moves only with the ids handed over, and that nothing handed over twice."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import Session, select

from app import graph_client as graph_module
from app import subscriptions
from app.config import Settings, mail_sync_errors, settings
from app.database import engine
from app.jobs import JOBS, graph_subscription, mail_catch_up
from app.models import GraphNotification, GraphSubscription, MailSyncState, Setting
from app.settings_store import (
    GRAPH_RENEW_MARGIN_MINUTES,
    GRAPH_SUBSCRIPTION_MINUTES,
    MAIL_BACKFILL_DAYS,
    set_setting,
)
from app.subscriptions import (
    MAX_PAGES_PER_RUN,
    SubscriptionError,
    catch_up,
    health_warnings,
    keep_subscription_alive,
    parse_graph_time,
    record_for_processing,
)
from tests.conftest import login_as

URL = "https://hooks.example.org/webhooks/graph/notifications"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def http_error(status, code="Forbidden"):
    request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/secret-path?token=abc")
    response = httpx.Response(status, json={"error": {"code": code, "message": "x"}}, request=request)
    return httpx.HTTPStatusError("boom https://graph.microsoft.com/v1.0/secret-path?token=abc", request=request, response=response)


class FakeGraph:
    """Just enough of Graph: subscriptions that expire, and an inbox delta query served from queued pages."""

    def __init__(self):
        self.subs = {}  # id -> dict as Graph lists it
        self.calls = []
        self.counter = 0
        self.fail = {}  # method name -> exception raised by it
        self.pages = []  # delta pages served in order
        self.delta_requests = []  # (link, since)
        self.granted = None  # expiry Graph "grants" instead of the one asked for

    def _check(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    def add_remote(self, resource, url, expires, sub_id=None):
        self.counter += 1
        sub_id = sub_id or f"sub-{self.counter}"
        self.subs[sub_id] = {
            "id": sub_id, "resource": resource, "notificationUrl": url,
            "expirationDateTime": expires.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        }
        return sub_id

    def list_subscriptions(self):
        self._check("list")
        return [dict(s) for s in self.subs.values()]

    def create_subscription(self, resource, notification_url, lifecycle_url, client_state, expires_at):
        self._check("create")
        self.created_with = dict(
            resource=resource, notification_url=notification_url, lifecycle_url=lifecycle_url,
            client_state=client_state, expires_at=expires_at,
        )
        sub_id = self.add_remote(resource, notification_url, self.granted or expires_at)
        return dict(self.subs[sub_id])

    def renew_subscription(self, subscription_id, expires_at):
        self._check("renew")
        if subscription_id not in self.subs:
            raise http_error(404, "ResourceNotFound")
        self.subs[subscription_id]["expirationDateTime"] = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.renewed_to = expires_at
        return dict(self.subs[subscription_id])

    def delete_subscription(self, subscription_id):
        self._check("delete")
        self.subs.pop(subscription_id, None)

    def inbox_delta_page(self, link, since=None):
        self._check("delta")
        self.delta_requests.append((link, since))
        return self.pages.pop(0)

    def count(self, name):
        return self.calls.count(name)


@pytest.fixture
def mail(monkeypatch):
    monkeypatch.setattr(settings, "graph_tenant_id", "t")
    monkeypatch.setattr(settings, "graph_client_id", "c")
    monkeypatch.setattr(settings, "graph_client_secret", "s")
    monkeypatch.setattr(settings, "graph_shared_mailbox", "support@example.org")
    monkeypatch.setattr(settings, "graph_notification_url", URL)
    monkeypatch.setattr(settings, "graph_webhook_client_state", "state-xyz")
    return FakeGraph()


def resource():
    return subscriptions.subscription_resource()


def row(db_session):
    db_session.expire_all()
    return db_session.exec(select(GraphSubscription)).one()


def run(db_session, graph, now=NOW):
    outcome = keep_subscription_alive(db_session, graph, now)
    db_session.commit()
    return outcome


def set_minutes(db_session, key, value):
    set_setting(db_session, key, str(value))
    db_session.commit()


# ---- Creating -------------------------------------------------------------------------------------


def test_nothing_happens_when_not_configured(db_session, monkeypatch):
    monkeypatch.setattr(settings, "graph_notification_url", "")
    graph = FakeGraph()
    assert keep_subscription_alive(db_session, graph, NOW).action == "off"
    assert graph.calls == [] and db_session.exec(select(GraphSubscription)).all() == []


def test_a_missing_subscription_is_created_for_the_inbox_with_the_lifecycle_address(db_session, mail):
    outcome = run(db_session, mail)

    assert outcome.action == "created"
    made = mail.created_with
    assert made["resource"] == "users/support@example.org/mailFolders('Inbox')/messages"
    assert made["notification_url"] == URL and made["lifecycle_url"] == URL  # lifecycle can only be set now
    assert made["client_state"] == "state-xyz"
    stored = row(db_session)
    assert stored.subscription_id in mail.subs and stored.expires_at is not None
    assert stored.last_error is None and stored.last_renewed_at == NOW


def test_the_client_state_is_never_stored(db_session, mail):
    run(db_session, mail)
    stored = row(db_session)
    assert "state-xyz" not in " ".join(str(v) for v in stored.model_dump().values())
    assert stored.client_state_fingerprint == subscriptions.client_state_fingerprint("state-xyz")


def test_the_expiry_graph_grants_is_the_one_kept(db_session, mail):
    mail.granted = NOW + timedelta(days=2)
    run(db_session, mail)
    assert row(db_session).expires_at == NOW + timedelta(days=2)


def test_the_lifetime_asked_for_is_the_setting_and_never_above_graphs_limit(db_session, mail):
    run(db_session, mail)
    assert mail.created_with["expires_at"] == NOW + timedelta(minutes=10000)

    set_minutes(db_session, GRAPH_SUBSCRIPTION_MINUTES, 99999)
    mail.subs.clear()
    run(db_session, mail)
    assert mail.created_with["expires_at"] == NOW + timedelta(minutes=subscriptions.MAX_LIFETIME_MINUTES)


# ---- No duplicates --------------------------------------------------------------------------------


def test_running_again_or_restarting_does_not_create_a_second_subscription(db_session, mail):
    run(db_session, mail)
    for _ in range(3):
        assert run(db_session, mail, NOW + timedelta(minutes=5)).action == "ok"
    assert mail.count("create") == 1 and len(mail.subs) == 1


def test_a_run_that_finds_the_row_locked_by_another_worker_does_nothing(db_session, mail):
    run(db_session, mail)
    mail.calls.clear()
    with Session(engine) as other:  # another replica, in the middle of its own check
        other.exec(select(GraphSubscription).with_for_update())
        outcome = keep_subscription_alive(db_session, mail, NOW)
    db_session.rollback()
    assert outcome.action == "busy" and mail.calls == []


def test_two_first_runs_in_a_row_from_different_sessions_make_one_subscription(db_session, mail):
    with Session(engine) as first, Session(engine) as second:
        keep_subscription_alive(first, mail, NOW)
        first.commit()
        keep_subscription_alive(second, mail, NOW)
        second.commit()
    assert mail.count("create") == 1


def test_if_our_row_is_lost_the_old_subscription_is_removed_not_duplicated(db_session, mail):
    old = mail.add_remote(resource(), URL, NOW + timedelta(days=5))  # ours at Graph, unknown to the database
    run(db_session, mail)
    assert mail.count("create") == 1
    assert old not in mail.subs and list(mail.subs) == [row(db_session).subscription_id]


def test_other_subscriptions_are_left_alone(db_session, mail):
    elsewhere = mail.add_remote(resource(), "https://other.example.org/hook", NOW + timedelta(days=5))
    other_resource = mail.add_remote("users/someone@example.org/messages", URL, NOW + timedelta(days=5))
    run(db_session, mail)
    assert elsewhere in mail.subs and other_resource in mail.subs


# ---- Renewing -------------------------------------------------------------------------------------


def test_it_is_renewed_when_less_than_the_margin_is_left_and_not_before(db_session, mail):
    run(db_session, mail)
    sub_id = row(db_session).subscription_id

    mail.subs[sub_id]["expirationDateTime"] = (NOW + timedelta(hours=24, minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert run(db_session, mail, NOW).action == "ok" and mail.count("renew") == 0

    mail.subs[sub_id]["expirationDateTime"] = (NOW + timedelta(hours=23, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert run(db_session, mail, NOW).action == "renewed" and mail.count("renew") == 1
    assert mail.renewed_to == NOW + timedelta(minutes=10000)
    assert row(db_session).expires_at == NOW + timedelta(minutes=10000)


def test_the_margin_is_a_setting(db_session, mail):
    run(db_session, mail)
    set_minutes(db_session, GRAPH_RENEW_MARGIN_MINUTES, 60)
    sub_id = row(db_session).subscription_id
    mail.subs[sub_id]["expirationDateTime"] = (NOW + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert run(db_session, mail).action == "ok"
    mail.subs[sub_id]["expirationDateTime"] = (NOW + timedelta(minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert run(db_session, mail).action == "renewed"


def test_a_margin_longer_than_the_lifetime_cannot_make_every_run_renew(db_session, mail):
    set_minutes(db_session, GRAPH_RENEW_MARGIN_MINUTES, 100000)
    run(db_session, mail)
    assert run(db_session, mail, NOW + timedelta(minutes=5)).action == "ok"


def test_renewing_a_subscription_graph_no_longer_has_creates_a_new_one(db_session, mail):
    run(db_session, mail)
    sub_id = row(db_session).subscription_id
    mail.subs[sub_id]["expirationDateTime"] = (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    real_list = mail.list_subscriptions

    def listed_then_gone():
        listed = real_list()
        mail.subs.pop(sub_id)  # it vanishes between the list and the PATCH
        return listed

    mail.list_subscriptions = listed_then_gone
    assert run(db_session, mail).action == "created"
    mail.list_subscriptions = real_list
    assert row(db_session).subscription_id != sub_id and len(mail.subs) == 1


def test_an_expired_or_vanished_subscription_is_recreated(db_session, mail):
    run(db_session, mail)
    mail.subs.clear()
    assert run(db_session, mail, NOW + timedelta(days=9)).action == "created"
    assert len(mail.subs) == 1 and mail.count("create") == 2


def test_a_changed_client_state_recreates_the_subscription(db_session, mail, monkeypatch):
    run(db_session, mail)
    first = row(db_session).subscription_id
    monkeypatch.setattr(settings, "graph_webhook_client_state", "a-new-state")
    assert run(db_session, mail).action == "created"
    assert first not in mail.subs and len(mail.subs) == 1
    assert mail.created_with["client_state"] == "a-new-state"


# ---- Lifecycle notifications ----------------------------------------------------------------------


def lifecycle(db_session, event, subscription_id=None):
    note = GraphNotification(lifecycle_event=event, subscription_id=subscription_id)
    db_session.add(note)
    db_session.commit()
    return note.id


def handled(db_session, note_id):
    db_session.expire_all()
    return db_session.get(GraphNotification, note_id).processed_at is not None


def test_reauthorization_required_renews_by_patch_even_when_not_low(db_session, mail):
    run(db_session, mail)
    sub_id = row(db_session).subscription_id
    note = lifecycle(db_session, "reauthorizationRequired", sub_id)

    outcome = run(db_session, mail, NOW + timedelta(minutes=30))

    assert outcome.action == "renewed" and handled(db_session, note)
    assert "reauthorize" not in " ".join(mail.calls)


def test_a_reauthorization_request_within_ten_minutes_of_a_renewal_is_not_acted_on(db_session, mail):
    run(db_session, mail)  # created now
    note = lifecycle(db_session, "reauthorizationRequired", row(db_session).subscription_id)
    mail.calls.clear()
    assert run(db_session, mail, NOW + timedelta(minutes=9)).action == "ok"
    assert "renew" not in mail.calls and handled(db_session, note)


def test_subscription_removed_recreates_and_asks_for_a_catch_up(db_session, mail):
    run(db_session, mail)
    sub_id = row(db_session).subscription_id
    note = lifecycle(db_session, "subscriptionRemoved", sub_id)

    outcome = run(db_session, mail, NOW + timedelta(minutes=1))

    assert (outcome.action, outcome.sync_wanted) == ("created", True)
    assert sub_id not in mail.subs and len(mail.subs) == 1 and handled(db_session, note)


def test_missed_asks_for_a_catch_up_and_changes_nothing_else(db_session, mail):
    run(db_session, mail)
    note = lifecycle(db_session, "missed", row(db_session).subscription_id)
    outcome = run(db_session, mail, NOW + timedelta(minutes=1))
    assert (outcome.action, outcome.sync_wanted) == ("ok", True) and handled(db_session, note)


def test_a_lifecycle_notice_from_an_old_subscription_changes_nothing(db_session, mail):
    run(db_session, mail)
    note = lifecycle(db_session, "subscriptionRemoved", "an-older-subscription")
    outcome = run(db_session, mail, NOW + timedelta(minutes=30))
    assert (outcome.action, outcome.sync_wanted) == ("ok", False) and handled(db_session, note)


def test_change_notifications_are_left_for_inbound_mail(db_session, mail):
    db_session.add(GraphNotification(change_type="created", resource_id="m1"))
    db_session.commit()
    run(db_session, mail)
    assert db_session.exec(select(GraphNotification)).one().processed_at is None


def test_a_failed_attempt_leaves_the_lifecycle_notices_waiting(db_session, mail):
    run(db_session, mail)
    note = lifecycle(db_session, "subscriptionRemoved", row(db_session).subscription_id)
    mail.fail["create"] = http_error(503, "ServiceUnavailable")
    with pytest.raises(SubscriptionError):
        run(db_session, mail, NOW + timedelta(minutes=1))
    assert not handled(db_session, note)


def test_a_real_webhook_lifecycle_post_is_picked_up(client, db_session, mail):
    run(db_session, mail)
    sub_id = row(db_session).subscription_id
    response = client.post(
        "/webhooks/graph/notifications",
        json={"value": [{"subscriptionId": sub_id, "clientState": "state-xyz", "lifecycleEvent": "missed"}]},
    )
    assert response.status_code == 202
    assert run(db_session, mail, NOW + timedelta(minutes=1)).sync_wanted is True


# ---- Failure --------------------------------------------------------------------------------------


def test_a_graph_failure_is_recorded_and_raised_without_secrets(db_session, mail):
    mail.fail["create"] = http_error(403, "ErrorAccessDenied")
    with pytest.raises(SubscriptionError) as raised:
        run(db_session, mail)

    assert str(raised.value) == "Graph answered 403 (ErrorAccessDenied)"
    stored = row(db_session)
    assert stored.last_error == "Graph answered 403 (ErrorAccessDenied)" and stored.last_error_at is not None
    assert stored.subscription_id is None
    assert "secret-path" not in stored.last_error and raised.value.__cause__ is None


def test_the_next_good_run_clears_the_error(db_session, mail):
    mail.fail["create"] = http_error(503)
    with pytest.raises(SubscriptionError):
        run(db_session, mail)
    del mail.fail["create"]
    run(db_session, mail)
    assert row(db_session).last_error is None


def test_failing_to_list_does_not_create_anything(db_session, mail):
    mail.fail["list"] = http_error(500)
    with pytest.raises(SubscriptionError):
        run(db_session, mail)
    assert mail.count("create") == 0


def test_the_job_fails_loudly_so_the_worker_logs_it(db_session, mail, monkeypatch, caplog):
    monkeypatch.setattr(settings, "graph_subscription_enabled", True)
    monkeypatch.setattr(subscriptions, "_graph", lambda graph=None: mail)
    mail.fail["list"] = http_error(401, "InvalidAuthenticationToken")
    with pytest.raises(SubscriptionError):
        graph_subscription(db_session)
    assert "Graph answered 401" in caplog.text


# ---- Health ---------------------------------------------------------------------------------------


def test_health_is_unchanged_when_nothing_is_set_up(client):
    assert client.get("/health").json() == {"status": "ok", "database": "ok"}


def test_health_warns_when_the_subscription_is_close_to_expiring_but_stays_ok(client, db_session, mail):
    run(db_session, mail, datetime.now(timezone.utc))
    assert client.get("/health").json() == {"status": "ok", "database": "ok"}

    stored = row(db_session)
    stored.expires_at = datetime.now(timezone.utc) + timedelta(hours=23)
    db_session.add(stored)
    db_session.commit()

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["warnings"] == ["Mail subscription expires soon and has not been renewed"]


def test_health_warns_when_expired_or_failing(client, db_session, mail):
    run(db_session, mail, datetime.now(timezone.utc))
    stored = row(db_session)
    stored.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    stored.last_error = "Graph answered 403"
    db_session.add(stored)
    db_session.add(MailSyncState(folder=subscriptions.mail_folder(), last_error="Graph answered 500"))
    db_session.commit()
    assert client.get("/health").json()["warnings"] == [
        "Mail subscription has expired",
        "Mail subscription: the last check failed",
        "Mail catch-up: the last run failed",
    ]


def test_health_does_not_say_how_the_failure_happened(client, db_session, mail):
    run(db_session, mail, datetime.now(timezone.utc))
    stored = row(db_session)
    stored.last_error = "Graph answered 403 (ErrorAccessDenied)"
    db_session.add(stored)
    db_session.commit()
    assert "403" not in client.get("/health").text


# ---- The catch-up ---------------------------------------------------------------------------------


def page(ids, delta=None, next_link=None):
    body = {"value": [{"id": i, "receivedDateTime": "2026-10-20T10:00:00Z"} for i in ids]}
    if next_link:
        body["@odata.nextLink"] = next_link
    if delta:
        body["@odata.deltaLink"] = delta
    return body


GRAPH_URL = "https://graph.microsoft.com/v1.0/users/support@example.org/mailFolders/inbox/messages/delta"


def sync_state(db_session):
    db_session.expire_all()
    return db_session.exec(select(MailSyncState)).one()


def notified(db_session):
    db_session.expire_all()
    return db_session.exec(select(GraphNotification).order_by(GraphNotification.id)).all()


def test_the_first_catch_up_looks_back_the_backfill_window_and_stores_the_delta_link(db_session, mail):
    mail.pages = [page(["a", "b"], delta=GRAPH_URL + "?$deltatoken=1")]

    handed = catch_up(db_session, mail, now=NOW)
    db_session.commit()

    assert handed == 2
    assert mail.delta_requests == [(None, NOW - timedelta(days=3))]
    state = sync_state(db_session)
    assert state.delta_link == GRAPH_URL + "?$deltatoken=1" and state.last_synced_at == NOW
    assert [n.resource_id for n in notified(db_session)] == ["a", "b"]
    assert notified(db_session)[0].resource == "users/support@example.org/messages/a"


def test_the_backfill_window_is_a_setting(db_session, mail):
    set_minutes(db_session, MAIL_BACKFILL_DAYS, 10)
    mail.pages = [page([], delta="d")]
    catch_up(db_session, mail, now=NOW)
    assert mail.delta_requests == [(None, NOW - timedelta(days=10))]


def test_later_runs_start_from_the_stored_delta_link(db_session, mail):
    mail.pages = [page(["a"], delta=GRAPH_URL + "?$deltatoken=1")]
    catch_up(db_session, mail, now=NOW)
    db_session.commit()

    mail.pages = [page(["b"], delta=GRAPH_URL + "?$deltatoken=2")]
    catch_up(db_session, mail, now=NOW + timedelta(minutes=5))
    db_session.commit()

    assert mail.delta_requests[1] == (GRAPH_URL + "?$deltatoken=1", None)
    assert sync_state(db_session).delta_link == GRAPH_URL + "?$deltatoken=2"


def test_catching_up_twice_over_the_same_mail_hands_over_nothing_new(db_session, mail):
    mail.pages = [page(["a", "b"], delta="d1")]
    assert catch_up(db_session, mail, now=NOW) == 2
    db_session.commit()
    mail.pages = [page(["a", "b"], delta="d2")]  # changed again (a flag), so Graph lists them again
    assert catch_up(db_session, mail, now=NOW) == 0
    db_session.commit()
    assert len(notified(db_session)) == 2


def test_mail_the_webhook_already_delivered_is_not_handed_over_again(db_session, mail):
    db_session.add(GraphNotification(change_type="created", resource_id="seen", processed_at=NOW))
    db_session.commit()
    mail.pages = [page(["seen", "new"], delta="d")]
    assert catch_up(db_session, mail, now=NOW) == 1
    db_session.commit()
    assert sorted(n.resource_id for n in notified(db_session)) == ["new", "seen"]


def test_a_repeated_id_in_one_run_is_handed_over_once(db_session, mail):
    mail.pages = [page(["a", "a"], next_link=GRAPH_URL + "?$skiptoken=1"), page(["a"], delta="d")]
    assert catch_up(db_session, mail, now=NOW) == 1


def test_deleted_messages_are_not_handed_over(db_session, mail):
    mail.pages = [{"value": [{"id": "gone", "@removed": {"reason": "deleted"}}, {"id": "kept"}], "@odata.deltaLink": "d"}]
    catch_up(db_session, mail, now=NOW)
    assert [n.resource_id for n in notified(db_session)] == ["kept"]


def test_every_page_is_followed_before_the_delta_link_is_stored(db_session, mail):
    mail.pages = [
        page(["a"], next_link=GRAPH_URL + "?$skiptoken=1"),
        page(["b"], next_link=GRAPH_URL + "?$skiptoken=2"),
        page(["c"], delta="final"),
    ]
    assert catch_up(db_session, mail, now=NOW) == 3
    db_session.commit()
    assert [r[0] for r in mail.delta_requests] == [None, GRAPH_URL + "?$skiptoken=1", GRAPH_URL + "?$skiptoken=2"]
    state = sync_state(db_session)
    assert state.delta_link == "final" and state.resume_link is None


def test_a_long_run_is_cut_short_and_resumes_where_it_stopped(db_session, mail):
    mail.pages = [page([f"m{i}"], next_link=GRAPH_URL + f"?$skiptoken={i}") for i in range(MAX_PAGES_PER_RUN)]
    catch_up(db_session, mail, now=NOW)
    db_session.commit()
    state = sync_state(db_session)
    assert state.resume_link == GRAPH_URL + f"?$skiptoken={MAX_PAGES_PER_RUN - 1}"
    assert state.delta_link is None and state.last_synced_at is None

    mail.pages = [page(["last"], delta="done")]
    catch_up(db_session, mail, now=NOW)
    db_session.commit()
    assert mail.delta_requests[-1][0] == GRAPH_URL + f"?$skiptoken={MAX_PAGES_PER_RUN - 1}"
    state = sync_state(db_session)
    assert state.delta_link == "done" and state.resume_link is None


def test_a_failure_keeps_the_old_delta_link_and_the_ids_are_found_next_time(db_session, mail):
    mail.pages = [page(["a"], delta="d1")]
    catch_up(db_session, mail, now=NOW)
    db_session.commit()

    mail.pages = [page(["b"], next_link=GRAPH_URL + "?$skiptoken=1")]
    mail.fail_second = True
    original = mail.inbox_delta_page

    def second_fails(link, since=None):
        if link == GRAPH_URL + "?$skiptoken=1":
            raise http_error(503, "ServiceUnavailable")
        return original(link, since)

    mail.inbox_delta_page = second_fails
    with pytest.raises(SubscriptionError):
        catch_up(db_session, mail, now=NOW + timedelta(minutes=5))

    state = sync_state(db_session)
    assert state.delta_link == "d1" and state.resume_link is None  # nothing moved
    assert state.last_error == "Graph answered 503 (ServiceUnavailable)"
    assert [n.resource_id for n in notified(db_session)] == ["a"]  # b is not recorded either: found again next run

    mail.inbox_delta_page = original
    mail.pages = [page(["b"], delta="d2")]
    assert catch_up(db_session, mail, now=NOW + timedelta(minutes=10)) == 1
    db_session.commit()
    assert sync_state(db_session).last_error is None


def test_graph_forgetting_our_place_restarts_from_the_backfill_window(db_session, mail):
    mail.pages = [page(["a"], delta="stale-link")]
    catch_up(db_session, mail, now=NOW)
    db_session.commit()

    served = [http_error(410, "syncStateNotFound"), page(["a", "b"], delta="fresh")]

    def gone_then_fresh(link, since=None):
        mail.delta_requests.append((link, since))
        item = served.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    mail.inbox_delta_page = gone_then_fresh
    later = NOW + timedelta(days=30)
    assert catch_up(db_session, mail, now=later) == 1  # "a" was collected before
    db_session.commit()
    assert mail.delta_requests[-2:] == [("stale-link", None), (None, later - timedelta(days=3))]
    assert sync_state(db_session).delta_link == "fresh"


def test_a_page_with_no_links_is_an_error_not_a_silent_stop(db_session, mail):
    mail.pages = [page(["a"])]
    with pytest.raises(SubscriptionError):
        catch_up(db_session, mail, now=NOW)
    assert notified(db_session) == [] and sync_state(db_session).delta_link is None


def test_a_replacement_intake_receives_the_ids_and_source(db_session, mail):
    calls = []

    def intake(session, ids, source):
        calls.append((list(ids), source))
        return len(ids)

    mail.pages = [page(["a", "b"], delta="d")]
    assert catch_up(db_session, mail, intake=intake, now=NOW) == 2
    assert calls == [(["a", "b"], "catch-up")] and notified(db_session) == []


def test_catching_up_waits_for_a_run_that_holds_the_state(db_session, mail):
    mail.pages = [page(["a"], delta="d")]
    catch_up(db_session, mail, now=NOW)
    db_session.commit()
    mail.calls.clear()
    with Session(engine) as other:
        other.exec(select(MailSyncState).with_for_update())
        assert catch_up(db_session, mail, now=NOW) == 0
    db_session.rollback()
    assert mail.calls == []


def test_the_default_intake_is_idempotent(db_session):
    assert record_for_processing(db_session, ["x", "y", "x"], "catch-up") == 2
    db_session.commit()
    assert record_for_processing(db_session, ["x", "z"], "catch-up") == 1
    assert record_for_processing(db_session, [], "catch-up") == 0


# ---- Jobs, settings, client -----------------------------------------------------------------------


def test_the_jobs_are_registered_every_five_minutes():
    names = {job.name: job.every_seconds for job in JOBS}
    assert names["graph-subscription"] == 300 and names["mail-catch-up"] == 300


def test_the_jobs_do_nothing_while_switched_off(db_session, mail):
    graph_subscription(db_session)
    mail_catch_up(db_session)
    assert db_session.exec(select(GraphSubscription)).all() == []
    assert db_session.exec(select(MailSyncState)).all() == []


def test_a_lifecycle_catch_up_request_runs_the_catch_up_too(db_session, mail, monkeypatch):
    monkeypatch.setattr(settings, "graph_subscription_enabled", True)
    monkeypatch.setattr(settings, "mail_catch_up_enabled", True)
    monkeypatch.setattr(subscriptions, "_graph", lambda graph=None: mail)
    graph_subscription(db_session)
    db_session.commit()
    mail.pages = [page(["lost"], delta="d")]
    lifecycle(db_session, "missed", row(db_session).subscription_id)

    graph_subscription(db_session)
    db_session.commit()

    assert [n.resource_id for n in notified(db_session) if n.lifecycle_event is None] == ["lost"]


def test_switching_on_without_the_graph_values_or_an_https_address_is_refused():
    assert mail_sync_errors(Settings(graph_subscription_enabled=True))
    assert mail_sync_errors(Settings(mail_catch_up_enabled=True))
    configured = dict(graph_tenant_id="t", graph_client_id="c", graph_client_secret="s", graph_shared_mailbox="m@x.org")
    assert mail_sync_errors(Settings(graph_subscription_enabled=True, graph_notification_url="http://x.org/h", **configured))
    assert not mail_sync_errors(Settings(graph_subscription_enabled=True, graph_notification_url=URL, **configured))
    assert not mail_sync_errors(Settings(mail_catch_up_enabled=True, **configured))  # polling needs no public address
    assert not mail_sync_errors(Settings())


def test_graph_times_with_seven_fractional_digits_parse():
    assert parse_graph_time("2026-10-27T10:00:00.0000000Z") == datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
    assert parse_graph_time("2026-10-27T10:00:00Z") == datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
    assert parse_graph_time("nonsense") is None and parse_graph_time(None) is None


class Recorder:
    def __init__(self, body=None):
        self.requests = []
        self.body = body or {}

    def __call__(self, method):
        def send(url, **kwargs):
            self.requests.append((method, url, kwargs))
            return httpx.Response(200, json=self.body, request=httpx.Request(method, url))

        return send


@pytest.fixture
def wire(monkeypatch):
    recorder = Recorder({"id": "s1", "expirationDateTime": "2026-10-27T10:00:00.0000000Z", "value": []})
    for method in ("get", "post", "patch", "delete"):
        monkeypatch.setattr(httpx, method, recorder(method.upper()))
    monkeypatch.setattr(settings, "graph_shared_mailbox", "support@example.org")
    client = graph_module.GraphClient()
    client._access_token, client._token_expires_at = "token-value", 9e12
    client.recorder = recorder
    return client


def test_the_client_creates_with_the_lifecycle_address_and_client_state(wire):
    wire.create_subscription("users/x/messages", URL, URL, "state-xyz", datetime(2026, 10, 27, 10, tzinfo=timezone.utc))
    method, url, kwargs = wire.recorder.requests[-1]
    assert (method, url) == ("POST", "https://graph.microsoft.com/v1.0/subscriptions")
    assert kwargs["json"] == {
        "changeType": "created", "notificationUrl": URL, "lifecycleNotificationUrl": URL,
        "resource": "users/x/messages", "expirationDateTime": "2026-10-27T10:00:00Z", "clientState": "state-xyz",
    }
    assert kwargs["headers"] == {"Authorization": "Bearer token-value"}


def test_the_client_renews_with_a_patch_and_deletes(wire):
    wire.renew_subscription("a/b", datetime(2026, 10, 27, 10, tzinfo=timezone.utc))
    assert wire.recorder.requests[-1][:2] == ("PATCH", "https://graph.microsoft.com/v1.0/subscriptions/a%2Fb")
    wire.delete_subscription("s1")
    assert wire.recorder.requests[-1][:2] == ("DELETE", "https://graph.microsoft.com/v1.0/subscriptions/s1")


def test_the_client_never_sends_its_token_to_a_link_outside_graph(wire):
    with pytest.raises(ValueError):
        wire.inbox_delta_page("https://evil.example.org/steal")
    with pytest.raises(ValueError):
        wire.inbox_delta_page("https://graph.microsoft.com.evil.org/v1.0/x")
    assert wire.recorder.requests == []


def test_the_client_starts_a_delta_with_the_window_filter_and_selects_only_ids(wire):
    wire.inbox_delta_page(None, datetime(2026, 10, 17, 12, tzinfo=timezone.utc))
    method, url, kwargs = wire.recorder.requests[-1]
    assert url == "https://graph.microsoft.com/v1.0/users/support@example.org/mailFolders/inbox/messages/delta"
    assert kwargs["params"] == {"$select": "id,receivedDateTime", "$filter": "receivedDateTime ge 2026-10-17T12:00:00Z"}


# ---- Admin page -----------------------------------------------------------------------------------


def test_the_admin_page_shows_the_state_and_no_secrets(client, db_session, mail):
    run(db_session, mail, datetime.now(timezone.utc))
    db_session.add(GraphNotification(change_type="created", resource_id="waiting"))
    db_session.commit()
    login_as(client, "admin@example.com")

    response = client.get("/admin/mail-intake")

    assert response.status_code == 200
    assert resource() in response.text.replace("&#39;", "'")
    assert "1 message notification" in response.text
    assert "state-xyz" not in response.text and "Bearer" not in response.text


def test_the_admin_page_is_for_super_admins_only(client):
    login_as(client, "agent@example.com")
    assert client.get("/admin/mail-intake").status_code == 403


def test_the_admin_page_works_before_anything_has_run(client):
    login_as(client, "admin@example.com")
    assert client.get("/admin/mail-intake").status_code == 200
