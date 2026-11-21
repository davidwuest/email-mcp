import json

import pytest

import simple_email_mcp
from helpers import call
from fakes import FakeIMAP, make_message


@pytest.fixture
def mailbox(monkeypatch):
    fake = FakeIMAP()
    fake.add_folder("Archive")
    monkeypatch.setattr(simple_email_mcp, "_imap_connect", lambda acct: fake)
    return fake


def seed(fake):
    fake.add_message("INBOX", make_message(subject="foreign"), flags={"\\Deleted"}, uid=1)
    fake.add_message("INBOX", make_message(subject="target"), uid=2)
    fake.add_message("INBOX", make_message(subject="other"), uid=3)


def move(uid="2"):
    return call("move", {"uid": uid, "destination_folder": "Archive", "account": "alpha"})


@pytest.mark.parametrize("capabilities,method", [
    ({"IMAP4REV1", "MOVE"}, "move"),
    ({"IMAP4REV1", "IMAP4REV2"}, "move"),
    ({"IMAP4REV1", "UIDPLUS"}, "copy_expunge"),
    ({"IMAP4REV1"}, None),
])
def test_method_is_chosen_from_capabilities(capabilities, method):
    assert simple_email_mcp._move_method(frozenset(capabilities)) == method


def test_capabilities_use_the_last_response_not_the_pre_login_list(mailbox):
    mailbox.capability_data = [b"IMAP4rev1 STALE", b"IMAP4rev1 MOVE UIDPLUS"]
    assert simple_email_mcp._imap_capabilities(mailbox) == frozenset({"IMAP4REV1", "MOVE", "UIDPLUS"})


@pytest.mark.parametrize("capabilities,expected", [
    (("IMAP4REV1", "MOVE"), ["MOVE"]),
    (("IMAP4REV1", "IMAP4REV2"), ["MOVE"]),
    (("IMAP4REV1", "UIDPLUS"), ["COPY", "STORE", "EXPUNGE"]),
])
def test_move_leaves_foreign_deleted_messages_alone(mailbox, capabilities, expected):
    mailbox.server_capabilities = set(capabilities)
    seed(mailbox)
    result = json.loads(move())
    assert result["status"] == "moved"
    assert [c[1] for c in mailbox.mutating() if c[0] == "uid"] == expected
    assert mailbox.bare_expunges == 0
    assert mailbox.uids_of("INBOX") == [1, 3]
    assert "\\Deleted" in mailbox.flags_of("INBOX", 1)
    assert len(mailbox.folders["Archive"].messages) == 1


def test_expunge_names_exactly_the_moved_uid(mailbox):
    mailbox.server_capabilities = {"IMAP4REV1", "UIDPLUS"}
    seed(mailbox)
    move()
    assert mailbox.commands_named("EXPUNGE") == [("uid", "EXPUNGE", "2")]
    assert mailbox.commands_named("STORE") == [("uid", "STORE", "2", "+FLAGS.SILENT", "(\\Deleted)")]


def test_move_without_move_and_uidplus_changes_nothing(mailbox):
    mailbox.server_capabilities = {"IMAP4REV1"}
    seed(mailbox)
    result = move()
    assert result.startswith("Error: server supports neither MOVE nor UIDPLUS")
    assert mailbox.mutating() == []
    assert mailbox.uids_of("INBOX") == [1, 2, 3]
    assert mailbox.folders["Archive"].messages == {}


def test_capability_is_queried_after_the_session_is_open(mailbox):
    seed(mailbox)
    move()
    names = [c[0] for c in mailbox.commands]
    assert names.index("capability") > names.index("select")


def uidplus_mailbox(mailbox):
    mailbox.server_capabilities = {"IMAP4REV1", "UIDPLUS"}
    seed(mailbox)
    return mailbox


def assert_foreign_message_untouched(mailbox):
    assert mailbox.bare_expunges == 0
    assert 1 in mailbox.folders["INBOX"].messages
    assert "\\Deleted" in mailbox.flags_of("INBOX", 1)


def test_move_command_refused_by_the_server(mailbox):
    seed(mailbox)
    mailbox.fail_on("MOVE")
    assert move() == "Error: Could not move email to 'Archive'."
    assert mailbox.uids_of("INBOX") == [1, 2, 3]
    assert_foreign_message_untouched(mailbox)


def test_copy_refused_means_no_store_and_no_expunge(mailbox):
    uidplus_mailbox(mailbox).fail_on("COPY")
    assert move() == "Error: Could not copy email to 'Archive'."
    assert mailbox.commands_named("STORE", "EXPUNGE") == []
    assert mailbox.uids_of("INBOX") == [1, 2, 3]
    assert "\\Deleted" not in mailbox.flags_of("INBOX", 2)


def test_store_refused_after_copy_reports_both_copies(mailbox):
    uidplus_mailbox(mailbox).fail_on("STORE")
    result = move()
    assert result == ("Error: Email was copied to 'Archive' but could not be marked for removal "
                      "from 'INBOX'; it now exists in both folders.")
    assert mailbox.commands_named("EXPUNGE") == []
    assert 2 in mailbox.folders["INBOX"].messages
    assert_foreign_message_untouched(mailbox)


def test_expunge_refused_after_store_reports_the_remaining_flag(mailbox):
    uidplus_mailbox(mailbox).fail_on("EXPUNGE")
    result = move()
    assert result == ("Error: Email was copied to 'Archive' but could not be removed from 'INBOX'; "
                      "it remains there flagged \\Deleted.")
    assert 2 in mailbox.folders["INBOX"].messages
    assert_foreign_message_untouched(mailbox)


@pytest.mark.parametrize("command,capabilities", [
    ("MOVE", {"IMAP4REV1", "MOVE"}), ("COPY", {"IMAP4REV1", "UIDPLUS"}),
    ("STORE", {"IMAP4REV1", "UIDPLUS"}), ("EXPUNGE", {"IMAP4REV1", "UIDPLUS"}),
])
def test_bad_raised_as_exception_is_reported(mailbox, command, capabilities):
    mailbox.server_capabilities = capabilities
    seed(mailbox)
    mailbox.fail_on(command, bad=True)
    assert move().startswith("Error moving email:")
    assert_foreign_message_untouched(mailbox)


def test_capability_failure_changes_nothing(mailbox):
    seed(mailbox)
    mailbox.capability = lambda: ("NO", [b"nope"])
    assert move().startswith("Error moving email:")
    assert mailbox.mutating() == []


def test_the_right_message_moved(mailbox):
    seed(mailbox)
    move()
    assert b"Subject: target" in mailbox.folders["Archive"].messages[1]["raw"]
