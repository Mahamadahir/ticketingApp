"""8C: approving or declining from the bot's card. The activities are signed by the test key set of test_teams_bot.py
and posted to the real endpoint; the vote goes through the real approvals code. Nothing here touches the network."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import bot_cards, outbox, teams
from app.approval_reminders import send_approval_reminders
from app.approvals import APPROVED, DECLINED, NOT_NEEDED, PENDING, current_approval, withdraw
from app.config import settings
from app.jobs import teams_copies
from app.models import (
    ApprovalVote, AuditLog, Notification, RequestType, RequestTypeQuestion, TeamsConversation, TeamsPost, Ticket,
    TicketAnswer, TicketEvent, TicketStatus, User,
)
from app.notification_settings import APPROVAL_WAITING, TEAMS, save_choices
from app.settings_store import APPROVAL_REMINDERS_ENABLED, APPROVAL_REMIND_AFTER_DAYS, set_setting
from tests.test_outbound_mail import dev_mail, rows  # noqa: F401  (dev_mail is a fixture)
from tests.test_approvals import approvals, events, many, person, raised_and_triaged, request_type, status
from tests.test_teams_bot import SERVICE, activity, bot, metadata, post, public, token  # noqa: F401  (fixtures)

OID = "aaaaaaaa-0000-0000-0000-00000000000a"
OTHER_OID = "bbbbbbbb-0000-0000-0000-00000000000b"
CONVERSATION = "a:1approver"


def card_action(oid=OID, verb="approval.approve", data=None, name="adaptiveCard/action", **inputs):
    """What Teams sends when a button is pressed: an invoke activity whose value holds the action as the card defined it,
    with the card's inputs merged into its data."""
    return activity(
        "invoke", oid=oid, name=name,
        value={"action": {"type": "Action.Execute", "verb": verb, "data": {**(data or {}), **inputs}}, "trigger": "manual"},
    )


def with_oid(db_session, email, oid=OID):
    user = db_session.exec(select(User).where(User.email == email)).one()
    user.entra_object_id = oid
    db_session.add(user)
    db_session.commit()
    return user


def vote_of(db_session, email):
    db_session.expire_all()
    return db_session.exec(select(ApprovalVote).join(User, User.id == ApprovalVote.voter_id).where(User.email == email)).one()


def data_for(vote):
    return {"approval": vote.approval_id, "vote": vote.id}


def press(public, vote, oid=OID, verb="approval.approve", data=None, **inputs):
    response = post(public, card_action(oid, verb, data if data is not None else data_for(vote), **inputs), token())
    assert response.status_code == 200
    return response.json() if response.content else None


def card_text(response):
    """All the text on the card an answer carries, for 'does it say ...' checks."""
    assert response["statusCode"] == 200 and response["type"] == "application/vnd.microsoft.card.adaptive"
    return json.dumps(response["value"])


@pytest.fixture
def one_approver(client, db_session):
    """A request waiting on one approver (boss, who is no agent and has an Entra id), 100% needed."""
    person(client, db_session, "boss@example.com", "Bo Boss")
    with_oid(db_session, "boss@example.com")
    return raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))


@pytest.fixture
def two_approvers(client, db_session):
    emails = many(client, db_session, ("a", "b"))
    with_oid(db_session, emails[0])
    with_oid(db_session, emails[1], OTHER_OID)
    return raised_and_triaged(client, db_session, request_type(db_session, emails, 50))


# ---- Voting ---------------------------------------------------------------------------------------------


def test_approve_records_the_vote_settles_the_request_and_replaces_the_card(public, db_session, one_approver):
    vote = vote_of(db_session, "boss@example.com")

    response = press(public, vote)

    text = card_text(response)
    assert "You approved" in text and "Approved" in text and "Bo Boss" in text  # who decided and the outcome
    assert "Action.Execute" not in text  # no buttons left to press
    assert approvals(db_session, one_approver) == {"boss@example.com": APPROVED}
    assert status(db_session, one_approver) == TicketStatus.OPEN


def test_decline_with_a_comment_records_it_and_closes_the_request(public, db_session, one_approver):
    vote = vote_of(db_session, "boss@example.com")

    response = press(public, vote, verb="approval.decline", comment="No budget this quarter")

    text = card_text(response)
    assert "You declined" in text and "No budget this quarter" in text and "Declined" in text
    assert approvals(db_session, one_approver) == {"boss@example.com": DECLINED}
    assert vote_of(db_session, "boss@example.com").comment == "No budget this quarter"
    assert status(db_session, one_approver) == TicketStatus.CLOSED


def test_the_vote_is_audited_and_the_event_says_via_teams(public, db_session, one_approver):
    vote = vote_of(db_session, "boss@example.com")

    press(public, vote, comment="Fine by me")

    [decided] = events(db_session, one_approver, "approval_decided")
    assert decided.actor_email == "boss@example.com" and decided.to_value == APPROVED
    assert decided.detail == "via Teams: Fine by me"
    entry = db_session.exec(select(AuditLog).where(AuditLog.action == "approval_approved")).one()
    assert entry.detail == "via Teams: Fine by me" and entry.actor_email == "boss@example.com"


def test_a_vote_without_a_comment_says_just_via_teams(public, db_session, two_approvers):
    press(public, vote_of(db_session, "a@example.com"))
    [decided] = events(db_session, two_approvers, "approval_decided")
    assert decided.detail == "via Teams"


def test_a_portal_vote_is_recorded_exactly_as_before(client, db_session, one_approver):
    from tests.test_approvals import answer

    answer(client, one_approver, "boss@example.com", "approved", "ok")

    [decided] = events(db_session, one_approver, "approval_decided")
    assert decided.detail == "ok"
    assert db_session.exec(select(AuditLog).where(AuditLog.action == "approval_approved")).one().detail == "ok"


def test_a_vote_that_does_not_settle_leaves_the_request_waiting_and_says_so(public, db_session, client):
    emails = many(client, db_session, ("a", "b", "c"))
    with_oid(db_session, emails[0])
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))

    text = card_text(press(public, vote_of(db_session, "a@example.com")))

    assert "Still waiting for the other approvers" in text
    assert status(db_session, ticket) == TicketStatus.AWAITING_APPROVAL


# ---- Who is voting --------------------------------------------------------------------------------------


def test_the_voter_is_the_person_teams_says_pressed_not_the_card(public, db_session, two_approvers):
    """Forged card data: b's oid presses a button carrying a's vote. Nothing is recorded, for either of them."""
    a_vote = vote_of(db_session, "a@example.com")

    response = press(public, a_vote, oid=OTHER_OID)

    assert response == {"statusCode": 200, "type": "application/vnd.microsoft.activity.message", "value": "This request is not waiting for an answer from you."}
    assert approvals(db_session, two_approvers) == {"a@example.com": PENDING, "b@example.com": PENDING}
    assert events(db_session, two_approvers, "approval_decided") == []


def test_someone_who_is_not_a_voter_at_all_cannot_vote_with_forged_ids(public, db_session, client, one_approver):
    person(client, db_session, "mallory@example.com")
    with_oid(db_session, "mallory@example.com", OTHER_OID)

    press(public, vote_of(db_session, "boss@example.com"), oid=OTHER_OID, verb="approval.decline")

    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}
    assert status(db_session, one_approver) == TicketStatus.AWAITING_APPROVAL


def test_an_unknown_object_id_records_nothing(public, db_session, one_approver):
    response = press(public, vote_of(db_session, "boss@example.com"), oid="cccccccc-0000-0000-0000-00000000000c")
    assert response["type"] == "application/vnd.microsoft.activity.message" and "match your Teams account" in response["value"]
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}


def test_no_object_id_at_all_records_nothing(public, db_session, one_approver):
    response = post(public, card_action(oid="", data=data_for(vote_of(db_session, "boss@example.com"))), token()).json()
    assert response["type"] == "application/vnd.microsoft.activity.message"
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}


def test_the_voter_is_found_through_a_linked_identity(public, db_session, one_approver):
    from app.models import LinkedIdentity

    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    db_session.add(LinkedIdentity(main_user_id=boss.id, entra_object_id=OTHER_OID, email="boss@other.example", method="admin", status="active"))
    db_session.commit()

    text = card_text(press(public, vote_of(db_session, "boss@example.com"), oid=OTHER_OID))

    assert "You approved" in text
    assert approvals(db_session, one_approver) == {"boss@example.com": APPROVED}


@pytest.mark.parametrize("data", [{}, {"vote": "x", "approval": 1}, {"vote": 999999, "approval": 1}, {"vote": True, "approval": True}])
def test_card_data_that_names_no_vote_records_nothing(public, db_session, one_approver, data):
    response = press(public, None, data=data)
    assert response["type"] == "application/vnd.microsoft.activity.message"
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}


def test_an_invoke_with_a_value_of_the_wrong_shape_records_nothing(public, db_session, one_approver):
    for value in (None, "x", [], {"action": "x"}, {"action": {"verb": "approval.approve", "data": "junk"}}):
        body = activity("invoke", oid=OID, name="adaptiveCard/action", value=value)
        assert post(public, body, token()).status_code == 200
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}


def test_a_vote_with_the_wrong_approval_id_is_refused(public, db_session, one_approver):
    vote = vote_of(db_session, "boss@example.com")
    press(public, vote, data={"vote": vote.id, "approval": vote.approval_id + 1})
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}


def test_other_verbs_and_other_invokes_do_nothing(public, db_session, one_approver):
    vote = vote_of(db_session, "boss@example.com")
    assert press(public, vote, verb="approval.delete")["type"] == "application/vnd.microsoft.activity.message"
    assert post(public, card_action(name="composeExtension/query", data=data_for(vote)), token()).content == b""
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}


def test_a_button_press_needs_the_same_token_as_any_activity(public, db_session, one_approver):
    body = card_action(data=data_for(vote_of(db_session, "boss@example.com")))
    assert post(public, body).status_code == 401
    assert post(public, body, token(aud="someone-else")).status_code == 401
    assert post(public, activity("invoke", tenant="33333333-3333-3333-3333-333333333333", name="adaptiveCard/action"), token()).status_code == 403
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}


# ---- Rules that must hold -------------------------------------------------------------------------------


def test_a_double_press_does_nothing_the_second_time_and_says_why(public, db_session, two_approvers):
    vote = vote_of(db_session, "a@example.com")
    press(public, vote)

    again = press(public, vote, verb="approval.decline")

    text = card_text(again)
    assert "already answered" in text or "settled" in text
    assert "You approved" in text or "Settled without your answer" in text  # the answer on record, not the second press
    assert len(events(db_session, two_approvers, "approval_decided")) == 1
    assert vote_of(db_session, "a@example.com").decision == APPROVED


def test_a_double_press_on_a_request_still_waiting_keeps_the_first_answer(public, db_session, client):
    emails = many(client, db_session, ("a", "b", "c"))
    with_oid(db_session, emails[0])
    ticket = raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    vote = vote_of(db_session, "a@example.com")
    press(public, vote)

    text = card_text(press(public, vote, verb="approval.decline"))

    assert "You have already answered this request" in text and "You approved" in text
    assert approvals(db_session, ticket)["a@example.com"] == APPROVED
    assert len(events(db_session, ticket, "approval_decided")) == 1


def test_a_stale_card_after_the_request_was_settled_without_you_says_so(public, db_session, two_approvers):
    press(public, vote_of(db_session, "a@example.com"))  # 50% of two: a settles it, b is not needed

    text = card_text(press(public, vote_of(db_session, "b@example.com"), oid=OTHER_OID, verb="approval.decline"))

    assert "settled without your answer" in text.lower() and "Approved" in text
    assert vote_of(db_session, "b@example.com").decision == NOT_NEEDED
    assert status(db_session, two_approvers) == TicketStatus.OPEN


def test_a_withdrawn_request_refuses_and_says_so(public, db_session, one_approver):
    withdraw(db_session, db_session.get(Ticket, one_approver.id), "jo@example.com")
    db_session.commit()

    text = card_text(press(public, vote_of(db_session, "boss@example.com")))

    assert "withdrawn" in text.lower()
    assert approvals(db_session, one_approver)["boss@example.com"] != APPROVED
    assert status(db_session, one_approver) == TicketStatus.CLOSED


def test_an_inactive_voter_cannot_vote_from_a_card(public, db_session, one_approver):
    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    boss.is_active = False
    db_session.add(boss)
    db_session.commit()

    text = card_text(press(public, vote_of(db_session, "boss@example.com")))

    assert "inactive" in text
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}


def test_a_voter_taken_out_of_the_group_cannot_vote_from_a_card(client, public, db_session):
    from app.approval_groups import remove_member
    from app.models import ApprovalGroup

    person(client, db_session, "boss@example.com")
    with_oid(db_session, "boss@example.com")
    one_approver = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com", "lead@example.com"], 100))
    group = db_session.get(ApprovalGroup, current_approval(db_session, one_approver).group_id)
    remove_member(db_session, group, db_session.exec(select(User.id).where(User.email == "boss@example.com")).one())
    db_session.commit()

    text = card_text(press(public, vote_of(db_session, "boss@example.com")))

    assert "no longer an approver" in text
    assert approvals(db_session, one_approver)["boss@example.com"] == PENDING


def test_nobody_approves_their_own_request_from_a_card(public, client, db_session, one_approver):
    """A vote that is the requester's own (it cannot exist through the app, but the rule is the approvals code's)."""
    jo = with_oid(db_session, "jo@example.com", OTHER_OID)
    approval = current_approval(db_session, one_approver)
    db_session.add(ApprovalVote(approval_id=approval.id, voter_id=jo.id))
    db_session.commit()

    text = card_text(press(public, vote_of(db_session, "jo@example.com"), oid=OTHER_OID))

    assert "cannot approve your own request" in text
    assert approvals(db_session, one_approver)["jo@example.com"] == PENDING


def test_a_self_approved_vote_is_already_answered_so_a_press_cannot_repeat_it(client, public, db_session):
    from tests.test_self_approval import REQUESTER, self_type

    person(client, db_session, REQUESTER)
    with_oid(db_session, REQUESTER)
    ticket = raised_and_triaged(client, db_session, self_type(db_session, [], 100))
    assert status(db_session, ticket) == TicketStatus.OPEN
    before = len(events(db_session, ticket, "approval_decided"))

    text = card_text(press(public, vote_of(db_session, REQUESTER), verb="approval.decline"))

    assert "already answered" in text or "no longer waiting" in text
    assert len(events(db_session, ticket, "approval_decided")) == before
    assert vote_of(db_session, REQUESTER).decision == APPROVED


def test_a_comment_over_the_limit_keeps_the_card_so_they_can_shorten_it(public, db_session, one_approver):
    vote = vote_of(db_session, "boss@example.com")
    response = press(public, vote, comment="x" * 1001)
    assert response["type"] == "application/vnd.microsoft.activity.message" and "at most 1000 characters" in response["value"]
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}
    assert "You approved" in card_text(press(public, vote, comment="x" * 1000))  # the same card still works


def test_an_unexpected_failure_records_nothing_and_points_to_the_portal(public, db_session, one_approver, monkeypatch):
    from app import teams_approvals

    def broken(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(teams_approvals, "record_decision", broken)

    response = press(public, vote_of(db_session, "boss@example.com"))

    assert response["type"] == "application/vnd.microsoft.activity.message" and "ticketing site" in response["value"]
    assert approvals(db_session, one_approver) == {"boss@example.com": PENDING}


def test_a_comment_that_is_not_text_is_ignored(public, db_session, one_approver):
    press(public, vote_of(db_session, "boss@example.com"), comment=["a", "b"])
    assert vote_of(db_session, "boss@example.com").comment == ""
    assert approvals(db_session, one_approver) == {"boss@example.com": APPROVED}


def test_words_from_the_requester_cannot_make_markup_on_the_answer_card(public, db_session, one_approver):
    ticket = db_session.get(Ticket, one_approver.id)
    ticket.subject = "[Click here](https://evil.example) **now**"
    db_session.add(ticket)
    db_session.commit()

    response = press(public, vote_of(db_session, "boss@example.com"), comment="[c](https://evil.example)")

    shown = json.dumps(response["value"]["body"])
    assert "\\\\[Click here\\\\]\\\\(" in shown and "\\\\[c\\\\]\\\\(" in shown


# ---- The request card -----------------------------------------------------------------------------------


@pytest.fixture
def delivery(monkeypatch):
    """The bot is on and nothing else is: the card goes to the chat. Records what the sender is asked to send."""
    monkeypatch.setattr(settings, "teams_bot_enabled", True)
    monkeypatch.setattr(settings, "teams_bot_app_id", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(settings, "teams_bot_tenant_id", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setattr(settings, "teams_notifications_enabled", False)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "")
    monkeypatch.setattr(settings, "web_base_url", "https://tickets.example.com")
    monkeypatch.setattr(settings, "public_base_url", "")


def installed(db_session, email, oid=OID):
    user = db_session.exec(select(User).where(User.email == email)).one()
    db_session.add(TeamsConversation(entra_object_id=oid, user_id=user.id, service_url=SERVICE, conversation_id=CONVERSATION))
    db_session.commit()
    return user


def age_notifications(db_session, minutes=60):
    for n in db_session.exec(select(Notification)).all():
        n.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        db_session.add(n)
    db_session.commit()


def sent_cards(db_session):
    """Runs the worker's two jobs (hand over, send) and returns what the bot was asked to send."""
    sent = []
    teams_copies(db_session)
    db_session.commit()
    teams.deliver_pending(db_session, send=lambda payload: sent.append(("channel", payload)), bot_send=lambda d: sent.append(("direct", d)))
    db_session.commit()
    return sent


def test_the_waiting_notification_reaches_the_approver_as_a_card_with_the_buttons(client, db_session, delivery):
    person(client, db_session, "boss@example.com", "Bo Boss")
    with_oid(db_session, "boss@example.com")
    kind = request_type(db_session, ["boss@example.com"], 100)
    question = RequestTypeQuestion(request_type_id=kind.id, label="Start date", kind="text", position=1)
    db_session.add(question)
    db_session.commit()
    ticket = raised_and_triaged(client, db_session, kind)
    db_session.add(TicketAnswer(ticket_id=ticket.id, question_id=question.id, question_label="Start date", question_kind="text", value="Monday [1](https://evil.example)"))
    db_session.commit()
    installed(db_session, "boss@example.com")
    age_notifications(db_session)

    [(route, sent)] = sent_cards(db_session)

    assert route == "direct" and (sent.service_url, sent.conversation_id) == (SERVICE, CONVERSATION)
    card = sent.card
    facts = {f["title"]: f["value"] for block in card["body"] if block["type"] == "FactSet" for f in block["facts"]}
    assert facts["Request"] == "New starter" and facts["Asked by"] == teams.plain("jo@example.com")
    assert facts["Start date"] == "Monday \\[1\\]\\(https://evil\\.example\\)"  # the answers they gave, shown as typed
    assert facts["Needed"] == teams.plain("1 of 1 must approve (100%)")
    assert any(b["type"] == "Input.Text" and b["id"] == "comment" for b in card["body"])
    approve, decline, portal = card["actions"]
    vote = vote_of(db_session, "boss@example.com")
    assert (approve["type"], approve["verb"], approve["title"]) == ("Action.Execute", "approval.approve", "Approve")
    assert (decline["type"], decline["verb"], decline["title"]) == ("Action.Execute", "approval.decline", "Decline")
    assert approve["data"] == decline["data"] == data_for(vote)
    assert portal == {"type": "Action.OpenUrl", "title": "Open in the portal", "url": f"https://tickets.example.com/portal/{ticket.number}"}


def test_the_card_the_bot_sends_can_be_pressed_end_to_end(client, public, db_session, delivery):
    person(client, db_session, "boss@example.com")
    with_oid(db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))
    installed(db_session, "boss@example.com")
    age_notifications(db_session)
    [(_, sent)] = sent_cards(db_session)
    [approve, *_] = sent.card["actions"]

    response = post(public, card_action(verb=approve["verb"], data=approve["data"]), token()).json()

    assert "You approved" in card_text(response)
    assert status(db_session, ticket) == TicketStatus.OPEN


def test_a_request_answered_before_the_copy_is_sent_sends_nothing(client, db_session, delivery):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com", "other@example.com"], 50))
    installed(db_session, "boss@example.com")
    age_notifications(db_session)
    teams_copies(db_session)
    db_session.commit()
    from tests.test_approvals import answer

    answer(client, ticket, "other@example.com", "approved")  # settles it: boss is not needed
    sent = []
    teams.deliver_pending(db_session, send=sent.append, bot_send=sent.append)
    db_session.commit()

    assert sent == []
    [post_row] = db_session.exec(select(TeamsPost).where(TeamsPost.kind == teams.NOTIFICATION)).all()
    assert "no longer" in post_row.last_error


def test_an_approver_with_no_conversation_gets_no_card_when_the_channel_fallback_is_off(client, db_session, delivery):
    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))
    age_notifications(db_session)

    assert sent_cards(db_session) == []  # the route function drops it, as for any copy; the bell still works


def test_a_request_waiting_for_a_vote_never_goes_to_the_shared_channel(client, db_session, delivery, monkeypatch):
    """Even with the channel fallback on, an approver with no chat gets nothing there: the post would name the request
    and who asked to everyone in the channel. Other kinds still fall back (test_teams_dm.py)."""
    monkeypatch.setattr(settings, "teams_notifications_enabled", True)
    monkeypatch.setattr(settings, "teams_new_ticket_webhook_url", "https://hooks.example/x")
    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))
    age_notifications(db_session)

    assert sent_cards(db_session) == []
    assert db_session.exec(select(TeamsPost)).all() == []  # dropped at the hand-over, so no failed row either


def test_no_card_for_a_person_who_can_no_longer_answer(client, db_session, delivery):
    from app.approval_groups import remove_member
    from app.models import ApprovalGroup

    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com", "lead@example.com"], 100))
    notification = db_session.exec(select(Notification).where(Notification.user_id == db_session.exec(select(User.id).where(User.email == "boss@example.com")).one())).one()
    group = db_session.get(ApprovalGroup, current_approval(db_session, ticket).group_id)
    remove_member(db_session, group, notification.user_id)
    db_session.commit()

    with pytest.raises(teams.Unsendable):
        bot_cards.notification_card(db_session, notification, db_session.get(Ticket, ticket.id))


def test_the_approver_can_turn_the_teams_copy_off_and_the_default_is_on(client, db_session, delivery):
    person(client, db_session, "boss@example.com")
    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()
    from app.notification_settings import is_enabled

    assert is_enabled(db_session, boss, APPROVAL_WAITING, TEAMS) is True  # the decision: on unless they switch it off
    save_choices(db_session, boss, {f"{APPROVAL_WAITING}:in_app"})
    db_session.commit()
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))
    installed(db_session, "boss@example.com")
    age_notifications(db_session)

    assert sent_cards(db_session) == []
    [n] = db_session.exec(select(Notification).where(Notification.kind == APPROVAL_WAITING)).all()
    assert n.teams_wanted is False and n.bell is True


def test_it_is_not_sent_for_a_read_notification_or_to_an_inactive_person(client, db_session, delivery):
    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))
    boss = installed(db_session, "boss@example.com")
    age_notifications(db_session)
    boss.is_active = False
    db_session.add(boss)
    db_session.commit()

    assert sent_cards(db_session) == []


def test_the_card_builder_refuses_when_there_is_nothing_to_vote_on(client, db_session, delivery):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))
    notification = db_session.exec(select(Notification).where(Notification.kind == APPROVAL_WAITING)).one()
    withdraw(db_session, db_session.get(Ticket, ticket.id), "jo@example.com")
    db_session.commit()

    with pytest.raises(teams.Unsendable):
        bot_cards.notification_card(db_session, notification, db_session.get(Ticket, ticket.id))


# ---- Reminders ------------------------------------------------------------------------------------------


def remind(db_session, days=3):
    set_setting(db_session, APPROVAL_REMINDERS_ENABLED, "on")
    set_setting(db_session, APPROVAL_REMIND_AFTER_DAYS, "2")
    db_session.commit()
    send_approval_reminders(db_session, datetime.now(timezone.utc) + timedelta(days=days))
    db_session.commit()


def reminder_notifications(db_session):
    db_session.expire_all()
    return db_session.exec(select(Notification).where(Notification.kind == APPROVAL_WAITING, Notification.bell.is_(False))).all()


def test_a_reminder_goes_to_an_approver_with_a_chat_as_a_card_even_with_mail_off(client, db_session, delivery, monkeypatch):
    monkeypatch.setattr(settings, "mail_sending", "off")
    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))
    installed(db_session, "boss@example.com")
    for n in db_session.exec(select(Notification)).all():
        n.read_at = datetime.now(timezone.utc)  # they have seen the first one; the reminder is a new copy
        db_session.add(n)
    db_session.commit()

    remind(db_session)

    [reminder] = reminder_notifications(db_session)
    assert "still waiting for your approval" in reminder.text and reminder.teams_wanted
    vote = vote_of(db_session, "boss@example.com")
    assert vote.reminder_count == 1
    [(route, sent)] = sent_cards(db_session)
    assert route == "direct" and [a["verb"] for a in sent.card["actions"][:2]] == ["approval.approve", "approval.decline"]

    remind(db_session, days=3)  # not due again until the repeat interval
    assert len(reminder_notifications(db_session)) == 1


def test_a_reminder_is_not_sent_to_teams_to_someone_with_no_chat_or_with_teams_off(client, db_session, delivery):
    emails = many(client, db_session, ("a", "b"))
    raised_and_triaged(client, db_session, request_type(db_session, emails, 100))
    installed(db_session, "a@example.com")
    b = db_session.exec(select(User).where(User.email == "b@example.com")).one()
    save_choices(db_session, b, {f"{APPROVAL_WAITING}:in_app"})
    a = db_session.exec(select(User).where(User.email == "a@example.com")).one()
    save_choices(db_session, a, {f"{APPROVAL_WAITING}:in_app"})  # a has the chat but switched Teams off
    db_session.commit()

    remind(db_session)

    assert reminder_notifications(db_session) == []


def test_a_second_teams_reminder_waits_until_the_first_has_been_handed_over(client, db_session, delivery):
    from app.notifications import notify_approval_reminder

    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))
    boss = db_session.exec(select(User).where(User.email == "boss@example.com")).one()

    assert notify_approval_reminder(db_session, ticket, boss) is not None
    db_session.flush()
    assert notify_approval_reminder(db_session, ticket, boss) is None  # the first is still waiting for the worker


def test_the_email_reminder_still_goes_and_both_count_as_one_reminder(client, db_session, delivery, dev_mail):
    person(client, db_session, "boss@example.com")
    raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"], 100))
    installed(db_session, "boss@example.com")
    for row in rows(db_session, outbox.APPROVAL):
        row.sent_at = datetime.now(timezone.utc)  # the approval email has gone
        db_session.add(row)
    db_session.commit()

    remind(db_session)

    assert len(rows(db_session, outbox.APPROVAL_REMINDER)) == 1
    assert len(reminder_notifications(db_session)) == 1
    assert vote_of(db_session, "boss@example.com").reminder_count == 1
