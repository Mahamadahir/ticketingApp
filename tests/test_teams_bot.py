"""8A: the Teams bot foundation. Activities are signed by a test key set served by a fake metadata endpoint, and
the Bot Connector is a fake (httpx.MockTransport). Nothing here touches the network."""

import json
import time
from datetime import datetime, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlmodel import select

from app import bot_connector, rate_limit, teams, teams_bot, teams_bot_auth, teams_conversations
from app.bot_connector import BotAuthFailed, ConversationRef
from app.config import Settings, settings, unsafe_setting_errors
from app.main import build_app
from app.models import AuditLog, LinkedIdentity, TeamsConversation, TeamsPost, User, UserRole

APP_ID = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
OTHER_TENANT = "33333333-3333-3333-3333-333333333333"
SERVICE = "https://smba.example.net/emea/"
OID = "aaaaaaaa-0000-0000-0000-000000000001"
URL = "/api/teams/messages"
KID = "key-1"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key, kid=KID, endorsements=("msteams",)):
    body = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return body | {"kid": kid, "use": "sig", "endorsements": list(endorsements)}


class FakeMetadata:
    """Stands in for login.botframework.com: the OpenID metadata and the key set."""

    def __init__(self):
        self.keys = [jwk(KEY)]
        self.calls = []
        self.broken = False

    def __call__(self, url):
        self.calls.append(url)
        if self.broken:
            raise httpx.ConnectError("down")
        if url == teams_bot_auth.METADATA_URL:
            return {"issuer": teams_bot_auth.ISSUER, "jwks_uri": "https://login.botframework.com/v1/.well-known/keys"}
        assert url == "https://login.botframework.com/v1/.well-known/keys"
        return {"keys": self.keys}


@pytest.fixture
def metadata(monkeypatch):
    fake = FakeMetadata()
    monkeypatch.setattr(teams_bot_auth, "fetch_json", fake)
    teams_bot_auth.KEYS.reset()
    yield fake
    teams_bot_auth.KEYS.reset()


@pytest.fixture
def bot(monkeypatch, metadata):
    monkeypatch.setattr(settings, "teams_bot_enabled", True)
    monkeypatch.setattr(settings, "teams_bot_app_id", APP_ID)
    monkeypatch.setattr(settings, "teams_bot_tenant_id", TENANT)
    monkeypatch.setattr(settings, "teams_bot_client_secret", "the-secret")
    rate_limit.TEAMS_BOT.reset()
    bot_connector.TOKENS.reset()
    yield
    bot_connector.TOKENS.reset()


def token(*, key=KEY, kid=KID, aud=APP_ID, iss=teams_bot_auth.ISSUER, service=SERVICE, exp_in=3600, algorithm="RS256", **extra):
    now = int(time.time())
    claims = {"aud": aud, "iss": iss, "nbf": now - 10, "exp": now + exp_in, "serviceurl": service} | extra
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def activity(type="message", *, tenant=TENANT, conversation_type="personal", oid=OID, **fields):
    base = {
        "type": type, "id": "act-1", "serviceUrl": SERVICE, "channelId": "msteams",
        "from": {"id": "29:teams-user", "aadObjectId": oid, "name": "Ann Able"},
        "recipient": {"id": f"28:{APP_ID}", "name": "IT Support"},
        "conversation": {"id": "a:conv-1", "conversationType": conversation_type, "tenantId": tenant},
        "channelData": {"tenant": {"id": tenant}},
    }
    return base | fields


def post(client, body, bearer=None, headers=None):
    headers = dict(headers or {})
    if bearer is not None:
        headers["Authorization"] = bearer if bearer.startswith("Bearer ") or " " in bearer else f"Bearer {bearer}"
    return client.post(URL, content=json.dumps(body) if not isinstance(body, (bytes, str)) else body, headers=headers | {"Content-Type": "application/json"})


@pytest.fixture
def public(bot, db_session):
    with TestClient(build_app("public")) as c:
        yield c


class FakeConnector:
    """Stands in for the Bot Connector and the token endpoint. `script` is a list of responses to give in turn
    (status codes or exceptions); once it is used up every call is a 200."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_requests: list[httpx.Request] = []
        self.script: list = []
        self.sleeps: list[float] = []
        self.token_counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "login.microsoftonline.com" in request.url.host:
            self.token_requests.append(request)
            self.token_counter += 1
            return httpx.Response(200, json={"access_token": f"bot-token-{self.token_counter}", "expires_in": 3600})
        self.requests.append(request)
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            return httpx.Response(step[0], headers=step[1], json={})
        return httpx.Response(step, json={"id": f"sent-{len(self.requests)}"})

    def body(self, index=-1):
        return json.loads(self.requests[index].content)


@pytest.fixture
def connector(bot, monkeypatch):
    fake = FakeConnector()
    monkeypatch.setattr(bot_connector, "new_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(bot_connector, "sleep", fake.sleeps.append)
    return fake


# ---- The switch and the roles ----------------------------------------------------------------------------


def test_the_route_is_served_by_the_public_app_and_all_but_never_web_or_worker():
    served = lambda role: set(build_app(role).openapi()["paths"])
    assert URL in served("public") and URL in served("all")
    assert URL not in served("web") and URL not in served("worker")


def test_the_web_and_worker_apps_answer_404(bot, db_session):
    for role in ("web", "worker"):
        with TestClient(build_app(role)) as c:
            assert c.post(URL, json=activity(), headers={"Authorization": f"Bearer {token()}"}).status_code == 404


def test_with_the_switch_off_the_route_answers_404_and_checks_nothing(public, metadata, monkeypatch):
    monkeypatch.setattr(settings, "teams_bot_enabled", False)
    assert post(public, activity(), token()).status_code == 404
    assert metadata.calls == []


def test_the_switch_needs_an_app_id_and_a_tenant():
    errors = unsafe_setting_errors(Settings(env="dev", teams_bot_enabled=True))
    assert any("TEAMS_BOT_APP_ID" in e for e in errors)
    assert unsafe_setting_errors(Settings(env="dev", teams_bot_enabled=True, teams_bot_app_id=APP_ID, teams_bot_tenant_id=TENANT)) == []
    assert unsafe_setting_errors(Settings(env="dev")) == []


def test_the_web_app_may_not_hold_the_bot_secret():
    assert any("TEAMS_BOT_CLIENT_SECRET" in e for e in unsafe_setting_errors(Settings(env="dev", app_role="web", teams_bot_client_secret="s")))
    assert unsafe_setting_errors(Settings(env="dev", app_role="public", teams_bot_client_secret="s")) == []


def test_the_route_is_rate_limited(public, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_teams_bot_per_minute", 2)
    codes = [post(public, activity(), "junk").status_code for _ in range(3)]
    assert codes == [401, 401, 429]


# ---- The 401 cases ---------------------------------------------------------------------------------------


def assert_refused(response, status=401):
    assert response.status_code == status
    assert response.content == b""  # nothing for a caller to learn from


def test_no_header_or_a_header_that_is_not_a_bearer_token(public):
    assert_refused(post(public, activity()))
    assert_refused(post(public, activity(), "Basic abc"))
    assert_refused(post(public, activity(), "Bearer "))
    assert_refused(post(public, activity(), "not-a-jwt"))


def test_a_body_that_is_not_json_or_not_an_activity(public):
    assert_refused(post(public, "{not json", token()))
    assert_refused(post(public, [1, 2], token()))


def test_an_oversized_body_is_refused(public):
    assert post(public, "x" * 300_000, token()).status_code == 413


def test_the_wrong_audience(public):
    assert_refused(post(public, activity(), token(aud="someone-elses-bot")))


def test_the_wrong_issuer(public):
    assert_refused(post(public, activity(), token(iss="https://sts.windows.net/" + TENANT + "/")))


def test_an_expired_token_but_not_one_inside_the_clock_skew(public, db_session):
    assert_refused(post(public, activity(), token(exp_in=-3600)))
    assert post(public, activity("conversationUpdate"), token(exp_in=-60)).status_code == 200  # a minute late is allowed


def test_a_token_without_an_expiry(public):
    assert_refused(post(public, activity(), token(exp=None)))


def test_a_token_signed_by_a_key_we_do_not_know(public):
    assert_refused(post(public, activity(), token(kid="other-kid")))


def test_a_token_with_a_known_key_id_but_the_wrong_signature(public):
    assert_refused(post(public, activity(), token(key=OTHER_KEY)))


def test_a_token_not_signed_with_rs256(public):
    forged = jwt.encode({"aud": APP_ID, "iss": teams_bot_auth.ISSUER, "exp": int(time.time()) + 600, "serviceurl": SERVICE}, "x" * 40, algorithm="HS256", headers={"kid": KID})
    assert_refused(post(public, activity(), forged))
    unsigned = jwt.encode({"aud": APP_ID, "iss": teams_bot_auth.ISSUER, "exp": int(time.time()) + 600}, None, algorithm="none", headers={"kid": KID})
    assert_refused(post(public, activity(), unsigned))


def test_the_service_url_must_match_the_token(public, db_session):
    assert_refused(post(public, activity(serviceUrl="https://evil.example/"), token()))
    assert_refused(post(public, activity(), token(service=None)))  # no claim at all
    assert post(public, activity("conversationUpdate", serviceUrl=SERVICE.rstrip("/")), token()).status_code == 200  # a trailing slash is not a difference
    assert db_session.exec(select(TeamsConversation)).all() == []


def test_a_key_not_endorsed_for_teams_or_another_channel_is_forbidden(public, metadata):
    metadata.keys = [jwk(KEY, endorsements=("webchat",))]
    assert_refused(post(public, activity(), token()), 403)
    teams_bot_auth.KEYS.reset()
    metadata.keys = [jwk(KEY)]
    assert_refused(post(public, activity(channelId="webchat"), token()), 403)


def test_no_bot_app_id_configured_refuses_everything(public, monkeypatch):
    monkeypatch.setattr(settings, "teams_bot_app_id", "")
    assert_refused(post(public, activity(), token()))


def test_keys_that_cannot_be_read_refuse_the_request(public, metadata):
    metadata.broken = True
    assert_refused(post(public, activity(), token()))


# ---- The keys: cached and refreshed ----------------------------------------------------------------------


def test_keys_are_read_once_and_cached(public, metadata, db_session):
    for _ in range(3):
        assert post(public, activity("conversationUpdate"), token()).status_code == 200
    assert len(metadata.calls) == 2  # the metadata and the key set, once


def test_a_failed_read_of_the_keys_is_not_repeated_on_every_request():
    fake = FakeMetadata()
    fake.broken = True
    original = teams_bot_auth.fetch_json
    teams_bot_auth.fetch_json = fake
    try:
        cache = teams_bot_auth.KeyCache()
        for at in (0.0, 5.0, 30.0):
            with pytest.raises(teams_bot_auth.TokenRejected):
                cache.key_for(KID, now=at)
        assert len(fake.calls) == 1  # one try inside the minute, however many requests
        fake.broken = False
        assert cache.key_for(KID, now=61.0)["kid"] == KID
    finally:
        teams_bot_auth.fetch_json = original


def test_an_unknown_key_id_refreshes_at_most_once_a_minute():
    calls = FakeMetadata()
    original = teams_bot_auth.fetch_json
    teams_bot_auth.fetch_json = calls
    try:
        cache = teams_bot_auth.KeyCache()
        cache.key_for(KID, now=1000.0)
        assert len(calls.calls) == 2
        for kid, at in (("new", 1010.0), ("new", 1030.0)):
            with pytest.raises(teams_bot_auth.TokenRejected):
                cache.key_for(kid, now=at)
        assert len(calls.calls) == 2  # the miss at 1010 is inside a minute of the first read
        calls.keys.append(jwk(OTHER_KEY, kid="new"))
        assert cache.key_for("new", now=1100.0)["kid"] == "new"  # a minute on, a new key is found
        assert len(calls.calls) == 4
        cache.key_for(KID, now=1100.0 + teams_bot_auth.KEYS_MAX_AGE_SECONDS)  # a day later everything is re-read
        assert len(calls.calls) == 6
    finally:
        teams_bot_auth.fetch_json = original


def test_a_failed_refresh_keeps_the_keys_we_have():
    fake = FakeMetadata()
    original = teams_bot_auth.fetch_json
    teams_bot_auth.fetch_json = fake
    try:
        cache = teams_bot_auth.KeyCache()
        cache.key_for(KID, now=0.0)
        fake.broken = True
        assert cache.key_for(KID, now=teams_bot_auth.KEYS_MAX_AGE_SECONDS + 1)["kid"] == KID
    finally:
        teams_bot_auth.fetch_json = original


# ---- The tenant ------------------------------------------------------------------------------------------


def test_another_tenant_is_forbidden_and_stores_nothing(public, db_session):
    assert_refused(post(public, activity("installationUpdate", action="add", tenant=OTHER_TENANT), token()), 403)
    assert db_session.exec(select(TeamsConversation)).all() == []


def test_an_activity_with_no_tenant_is_forbidden(public):
    body = activity()
    del body["channelData"]
    assert_refused(post(public, body, token()), 403)


def test_the_tenant_is_compared_without_regard_to_case(public, db_session):
    assert post(public, activity("installationUpdate", action="add", tenant=TENANT.upper()), token()).status_code == 200
    assert len(db_session.exec(select(TeamsConversation)).all()) == 1


# ---- The dispatcher --------------------------------------------------------------------------------------


def test_types_with_no_handler_are_accepted_and_ignored(public, db_session):
    for kind in ("conversationUpdate", "typing", "invoke", "event", "somethingNew"):
        assert post(public, activity(kind), token()).status_code == 200
    assert db_session.exec(select(TeamsConversation)).all() == []


def test_a_handler_is_one_registration_per_type_and_its_answer_is_returned(public, monkeypatch):
    monkeypatch.setattr(teams_bot, "HANDLERS", dict(teams_bot.HANDLERS))

    @teams_bot.handler("conversationUpdate")
    def answer(session, act):
        return teams_bot.Handled(body={"status": 200, "seen": act["type"]})

    assert post(public, activity("conversationUpdate"), token()).json() == {"status": 200, "seen": "conversationUpdate"}
    with pytest.raises(RuntimeError):
        teams_bot.handler("conversationUpdate")(lambda session, act: None)


# ---- Install, uninstall and the store --------------------------------------------------------------------


def audits(db_session):
    db_session.expire_all()
    return [(a.action, a.actor_email, a.entity_type) for a in db_session.exec(select(AuditLog).order_by(AuditLog.id)).all()]


def make_user(db_session, email="ann@example.com", oid=OID, name="Ann Able", role=UserRole.END_USER):
    user = User(email=email, display_name=name, role=role, entra_object_id=oid)
    db_session.add(user)
    db_session.commit()
    return user


def test_installing_stores_the_conversation_for_the_person_and_is_audited(public, db_session):
    ann = make_user(db_session)
    assert post(public, activity("installationUpdate", action="add"), token()).status_code == 200

    row = db_session.exec(select(TeamsConversation)).one()
    assert (row.entra_object_id, row.user_id, row.service_url, row.conversation_id, row.teams_user_id) == (OID, ann.id, SERVICE, "a:conv-1", "29:teams-user")
    assert audits(db_session) == [("teams_bot_installed", "ann@example.com", "teams_conversation")]
    assert teams_conversations.reference_for(db_session, ann).id == row.id


def test_installing_twice_keeps_one_row_and_audits_once(public, db_session):
    for _ in range(2):
        post(public, activity("installationUpdate", action="add", conversation={"id": "a:conv-2", "conversationType": "personal"}), token())
    row = db_session.exec(select(TeamsConversation)).one()
    assert row.conversation_id == "a:conv-2"
    assert [a[0] for a in audits(db_session)] == ["teams_bot_installed"]


def test_uninstalling_removes_it_and_is_audited(public, db_session):
    make_user(db_session)
    post(public, activity("installationUpdate", action="add"), token())
    assert post(public, activity("installationUpdate", action="remove"), token()).status_code == 200
    assert db_session.exec(select(TeamsConversation)).all() == []
    assert [a[0] for a in audits(db_session)] == ["teams_bot_installed", "teams_bot_uninstalled"]
    # removing what is not there does nothing and says nothing
    post(public, activity("installationUpdate", action="remove"), token())
    assert [a[0] for a in audits(db_session)] == ["teams_bot_installed", "teams_bot_uninstalled"]


def test_the_upgrade_actions_count_as_install_and_uninstall(public, db_session):
    post(public, activity("installationUpdate", action="add-upgrade"), token())
    assert len(db_session.exec(select(TeamsConversation)).all()) == 1
    post(public, activity("installationUpdate", action="remove-upgrade"), token())
    assert db_session.exec(select(TeamsConversation)).all() == []


def test_only_the_personal_scope_is_served(public, db_session):
    for scope in ("channel", "groupChat"):
        post(public, activity("installationUpdate", action="add", conversation_type=scope), token())
        post(public, activity("message", conversation_type=scope, text="hi"), token())
    assert db_session.exec(select(TeamsConversation)).all() == []


def test_a_person_with_no_account_still_gets_a_row_found_once_they_have_one(public, db_session):
    post(public, activity("installationUpdate", action="add"), token())
    row = db_session.exec(select(TeamsConversation)).one()
    assert row.user_id is None
    assert audits(db_session) == [("teams_bot_installed", "teams-bot", "teams_conversation")]
    ann = make_user(db_session)  # signs in later with the same object id
    assert teams_conversations.reference_for(db_session, ann).id == row.id


def test_people_are_matched_by_object_id_never_by_name(public, db_session):
    make_user(db_session, email="other@example.com", oid="bbbbbbbb-0000-0000-0000-000000000002", name="Ann Able")  # same display name
    post(public, activity("installationUpdate", action="add"), token())  # the activity says "Ann Able" too
    assert db_session.exec(select(TeamsConversation)).one().user_id is None


def test_the_object_id_is_matched_without_regard_to_case(public, db_session):
    ann = make_user(db_session, oid=OID.upper())
    post(public, activity("installationUpdate", action="add", **{"from": {"id": "29:x", "aadObjectId": OID.upper()}}), token())
    row = db_session.exec(select(TeamsConversation)).one()
    assert (row.entra_object_id, row.user_id) == (OID, ann.id)


def test_a_linked_identity_reaches_the_main_person(public, db_session):
    main = make_user(db_session, email="ann@example.com", oid="cccccccc-0000-0000-0000-000000000003")
    db_session.add(LinkedIdentity(main_user_id=main.id, email="ann@alias.example", entra_object_id=OID, method="admin", status="active"))
    db_session.commit()
    post(public, activity("installationUpdate", action="add"), token())
    row = db_session.exec(select(TeamsConversation)).one()
    assert row.user_id == main.id
    assert teams_conversations.reference_for(db_session, main).id == row.id


def test_an_account_merged_into_another_resolves_to_the_main_person(public, db_session):
    main = make_user(db_session, email="ann@example.com", oid="cccccccc-0000-0000-0000-000000000003")
    old = make_user(db_session, email="ann@old.example", oid=OID)
    db_session.add(LinkedIdentity(main_user_id=main.id, secondary_user_id=old.id, email="ann@old.example", entra_object_id=OID, method="admin", status="active"))
    db_session.commit()
    post(public, activity("installationUpdate", action="add"), token())
    assert db_session.exec(select(TeamsConversation)).one().user_id == main.id


def test_unusable_values_are_not_stored(db_session):
    assert teams_conversations.save_reference(db_session, "", SERVICE, "c") == (None, False)
    assert teams_conversations.save_reference(db_session, OID, SERVICE, "c" * 600) == (None, False)
    assert teams_conversations.save_reference(db_session, OID, "", "c") == (None, False)


# ---- A message to the bot --------------------------------------------------------------------------------


def test_a_message_stores_the_conversation_and_replies(public, connector, db_session):
    assert post(public, activity("message", text="hello"), token()).status_code == 200

    assert db_session.exec(select(TeamsConversation)).one().conversation_id == "a:conv-1"
    request = connector.requests[0]
    assert str(request.url) == "https://smba.example.net/emea/v3/conversations/a%3Aconv-1/activities/act-1"
    assert request.headers["Authorization"] == "Bearer bot-token-1"
    body = connector.body()
    assert body["type"] == "message" and body["text"] == teams_bot.MESSAGE_NOT_READ_YET
    assert body["replyToId"] == "act-1" and body["recipient"]["id"] == "29:teams-user" and body["from"]["id"] == f"28:{APP_ID}"


def test_the_reply_is_work_for_after_the_answer_not_part_of_it(bot, connector, db_session):
    """Teams sends the activity again when the answer is late, so receive() itself never calls Teams back."""
    handled = teams_bot.receive(db_session, f"Bearer {token()}", activity("message", text="hello"))
    assert connector.requests == [] and handled.after is not None and handled.body is None
    handled.after()
    assert len(connector.requests) == 1


def test_a_failing_reply_does_not_fail_the_request(public, connector, db_session):
    connector.script = [500, 500, 500]
    assert post(public, activity("message", text="hello"), token()).status_code == 200
    assert len(db_session.exec(select(TeamsConversation)).all()) == 1  # stored before the reply was tried


# ---- The outbound client ---------------------------------------------------------------------------------

REF = ConversationRef("https://smba.example.net/emea", "a:conv-1")


def test_a_card_goes_to_the_conversation_as_one_adaptive_card(connector):
    card = {"type": "AdaptiveCard", "version": "1.4", "body": []}
    assert bot_connector.send_card(REF, card) == "sent-1"
    request = connector.requests[0]
    assert request.method == "POST" and str(request.url) == "https://smba.example.net/emea/v3/conversations/a%3Aconv-1/activities"
    assert connector.body() == {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def test_text_goes_as_plain_text(connector):
    bot_connector.send_text(REF, "Hello *there*")
    assert connector.body() == {"type": "message", "text": "Hello *there*", "textFormat": "plain"}


def test_replying_with_a_card(connector):
    bot_connector.reply_to(activity(), card={"type": "AdaptiveCard"})
    assert connector.body()["attachments"][0]["content"] == {"type": "AdaptiveCard"}


def test_creating_a_conversation(connector):
    ref = bot_connector.create_conversation("https://smba.example.net/emea/", "29:teams-user")
    request = connector.requests[0]
    assert str(request.url) == "https://smba.example.net/emea/v3/conversations"
    assert connector.body() == {
        "bot": {"id": f"28:{APP_ID}"}, "isGroup": False, "members": [{"id": "29:teams-user"}],
        "tenantId": TENANT, "channelData": {"tenant": {"id": TENANT}},
    }
    assert ref == ConversationRef("https://smba.example.net/emea/", "sent-1")


def test_a_conversation_the_connector_does_not_return_is_an_error(connector):
    connector.script = [(200, {})]
    with pytest.raises(ValueError):
        bot_connector.create_conversation(SERVICE, "29:x")


def test_the_token_comes_from_our_tenant_and_is_kept(connector):
    bot_connector.send_text(REF, "one")
    bot_connector.send_text(REF, "two")
    assert len(connector.token_requests) == 1
    request = connector.token_requests[0]
    assert request.url.path == f"/{TENANT}/oauth2/v2.0/token"
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form == {"grant_type": "client_credentials", "client_id": APP_ID, "client_secret": "the-secret", "scope": "https://api.botframework.com/.default"}
    assert [r.headers["Authorization"] for r in connector.requests] == ["Bearer bot-token-1"] * 2


def test_without_a_secret_the_token_comes_from_the_managed_identity(connector, monkeypatch):
    monkeypatch.setattr(settings, "teams_bot_client_secret", "")
    monkeypatch.setattr(bot_connector, "managed_identity_token", lambda: ("identity-token", time.time() + 3600))
    bot_connector.send_text(REF, "hi")
    assert connector.token_requests == []
    assert connector.requests[0].headers["Authorization"] == "Bearer identity-token"


def test_a_token_close_to_expiry_is_renewed(connector):
    bot_connector.send_text(REF, "one")
    bot_connector.TOKENS._expires_at = time.time() + 10
    bot_connector.send_text(REF, "two")
    assert [r.headers["Authorization"] for r in connector.requests] == ["Bearer bot-token-1", "Bearer bot-token-2"]


def test_a_401_gets_one_retry_with_a_fresh_token(connector):
    connector.script = [401, 200]
    bot_connector.send_text(REF, "hi")
    assert [r.headers["Authorization"] for r in connector.requests] == ["Bearer bot-token-1", "Bearer bot-token-2"]
    connector.script = [401, 401]
    with pytest.raises(BotAuthFailed) as refused:
        bot_connector.send_text(REF, "hi")
    # a refused credential is not a refused message: an outbox row keeps trying until someone fixes the secret
    assert not teams.is_permanent(refused.value)


def test_a_token_request_the_directory_refuses_is_also_a_credential_failure(bot, monkeypatch):
    monkeypatch.setattr(bot_connector, "new_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "invalid_client"}))))
    with pytest.raises(BotAuthFailed):
        bot_connector.send_text(REF, "hi")


def test_a_server_error_is_retried_with_a_short_wait(connector):
    connector.script = [502, 503, 200]
    assert bot_connector.send_text(REF, "hi") == "sent-3"
    assert connector.sleeps == [1.0, 3.0]


def test_a_rate_limit_waits_as_long_as_the_connector_asks_up_to_a_cap(connector):
    connector.script = [(429, {"Retry-After": "2"}), (429, {"Retry-After": "600"}), 200]
    bot_connector.send_text(REF, "hi")
    assert connector.sleeps == [2.0, bot_connector.MAX_RETRY_AFTER_SECONDS]


def test_a_network_error_is_retried(connector):
    connector.script = [httpx.ConnectError("down"), 200]
    bot_connector.send_text(REF, "hi")
    assert len(connector.requests) == 2
    assert connector.sleeps == [1.0]


def test_a_read_timeout_is_not_retried_because_the_message_may_have_been_delivered(connector):
    connector.script = [httpx.ReadTimeout("slow")]
    with pytest.raises(httpx.ReadTimeout):
        bot_connector.send_text(REF, "hi")
    assert len(connector.requests) == 1 and connector.sleeps == []


def test_when_the_retries_run_out_the_error_reaches_the_caller(connector):
    connector.script = [500, 500, 500]
    with pytest.raises(httpx.HTTPStatusError) as failed:
        bot_connector.send_text(REF, "hi")
    assert failed.value.response.status_code == 500
    assert len(connector.requests) == 3 and connector.sleeps == [1.0, 3.0]


def test_a_refusal_is_not_retried(connector):
    connector.script = [400]
    with pytest.raises(httpx.HTTPStatusError) as failed:
        bot_connector.send_text(REF, "hi")
    assert len(connector.requests) == 1 and connector.sleeps == []
    assert teams.is_permanent(failed.value)


def test_a_service_address_that_is_not_https_is_never_sent_the_token(connector):
    for bad in ("http://smba.example.net/", "https://user:pw@smba.example.net/", "javascript:alert(1)", ""):
        with pytest.raises(bot_connector.BotNotConfigured):
            bot_connector.send_text(ConversationRef(bad, "c"), "hi")
    assert connector.requests == [] and connector.token_requests == []


def test_redirects_are_not_followed(bot):
    assert bot_connector.new_client().follow_redirects is False


def test_nothing_is_sent_when_the_bot_is_off_or_unconfigured(connector, monkeypatch):
    monkeypatch.setattr(settings, "teams_bot_app_id", "")
    with pytest.raises(bot_connector.BotNotConfigured):
        bot_connector.send_text(REF, "hi")
    assert connector.requests == []


def test_a_failed_send_is_kept_on_an_outbox_row_with_the_outboxs_backoff(connector, db_session):
    """The client raises what httpx raised, so a Teams outbox row records it the way it records a webhook failure."""
    connector.script = [500, 500, 500]
    post_row = TeamsPost(kind="notification")
    now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    try:
        bot_connector.send_text(REF, "hi")
    except Exception as error:
        teams.record_failure(post_row, error, now)
    assert post_row.attempts == 1 and post_row.last_error == "Teams answered 500"
    assert post_row.next_attempt_at == datetime(2026, 11, 9, 10, 2, tzinfo=timezone.utc)
    assert "the-secret" not in post_row.last_error and "bot-token" not in post_row.last_error
    assert [teams.backoff_minutes(n) for n in range(1, 8)] == [2, 4, 8, 16, 32, 60, 60]
