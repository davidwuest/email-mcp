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


def assert_foreign_message_survived(mailbox):
    assert mailbox.uids("INBOX") == [1, 3, 4]
    assert "\\Deleted" in mailbox.flags("INBOX", 4)
    assert len(mailbox.uids("Archive")) == 1


def test_move_with_the_servers_own_capabilities(mailbox, monkeypatch):
    seen = []
    real = simple_email_mcp._imap_capabilities
    monkeypatch.setattr(simple_email_mcp, "_imap_capabilities", lambda conn: seen.append(real(conn)) or seen[-1])
    seed(mailbox)
    assert json.loads(move_second(mailbox))["status"] == "moved"
    assert simple_email_mcp._move_method(seen[0]) == "move"
    assert_foreign_message_survived(mailbox)


def test_move_via_copy_and_uid_expunge(mailbox, monkeypatch):
    monkeypatch.setattr(simple_email_mcp, "_imap_capabilities", lambda conn: frozenset({"IMAP4REV1", "UIDPLUS"}))
    seed(mailbox)
    assert json.loads(move_second(mailbox))["status"] == "moved"
    assert_foreign_message_survived(mailbox)


def test_move_is_refused_without_move_and_uidplus(mailbox, monkeypatch):
    monkeypatch.setattr(simple_email_mcp, "_imap_capabilities", lambda conn: frozenset({"IMAP4REV1"}))
    seed(mailbox)
    assert move_second(mailbox).startswith("Error: server supports neither MOVE nor UIDPLUS")
    assert mailbox.uids("INBOX") == [1, 2, 3, 4]
    assert mailbox.uids("Archive") == []
