import email
from email.header import decode_header

import pytest

import simple_email_mcp as m
from helpers import call, call_json
from fakes import FakeIMAP, SmtpRecorder, make_message

OWN = {"alpha@example.test", "team@example.test"}


def pairs(*addresses):
    return [("", a) for a in addresses]


# ── merging (R18, R19) ────────────────────────────────────────────────────────

def merge(to="sender@example.test", auto=(), cc=(), bcc=(), own=OWN):
    return m._merge_reply_recipients(to, pairs(*auto), list(cc), list(bcc), own)


def test_extras_are_added_to_the_automatic_cc_in_order():
    cc, bcc, dropped = merge(auto=["carol@example.test"], cc=pairs("erin@example.test"))
    assert [a for _, a in cc] == ["carol@example.test", "erin@example.test"]
    assert bcc == [] and dropped == []


def test_duplicates_are_removed_ignoring_case_and_reported():
    cc, _, dropped = merge(auto=["carol@example.test"], cc=pairs("Carol@Example.test", "erin@example.test"))
    assert [a for _, a in cc] == ["carol@example.test", "erin@example.test"]
    assert dropped == [{"address": "Carol@Example.test", "field": "cc", "reason": "duplicate"}]


def test_precedence_is_to_then_cc_then_bcc():
    cc, bcc, dropped = merge(cc=pairs("Sender@example.test", "erin@example.test"),
                             bcc=pairs("erin@example.test", "frank@example.test", "frank@example.test"))
    assert [a for _, a in cc] == ["erin@example.test"]
    assert [a for _, a in bcc] == ["frank@example.test"]
    assert dropped == [
        {"address": "Sender@example.test", "field": "cc", "reason": "duplicate"},
        {"address": "erin@example.test", "field": "bcc", "reason": "duplicate"},
        {"address": "frank@example.test", "field": "bcc", "reason": "duplicate"},
    ]


def test_own_addresses_of_the_sending_account_are_dropped_and_reported():
    cc, bcc, dropped = merge(cc=pairs("TEAM@example.test"), bcc=pairs("alpha@example.test"))
    assert cc == [] and bcc == []
    assert dropped == [{"address": "TEAM@example.test", "field": "cc", "reason": "own_address"},
                       {"address": "alpha@example.test", "field": "bcc", "reason": "own_address"}]


def test_addresses_of_other_configured_accounts_are_kept():
    cc, bcc, dropped = merge(cc=pairs("beta@example.test"), bcc=pairs("beta@example.test"))
    assert [a for _, a in cc] == ["beta@example.test"]
    assert bcc == [] and dropped[0]["reason"] == "duplicate"


def test_automatic_recipients_that_are_own_or_in_to_vanish_silently():
    cc, _, dropped = merge(auto=["alpha@example.test", "sender@example.test", "carol@example.test"])
    assert [a for _, a in cc] == ["carol@example.test"]
    assert dropped == []


def test_display_names_of_caller_addresses_survive():
    cc, _, _ = merge(cc=[("Erin Example", "erin@example.test")])
    assert cc == [("Erin Example", "erin@example.test")]
    assert m._format_address_header(cc) == "Erin Example <erin@example.test>"


def test_own_addresses_come_from_the_sending_account():
    acct = {"address": "Alpha@Example.test", "send_as": "Team@Example.test"}
    assert m._own_addresses(acct) == {"alpha@example.test", "team@example.test"}


# ── handlers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def env(monkeypatch, set_send_code):
    set_send_code("4711")
    fake = FakeIMAP()
    fake.add_folder("Sent", flags=("\\HasNoChildren", "\\Sent"))
    fake.add_message("INBOX", make_message(
        sender="Sender <sender@example.test>", to="alpha@example.test, bob@example.test",
        cc="carol@example.test", message_id="<orig-1@example.test>"), uid=1)
    smtp = SmtpRecorder()
    monkeypatch.setattr(m, "_imap_connect", lambda acct: fake)
    monkeypatch.setattr(m, "_smtp_send", smtp)
    return fake, smtp


def sent(smtp):
    return email.message_from_string(smtp.sent[0]["mime"])


def reply(action, **extra):
    return call_json(action, {"uid": "1", "body": "Thanks", "account": "alpha", "confirmation_code": "4711", **extra})


def test_reply_delivers_cc_and_bcc_to_everyone(env):
    _, smtp = env
    result = reply("reply", cc="erin@example.test", bcc="frank@example.test")
    assert result["status"] == "sent"
    assert set(smtp.sent[0]["recipients"]) == {"sender@example.test", "erin@example.test", "frank@example.test"}
    message = sent(smtp)
    assert message["Cc"] == "erin@example.test"
    assert message["Bcc"] is None and "frank@example.test" not in message.as_string()
    assert result["cc"] == "erin@example.test" and result["bcc"] == "frank@example.test"


def test_reply_all_keeps_the_automatic_cc_and_adds_the_extras(env):
    _, smtp = env
    result = reply("reply_all", cc="erin@example.test, Bob <bob@example.test>", bcc="frank@example.test")
    message = sent(smtp)
    assert message["Cc"] == "bob@example.test, carol@example.test, erin@example.test"
    assert set(smtp.sent[0]["recipients"]) == {"sender@example.test", "bob@example.test", "carol@example.test",
                                               "erin@example.test", "frank@example.test"}
    assert len(smtp.sent[0]["recipients"]) == 5
    assert result["dropped"] == [{"address": "bob@example.test", "field": "cc", "reason": "duplicate"}]
    assert message["Bcc"] is None


def test_reply_all_without_extras_behaves_as_before(env):
    _, smtp = env
    result = reply("reply_all")
    assert sent(smtp)["Cc"] == "bob@example.test, carol@example.test"
    assert "dropped" not in result and "bcc" not in result


def test_reply_all_reports_what_was_dropped(env):
    _, smtp = env
    result = reply("reply_all", cc="team@example.test, carol@example.test")
    assert {(d["field"], d["reason"]) for d in result["dropped"]} == {("cc", "own_address"), ("cc", "duplicate")}


def test_send_code_still_blocks_a_reply_with_cc(env):
    _, smtp = env
    result = call("reply", {"uid": "1", "body": "x", "cc": "erin@example.test", "account": "alpha"})
    assert result.startswith("BLOCKED")
    assert smtp.sent == []


@pytest.mark.parametrize("action,params", [
    ("send", {"to": "dave@example.test", "subject": "Hi", "body": "Text"}),
    ("reply", {"uid": "1", "body": "x"}),
    ("reply_all", {"uid": "1", "body": "x"}),
])
def test_reply_to_header_is_set_without_comparing_it_to_own_addresses(env, action, params):
    _, smtp = env
    result = call_json(action, {**params, "reply_to": "team@example.test", "account": "alpha", "confirmation_code": "4711"})
    assert result["status"] == "sent"
    assert sent(smtp)["Reply-To"] == "team@example.test"
    assert result["reply_to"] == "team@example.test"


def test_reply_to_display_name_is_rfc2047_encoded_but_the_address_is_not(env):
    _, smtp = env
    call_json("send", {"to": "dave@example.test", "subject": "Hi", "body": "Text", "account": "alpha",
                       "reply_to": "Jörg Müller <j@example.test>", "confirmation_code": "4711"})
    header = smtp.sent[0]["mime"].split("Reply-To: ", 1)[1].split("\n", 1)[0]
    assert header.isascii() and header.endswith("<j@example.test>")
    fragments = decode_header(header.rsplit("<", 1)[0].strip())
    assert fragments[0][0].decode(fragments[0][1]) == "Jörg Müller"


def test_no_reply_to_header_unless_requested(env):
    _, smtp = env
    reply("reply")
    assert sent(smtp)["Reply-To"] is None


def test_line_breaks_in_reply_to_are_rejected_before_anything_happens(env):
    _, smtp = env
    result = call_json("reply", {"uid": "1", "body": "x", "reply_to": "a@example.test\r\nBcc: x@example.test",
                                 "account": "alpha", "confirmation_code": "4711"})
    assert "problems" in result and smtp.sent == []


def test_a_numeric_reply_to_suggests_reply_to_uid(env):
    result = call_json("send", {"to": "dave@example.test", "subject": "Hi", "body": "Text", "reply_to": "4711"})
    entry = next(p for p in result["problems"] if p["param"] == "reply_to")
    assert entry["problem"] == "invalid_address" and entry["did_you_mean"] == ["reply_to_uid"]


@pytest.mark.parametrize("name", ["reply_to_address", "replyTo", "replyto"])
@pytest.mark.parametrize("action", ["send", "reply", "reply_all"])
def test_reply_to_spellings_get_a_suggestion(action, name):
    base = {"to": "d@example.test", "subject": "s", "body": "b"} if action == "send" else {"uid": 1, "body": "b"}
    result = call_json(action, {**base, name: "x@example.test"})
    assert next(p for p in result["problems"] if p["param"] == name)["did_you_mean"] == ["reply_to"]


@pytest.mark.parametrize("name", ["cc", "bcc", "reply_to"])
def test_forward_takes_no_cc_bcc_or_reply_to(env, name):
    result = call_json("forward", {"uid": "1", "to": "dave@example.test", name: "x@example.test", "account": "alpha"})
    assert next(p for p in result["problems"] if p["param"] == name)["problem"] == "unknown_param"
