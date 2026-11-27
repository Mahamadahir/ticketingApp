"""#343: the tidy view of an emailed message (app/mail_display.py): links labelled with their text and pointing at the URL as
received (Safe Links kept), the real host shown on a mismatch, tel: parts dropped, picture-name lines hidden. Display only."""

from urllib.parse import quote

import pytest

from app.mail_display import mail_html
from app.models import Message, MessageDirection
from tests.test_portal_request import make, say
from tests.test_ticket_thread import add, thread_html
from tests.test_sla import triaged_ticket
from tests.conftest import login_as

SAFE = "https://gbr01.safelinks.protection.outlook.com/?url=" + quote("https://vardags.com/", safe="") + "&data=05%7C02&reserved=0"
SIGNATURE = (
    "Mahamad Dahir\n\nIT Support Analyst\n\n[Vardags]\n\n"
    "Tel:  +44 (0)207 404 9390<tel:+44207%20404%209390>\n"
    f"Web:  vardags.com<{SAFE}>"
)


def href_of(html):
    return html.split('href="')[1].split('"')[0]


def test_the_signature_is_tidied():
    html = str(mail_html(SIGNATURE))
    assert 'Web:  <a href="' in html and ">vardags.com</a>" in html
    assert href_of(html).replace("&amp;", "&") == SAFE  # exactly as received, wrapper kept
    assert "&lt;" not in html and "tel:" not in html and "Tel:  +44 (0)207 404 9390\n" in html
    assert "[Vardags]" not in html and "Mahamad Dahir" in html
    assert "muted" not in html  # the label matches the real host
    assert 'rel="noopener noreferrer"' in html and 'target="_blank"' in html


@pytest.mark.parametrize("label", ["here", "example.org", "Click"])
def test_a_mismatch_shows_the_real_host(label):
    html = str(mail_html(f"See {label}<{SAFE}>"))
    assert f">{label}</a>" in html and '<span class="muted small">(vardags.com)</span>' in html


def test_a_plain_link_works_the_same_way():
    ok = str(mail_html("x vardags.com<https://www.vardags.com/a>"))
    assert 'href="https://www.vardags.com/a"' in ok and "muted" not in ok
    bad = str(mail_html("x paypal.com<https://evil.example/a>"))
    assert "(evil.example)" in bad and 'href="https://evil.example/a"' in bad


def test_a_bare_link_shows_the_decoded_address():
    html = str(mail_html(f"Go to <{SAFE}>"))
    assert ">https://vardags.com/</a>" in html and href_of(html).replace("&amp;", "&") == SAFE


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html;base64,AAAA", "file:///c:/x", "ftp://a.example/x"])
def test_other_schemes_are_not_links(url):
    html = str(mail_html(f"bad<{url}>"))
    assert "<a " not in html and "bad&lt;" in html


def test_mailto_is_a_link():
    html = str(mail_html("Write to ann<mailto:ann@example.com>"))
    assert 'href="mailto:ann@example.com"' in html and "(ann@example.com)" in html


def test_html_is_escaped():
    html = str(mail_html('<script>alert(1)</script> <b>x</b> a"b<https://a.example/"onmouseover=1>'))
    assert "<script" not in html and "<b>" not in html and "&lt;script&gt;" in html
    assert 'href="https://a.example/&#34;onmouseover=1"' in html and ' onmouseover' not in html


def test_brackets_in_sentences_are_kept_and_a_picture_line_is_hidden():
    assert str(mail_html("[URGENT] printer down\nsee [1]\n[1]")) == "[URGENT] printer down\nsee [1]\n[1]"
    assert str(mail_html("a\n  [Vardags]  \nb")) == "a\nb"
    assert str(mail_html("a\r\n[cid:image001.png@01D]\r\nb")) == "a\r\nb"
    assert str(mail_html(None)) == ""


# ---- the pages ---------------------------------------------------------------------------------------------------


def test_the_agent_page_tidies_an_emailed_reply_and_its_folded_quote(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    body = f"Thanks\n{SIGNATURE}\n\nOn Tue, 17 Nov 2026 at 10:15, IT <it@example.com> wrote:\n> old\n\n[Logo]\nSite  vardags.com<{SAFE}>"
    add(db_session, ticket, body, MessageDirection.INBOUND, channel="email", sender="Ann <me@example.com>")
    html = thread_html(client, ticket)
    assert html.count('rel="noopener noreferrer"') == 2 and "[Vardags]" not in html and "[Logo]" not in html
    assert "tel:" not in html and "&lt;https" not in html


def test_the_agent_page_leaves_an_agents_note_and_reply_alone(client, db_session):
    ticket, _ = triaged_ticket(client, db_session)
    add(db_session, ticket, "note vardags.com<https://vardags.com>\n[Keep]", MessageDirection.INTERNAL, channel="agent")
    add(db_session, ticket, "reply vardags.com<https://vardags.com>\n[Keep2]", MessageDirection.OUTBOUND, channel="email")
    html = thread_html(client, ticket)
    assert "<a href=\"https://vardags.com\"" not in html and "[Keep]" in html and "[Keep2]" in html


def test_the_portal_tidies_an_email_but_not_a_portal_comment(client, db_session):
    login_as(client, "jo@example.com", "Jo Lee")
    ticket = make(db_session)
    say(db_session, ticket, SIGNATURE, MessageDirection.INBOUND, sender="jo@example.com")
    say(db_session, ticket, "typed vardags.com<https://vardags.com>\n[Mine]", MessageDirection.INBOUND, sender="Jo Lee", channel="portal")
    page = client.get(f"/portal/{ticket.number}").text
    assert page.count('rel="noopener noreferrer"') == 1 and "[Vardags]" not in page
    assert "vardags.com&lt;https://vardags.com&gt;" in page and "[Mine]" in page


def test_the_stored_body_is_unchanged(client, db_session):
    login_as(client, "jo@example.com", "Jo Lee")
    ticket = make(db_session)
    msg = say(db_session, ticket, SIGNATURE, MessageDirection.INBOUND, sender="jo@example.com")
    client.get(f"/portal/{ticket.number}")
    db_session.expire_all()
    assert db_session.get(Message, msg.id).body == SIGNATURE


def test_punctuation_around_a_label_is_not_a_mismatch_and_a_spaced_tel_is_dropped():
    assert "muted" not in str(mail_html("see (vardags.com)<https://vardags.com/>"))
    assert "muted" not in str(mail_html("see vardags.com.<https://vardags.com/>"))
    assert str(mail_html("Tel: 0207 404 9390<tel:+44 207 404 9390>")) == "Tel: 0207 404 9390"


def test_a_message_of_only_a_bracketed_name_is_not_emptied():
    assert str(mail_html("[Vardags]")) == "[Vardags]"
