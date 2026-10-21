"""4T: admin-managed email templates. Rendering and validation (app/email_templates.py), the admin
page with its preview, reset and audit entries, and that the emails built from the defaults read
exactly as the hard-coded wording they replaced."""

import pytest
from sqlmodel import select

from app import email_templates as et
from app import mail, outbox
from app.approvals import waive
from app.models import AuditLog, EmailTemplate, Message, Ticket, User, UserRole
from tests.conftest import login_as, set_role
from tests.test_approvals import answer, person, raised_and_triaged, request_type
from tests.test_email_preview import ticket_for_email
from tests.test_outbound_mail import dev_mail, rows  # noqa: F401  (dev_mail is a fixture)


def admin(client, db_session):
    set_role(db_session, login_as(client, "admin@example.com"), UserRole.SUPER_ADMIN)


def audit(db_session):
    db_session.expire_all()
    return db_session.exec(select(AuditLog).where(AuditLog.entity_type == "email_template").order_by(AuditLog.id)).all()


def save(client, key, body, subject="[{reference}] Approval needed: {subject}", relogin=True):
    if relogin:  # earlier steps of a test may have signed in as someone else
        login_as(client, "admin@example.com")
    return client.post(f"/admin/email-templates/{key}", data={"subject": subject, "body": body}, follow_redirects=False)


# ---- Rendering ------------------------------------------------------------------------------------


def test_placeholders_are_substituted_once_and_values_are_never_rescanned():
    assert et.render_text("Hi {a}, {b}", {"a": "{b}", "b": "x"}) == "Hi {b}, x"  # a's value is not expanded
    assert et.render_text("{unknown} stays", {"a": "x"}) == "{unknown} stays"
    assert et.render_text("{a.__class__} {0}", {"a": "x"}) == "{a.__class__} {0}"  # no attribute or index syntax


def test_nothing_in_admin_text_is_executed():
    out = et.render_text("{{ 7*7 }} {% if 1 %}x{% endif %} ${x} {a!r:>10}", {"a": "v"})
    assert out == "{{ 7*7 }} {% if 1 %}x{% endif %} ${x} {a!r:>10}"


def test_a_line_that_is_only_an_empty_placeholder_takes_its_blank_line_with_it():
    assert et.render_text("A\n\n{x}\n\nB", {"x": ""}) == "A\n\nB"
    assert et.render_text("A\n\n{x}\n\nB", {"x": "mid"}) == "A\n\nmid\n\nB"
    assert et.render_text("A {x} B", {"x": ""}) == "A  B"


# ---- Validation -----------------------------------------------------------------------------------


def test_an_unknown_placeholder_is_refused():
    defn = et.definition("approval_request")
    with pytest.raises(et.TemplateError, match=r"\{password\}"):
        et.validate(defn, "Subject", "Open {link} and tell {password}")
    with pytest.raises(et.TemplateError, match=r"\{link\}"):  # link is for the body only
        et.validate(defn, "Go to {link}", "Open {link}")


def test_a_required_placeholder_must_stay():
    defn = et.definition("approval_request")
    with pytest.raises(et.TemplateError, match=r"must include \{link\}"):
        et.validate(defn, "Subject", "Please approve this")


def test_a_pasted_approval_address_is_refused():
    defn = et.definition("approval_request")
    with pytest.raises(et.TemplateError, match="cannot be written"):
        et.validate(defn, "S", "{link} or https://tickets.example.org/approvals/abc")


def test_empty_and_overlong_text_is_refused():
    defn = et.definition("approval_waived")
    for body in ("   ", "x" * (et.MAX_BODY_LENGTH + 1)):
        with pytest.raises(et.TemplateError):
            et.validate(defn, None, body)
    with pytest.raises(et.TemplateError):
        et.validate(et.definition("approval_request"), "x" * (et.MAX_SUBJECT_LENGTH + 1), "{link}")
    with pytest.raises(et.TemplateError):
        et.validate(et.definition("approval_request"), "  ", "{link}")


def test_every_default_is_valid_and_previews_with_its_sample():
    for defn in et.REGISTRY.values():
        et.validate(defn, defn.subject_default, defn.body_default)
        preview = et.render_preview(defn, defn.subject_default, defn.body_default)
        assert "{" not in preview.body, defn.key  # every placeholder had a sample


def test_a_key_that_is_not_in_the_registry_is_refused():
    with pytest.raises(et.TemplateError):
        et.definition("nope")


# ---- Saving and resetting -------------------------------------------------------------------------


def test_saving_changes_the_next_email_and_reset_restores_the_default(client, db_session, dev_mail):
    admin(client, db_session)
    ticket = ticket_for_email(client, db_session)
    assert save(client, "raised_for", "Hello from {raised_by}: {reference}", subject="").status_code == 303

    [row] = rows(db_session, outbox.RAISED_FOR)
    assert mail.build_raised_for_mail(db_session, row).body == f"Hello from Agent Ann: {ticket.reference}"

    assert client.post("/admin/email-templates/raised_for/reset", follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(EmailTemplate, "raised_for") is None
    assert "has raised a ticket for you" in mail.build_raised_for_mail(db_session, row).body
    assert [(a.action, a.actor_email) for a in audit(db_session)] == [("edited", "admin@example.com"), ("reset", "admin@example.com")]


def test_a_bad_placeholder_cannot_be_saved(client, db_session):
    admin(client, db_session)
    response = save(client, "approval_approved", "Dear {nobody}")
    assert response.status_code == 400 and "{nobody}" in response.text
    assert save(client, "approval_request", "no link here").status_code == 400
    assert db_session.exec(select(EmailTemplate)).all() == [] and audit(db_session) == []


def test_saving_the_default_text_stores_nothing_and_an_unchanged_save_is_not_audited(client, db_session):
    admin(client, db_session)
    defn = et.definition("approval_waived")
    assert save(client, "approval_waived", defn.body_default).status_code == 303
    assert db_session.exec(select(EmailTemplate)).all() == [] and audit(db_session) == []

    save(client, "approval_waived", "Going ahead.")
    save(client, "approval_waived", "Going ahead.")  # same again
    [row] = db_session.exec(select(EmailTemplate)).all()
    assert (row.key, row.body, row.updated_by) == ("approval_waived", "Going ahead.", "admin@example.com")
    assert [a.action for a in audit(db_session)] == ["edited"]
    assert "text changed" in audit(db_session)[0].detail and "approval_waived" in audit(db_session)[0].detail


def test_resetting_what_was_never_changed_is_quiet_and_unknown_keys_are_404(client, db_session):
    admin(client, db_session)
    assert client.post("/admin/email-templates/approval_waived/reset", follow_redirects=False).status_code == 303
    assert audit(db_session) == []
    assert client.post("/admin/email-templates/nope/reset").status_code == 404
    assert save(client, "nope", "x").status_code == 404
    assert client.post("/admin/email-templates/nope/preview", data={"body": "x"}).status_code == 200  # shows the error


def test_only_a_super_admin_manages_templates(client, db_session):
    login_as(client, "agent@example.com")
    assert client.get("/admin/email-templates").status_code in (401, 403)
    assert save(client, "approval_waived", "x", relogin=False).status_code in (401, 403)
    assert client.post("/admin/email-templates/approval_waived/preview", data={"body": "x"}).status_code in (401, 403)
    assert db_session.exec(select(EmailTemplate)).all() == []


def test_a_template_that_stopped_validating_falls_back_to_the_default(db_session):
    db_session.add(EmailTemplate(key="approval_waived", subject="", body="Hi {gone}", updated_by="a@example.com"))
    db_session.commit()
    assert et.current_text(db_session, "approval_waived") == (None, et.definition("approval_waived").body_default)


# ---- The admin page and preview -------------------------------------------------------------------


def test_the_page_lists_every_template_with_a_sample_preview(client, db_session):
    admin(client, db_session)
    page = client.get("/admin/email-templates").text
    for defn in et.REGISTRY.values():
        assert defn.label in page
    assert "INC-1042" in page and "Reset to default" not in page
    save(client, "approval_waived", "Going ahead, {reference}.")
    page = client.get("/admin/email-templates").text
    assert "Reset to default" in page and "Going ahead, INC-1042." in page


def test_the_preview_shows_sample_data_or_the_problem_and_saves_nothing(client, db_session):
    admin(client, db_session)
    ok = client.post("/admin/email-templates/approval_declined/preview", data={"body": "No, sorry about {subject}.{reason_text}"})
    assert "No, sorry about Printer on floor 2 is jammed. Reason: Not in this year&#39;s budget." in ok.text
    bad = client.post("/admin/email-templates/approval_declined/preview", data={"body": "Dear {who}"})
    assert "{who}" in bad.text and "Preview with sample data" not in bad.text
    assert db_session.exec(select(EmailTemplate)).all() == [] and audit(db_session) == []


def test_the_audit_log_page_filters_on_templates(client, db_session):
    admin(client, db_session)
    save(client, "approval_waived", "Going ahead.")
    page = client.get("/admin/audit-log", params={"entity_type": "email_template"}).text
    assert "approval_waived" in page


# ---- Existing emails are unchanged by default ------------------------------------------------------


def test_the_raised_for_you_email_reads_as_it_always_did(client, db_session, dev_mail):
    ticket = ticket_for_email(client, db_session)
    [row] = rows(db_session, outbox.RAISED_FOR)
    built = mail.build_raised_for_mail(db_session, row)
    ticket = db_session.get(Ticket, ticket.id)
    creator = db_session.get(User, ticket.created_by_id)
    first = mail.own_first_message(db_session, ticket)
    expected = "\n".join([
        f"{creator.display_name if creator else 'IT Support'} has raised a ticket for you with IT Support.",
        "",
        f"Reference: {ticket.reference}",
        f"Subject: {ticket.subject}",
        *(["", first.body] if first else []),
        "",
        "Reply to this email to add anything else. Please keep the reference in the subject.",
    ])
    assert built.body == expected and built.record == expected


def test_the_approval_email_reads_as_it_always_did(client, db_session, dev_mail):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    [row] = rows(db_session, outbox.APPROVAL)
    first = mail.own_first_message(db_session, ticket)

    built = mail.build_approval_mail(db_session, row)

    link = next(line for line in built.body.splitlines() if line.startswith("https://"))
    head = [
        f"{ticket.requester_email} has asked for something that needs your approval.",
        "", f"Reference: {ticket.reference}", f"Subject: {ticket.subject}",
        "", "What they wrote (the requester's own words, not from IT):", "---", first.body.strip(), "---",
    ]
    tail = ["", "Open the link to approve or decline. Opening it decides nothing.", "The link works for 14 days."]
    assert built.body == "\n".join(head + ["", link] + tail)
    assert built.record == "\n".join(head + ["", "[answer link left out]"] + tail)
    assert built.subject == f"[{ticket.reference}] Approval needed: {ticket.subject}"


def test_an_edited_approval_template_changes_the_email_but_the_link_is_still_made_at_send_time(client, db_session, dev_mail):
    admin(client, db_session)
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    assert save(client, "approval_request", "Please decide on {reference} here: {link}", subject="Decide {ticket_number}").status_code == 303
    [row] = rows(db_session, outbox.APPROVAL)

    built = mail.build_approval_mail(db_session, row)

    assert built.subject == f"Decide {ticket.number}"
    assert built.body.startswith(f"Please decide on {ticket.reference} here: https://tickets.example.org/approvals/")
    assert built.record == f"Please decide on {ticket.reference} here: [answer link left out]"
    stored = db_session.get(EmailTemplate, "approval_request")
    assert "/approvals/" not in stored.body and "{link}" in stored.body  # the template never holds a link


def test_the_requesters_words_cannot_inject_a_placeholder_into_the_approval_email(client, db_session, dev_mail):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    first = db_session.exec(select(Message).where(Message.ticket_id == ticket.id)).first()
    first.body = "Look: {link}"
    db_session.add(first)
    db_session.commit()
    [row] = rows(db_session, outbox.APPROVAL)
    built = mail.build_approval_mail(db_session, row)
    assert "Look: {link}" in built.body and built.body.count("/approvals/") == 1


def test_an_edited_notice_is_what_the_requester_is_told_and_emailed(client, db_session, dev_mail):
    admin(client, db_session)
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    save(client, "approval_declined", "Sorry, {reference} is closed.{reason_text}")

    answer(client, ticket, "boss@example.com", "declined", "No budget")

    db_session.expire_all()
    [row] = rows(db_session, outbox.NOTICE)
    assert db_session.get(Message, row.message_id).body == f"Sorry, {ticket.reference} is closed. Reason: No budget"


def test_the_waived_notice_reads_as_it_always_did(client, db_session, dev_mail):
    person(client, db_session, "boss@example.com")
    ticket = raised_and_triaged(client, db_session, request_type(db_session, ["boss@example.com"]))
    admin_user = db_session.get(User, login_as(client, "admin@example.com"))
    waive(db_session, db_session.get(Ticket, ticket.id), admin_user)
    db_session.commit()
    [row] = rows(db_session, outbox.NOTICE)
    assert db_session.get(Message, row.message_id).body == (
        "Your request is going ahead: IT has let it through without waiting for further approval."
    )
