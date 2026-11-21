"""Calls the way the email-beantworten skill makes them must keep working (R31)."""
import json

import pytest

import simple_email_mcp
from helpers import call, call_json
from fakes import FakeIMAP, SmtpRecorder, make_message


@pytest.fixture
def env(monkeypatch, set_send_code):
    set_send_code("4711")
    fake = FakeIMAP()
    fake.add_folder("Archive")
    fake.add_folder("Sent", flags=("\\HasNoChildren", "\\Sent"))
    for uid in (42, 43):
        fake.add_message("INBOX", make_message(
            subject=f"Subject {uid}", cc="carol@example.test, alpha@example.test",
            message_id=f"<orig-{uid}@example.test>"), uid=uid)
    smtp = SmtpRecorder()
    monkeypatch.setattr(simple_email_mcp, "_imap_connect", lambda acct: fake)
    monkeypatch.setattr(simple_email_mcp, "_smtp_send", smtp)
    return fake, smtp


def test_search(env):
    result = call_json("search", {"query": "ALL", "folder": "INBOX", "account": "alpha"})
    assert result["count"] == 2
    assert [m["uid"] for m in result["emails"]] == ["43", "42"]
    assert result["folder"] == "INBOX"


@pytest.mark.parametrize("limit", [20, "20"])
def test_list_emails(env, limit):
    result = call_json("list_emails", {"limit": limit, "account": "alpha"})
    assert [m["uid"] for m in result["emails"]] == ["43", "42"]


def test_read(env):
    result = call_json("read", {"uid": "42", "folder": "INBOX", "account": "alpha"})
    assert result["subject"] == "Subject 42"
    assert result["uid"] == "42"
    assert "Original body" in result["body"]


def test_list_folders(env):
    result = call_json("list_folders", {"account": "alpha"})
    assert {"INBOX", "Archive", "Sent"} <= {f["raw_name"] for f in result["folders"]}


def sent_message(smtp):
    import email
    return email.message_from_string(smtp.sent[0]["mime"])


@pytest.mark.parametrize("action", ["reply", "reply_all"])
def test_reply(env, tmp_path, action):
    fake, smtp = env
    attachment = tmp_path / "note.txt"
    attachment.write_text("hello")
    result = call_json(action, {
        "uid": "42", "body": "Thanks", "body_html": "<p>Thanks</p>", "folder": "INBOX",
        "account": "alpha", "confirmation_code": "4711", "attachments": str(attachment)})
    assert result["status"] == "sent"
    assert len(smtp.sent) == 1
    message = sent_message(smtp)
    assert message["Subject"] == "Re: Subject 42"
    assert message["In-Reply-To"] == "<orig-42@example.test>"
    assert smtp.sent[0]["recipients"][0] == "sender@example.test"
    assert ("carol@example.test" in smtp.sent[0]["recipients"]) == (action == "reply_all")
    assert "alpha@example.test" not in smtp.sent[0]["recipients"]
    assert len(fake.folders["Sent"].messages) == 1


def test_send(env, tmp_path):
    fake, smtp = env
    attachment = tmp_path / "note.txt"
    attachment.write_text("hello")
    result = call_json("send", {
        "to": "dave@example.test", "subject": "Hi", "body": "Text", "body_html": "<p>Text</p>",
        "cc": "erin@example.test", "bcc": "frank@example.test", "attachments": str(attachment),
        "account": "alpha", "confirmation_code": "4711"})
    assert result["status"] == "sent"
    assert len(smtp.sent) == 1
    assert set(smtp.sent[0]["recipients"]) == {"dave@example.test", "erin@example.test", "frank@example.test"}
    message = sent_message(smtp)
    assert message["Cc"] == "erin@example.test"
    assert message["Bcc"] is None
    assert len(fake.folders["Sent"].messages) == 1


def test_forward(env):
    fake, smtp = env
    result = call_json("forward", {
        "uid": "42", "to": "dave@example.test", "body": "FYI", "folder": "INBOX",
        "account": "alpha", "confirmation_code": "4711"})
    assert result["status"] == "sent"
    assert smtp.sent[0]["recipients"] == ["dave@example.test"]
    assert sent_message(smtp)["Subject"] == "Fwd: Subject 42"


@pytest.mark.parametrize("action,params", [
    ("send", {"to": "dave@example.test", "subject": "Hi", "body": "Text"}),
    ("reply", {"uid": "42", "body": "Thanks"}),
    ("reply_all", {"uid": "42", "body": "Thanks"}),
    ("forward", {"uid": "42", "to": "dave@example.test"}),
])
@pytest.mark.parametrize("code", [None, "0000"])
def test_send_code_still_blocks(env, action, params, code):
    _, smtp = env
    payload = {**params, "account": "alpha"}
    if code:
        payload["confirmation_code"] = code
    assert call(action, payload).startswith("BLOCKED")
    assert smtp.sent == []


def test_move_has_effect(env):
    fake, _ = env
    result = call_json("move", {"uid": "42", "destination_folder": "Archive", "source_folder": "INBOX", "account": "alpha"})
    assert "error" not in result
    assert 42 not in fake.folders["INBOX"].messages
    assert len(fake.folders["Archive"].messages) == 1
    assert 43 in fake.folders["INBOX"].messages


@pytest.mark.parametrize("action,flag", [("flag", "\\Flagged"), ("read", "\\Seen")])
def test_mark_sets_flag(env, action, flag):
    fake, _ = env
    call("mark", {"uid": "42", "action": action, "account": "alpha"})
    assert flag in fake.flags_of("INBOX", 42)
    assert flag not in fake.flags_of("INBOX", 43)


@pytest.mark.parametrize("action,flag", [("unflag", "\\Flagged"), ("unread", "\\Seen")])
def test_mark_clears_flag(env, action, flag):
    fake, _ = env
    fake.folders["INBOX"].messages[42]["flags"].add(flag)
    call("mark", {"uid": "42", "action": action, "account": "alpha"})
    assert flag not in fake.flags_of("INBOX", 42)


def test_schema_query_still_works():
    result = call_json("move")
    assert result["action"] == "move"
    assert "destination_folder" in result["params"]
