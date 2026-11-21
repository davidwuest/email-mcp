import json

import pytest

import simple_email_mcp
from helpers import call
from fakes import make_message


def seed(mailbox):
    mailbox.create("Archive")
    for subject in ("one", "two", "three"):
        mailbox.append("INBOX", make_message(subject=subject).replace(b"\n", b"\r\n"), "")
    mailbox.append("INBOX", make_message(subject="foreign").replace(b"\n", b"\r\n"), "(\\Deleted)")
    assert mailbox.uids("INBOX") == [1, 2, 3, 4]


def move_second(mailbox):
    return call("move", {"uid": "2", "destination_folder": "Archive", "account": "test"})


def move_three_and_one_missing(mailbox):
    return json.loads(call("move", {"uid": [2, 3, 99], "destination_folder": "Archive", "account": "test"}))


def assert_foreign_message_survived(mailbox):
    assert mailbox.uids("INBOX") == [1, 3, 4]
    assert "\\Deleted" in mailbox.flags("INBOX", 4)
    assert len(mailbox.uids("Archive")) == 1


def test_move_with_the_servers_own_capabilities(mailbox, monkeypatch):
    seen = []
    real = simple_email_mcp._imap_capabilities
    monkeypatch.setattr(simple_email_mcp, "_imap_capabilities", lambda conn: seen.append(real(conn)) or seen[-1])
    seed(mailbox)
    assert json.loads(move_second(mailbox))["status"] == "ok"
    assert simple_email_mcp._move_method(seen[0]) == "move"
    assert_foreign_message_survived(mailbox)


def test_move_via_copy_and_uid_expunge(mailbox, monkeypatch):
    monkeypatch.setattr(simple_email_mcp, "_imap_capabilities", lambda conn: frozenset({"IMAP4REV1", "UIDPLUS"}))
    seed(mailbox)
    assert json.loads(move_second(mailbox))["status"] == "ok"
    assert_foreign_message_survived(mailbox)


def test_move_is_refused_without_move_and_uidplus(mailbox, monkeypatch):
    monkeypatch.setattr(simple_email_mcp, "_imap_capabilities", lambda conn: frozenset({"IMAP4REV1"}))
    seed(mailbox)
    assert move_second(mailbox).startswith("Error: server supports neither MOVE nor UIDPLUS")
    assert mailbox.uids("INBOX") == [1, 2, 3, 4]
    assert mailbox.uids("Archive") == []


def test_bulk_move_with_a_missing_uid_on_the_servers_own_method(mailbox):
    seed(mailbox)
    mailbox.append("INBOX", make_message(subject="five").replace(b"\n", b"\r\n"), "")
    result = move_three_and_one_missing(mailbox)
    assert result["status"] == "partial" and result["moved"] == ["2", "3"] and result["not_found"] == ["99"]
    assert result["method"] == "move" and result["requested"] == 3
    assert mailbox.uids("INBOX") == [1, 4, 5]
    assert "\\Deleted" in mailbox.flags("INBOX", 4)
    assert len(mailbox.uids("Archive")) == 2


def test_bulk_move_via_copy_and_uid_expunge_keeps_the_foreign_message(mailbox, monkeypatch):
    monkeypatch.setattr(simple_email_mcp, "_imap_capabilities", lambda conn: frozenset({"IMAP4REV1", "UIDPLUS"}))
    seed(mailbox)
    result = move_three_and_one_missing(mailbox)
    assert result["status"] == "partial" and result["method"] == "copy_expunge"
    assert result["moved"] == ["2", "3"] and result["not_found"] == ["99"] and result["duplicated"] == []
    assert mailbox.uids("INBOX") == [1, 4]
    assert "\\Deleted" in mailbox.flags("INBOX", 4)


def test_bulk_mark_with_a_missing_uid(mailbox):
    seed(mailbox)
    result = json.loads(call("mark", {"uid": [1, 2, 99], "action": "flag", "account": "test"}))
    assert result["status"] == "partial" and result["marked"] == ["1", "2"] and result["not_found"] == ["99"]
    assert "\\Flagged" in mailbox.flags("INBOX", 1) and "\\Flagged" in mailbox.flags("INBOX", 2)
    assert "\\Flagged" not in mailbox.flags("INBOX", 3)
    cleared = json.loads(call("mark", {"uid": "1,2", "action": "unflag", "account": "test"}))
    assert cleared["status"] == "ok"
    assert "\\Flagged" not in mailbox.flags("INBOX", 1)
