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
    assert result["status"] == "ok" and result["moved"] == ["2"]
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
    result = json.loads(move())
    assert result["status"] == "failed" and result["failed"] == ["2"] and result["moved"] == []
    assert "injected failure" in result["error"]
    assert mailbox.uids_of("INBOX") == [1, 2, 3]
    assert_foreign_message_untouched(mailbox)


def test_copy_refused_means_no_store_and_no_expunge(mailbox):
    uidplus_mailbox(mailbox).fail_on("COPY")
    result = json.loads(move())
    assert result["status"] == "failed" and result["failed"] == ["2"] and result["duplicated"] == []
    assert mailbox.commands_named("STORE", "EXPUNGE") == []
    assert mailbox.uids_of("INBOX") == [1, 2, 3]
    assert "\\Deleted" not in mailbox.flags_of("INBOX", 2)


def test_store_refused_after_copy_reports_the_duplicate_and_undoes_nothing_else(mailbox):
    uidplus_mailbox(mailbox).fail_on("STORE", times=1)
    result = json.loads(move())
    assert result["status"] == "failed" and result["duplicated"] == ["2"] and result["moved"] == []
    assert mailbox.commands_named("EXPUNGE") == []
    assert 2 in mailbox.folders["INBOX"].messages and len(mailbox.folders["Archive"].messages) == 1
    assert "\\Deleted" not in mailbox.flags_of("INBOX", 2)
    assert_foreign_message_untouched(mailbox)


def test_expunge_refused_after_store_clears_the_flag_again(mailbox):
    uidplus_mailbox(mailbox).fail_on("EXPUNGE")
    result = json.loads(move())
    assert result["duplicated"] == ["2"]
    assert ("uid", "STORE", "2", "-FLAGS.SILENT", "(\\Deleted)") in mailbox.commands
    assert "\\Deleted" not in mailbox.flags_of("INBOX", 2)
    assert_foreign_message_untouched(mailbox)


def test_a_failed_cleanup_is_reported(mailbox):
    uidplus_mailbox(mailbox).fail_on("EXPUNGE")
    original_store = mailbox._uid_store

    def store_that_cannot_clean_up(uid_set, op, flags):
        if op.startswith("-"):
            return "NO", [b"cleanup refused"]
        return original_store(uid_set, op, flags)

    mailbox._uid_store = store_that_cannot_clean_up
    result = json.loads(move())
    assert result["duplicated"] == ["2"]
    assert "\\Deleted flag" in result["error"]


@pytest.mark.parametrize("command,capabilities", [
    ("MOVE", {"IMAP4REV1", "MOVE"}), ("COPY", {"IMAP4REV1", "UIDPLUS"}),
    ("STORE", {"IMAP4REV1", "UIDPLUS"}), ("EXPUNGE", {"IMAP4REV1", "UIDPLUS"}),
])
def test_bad_raised_as_exception_is_handled_like_no(mailbox, command, capabilities):
    mailbox.server_capabilities = capabilities
    seed(mailbox)
    mailbox.fail_on(command, bad=True, times=1)
    result = json.loads(move())
    assert result["status"] == "failed" and result["error"]
    assert result["failed"] + result["duplicated"] == ["2"]
    assert_foreign_message_untouched(mailbox)


def test_capability_failure_changes_nothing(mailbox):
    seed(mailbox)
    mailbox.capability = lambda: ("NO", [b"nope"])
    assert move().startswith("Error moving emails:")
    assert mailbox.mutating() == []


def test_the_right_message_moved(mailbox):
    seed(mailbox)
    move()
    assert b"Subject: target" in mailbox.folders["Archive"].messages[1]["raw"]


# ── bulk move (R23, R24, R28) ─────────────────────────────────────────────────

def bulk_seed(fake, count=5):
    fake.add_message("INBOX", make_message(subject="foreign"), flags={"\\Deleted"}, uid=1)
    for uid in range(2, count + 1):
        fake.add_message("INBOX", make_message(subject=f"m{uid}"), uid=uid)


def move_many(uid, **extra):
    return json.loads(call("move", {"uid": uid, "destination_folder": "Archive", "account": "alpha", **extra}))


def test_move_result_format_for_a_complete_move(mailbox):
    bulk_seed(mailbox)
    result = move_many([2, 3])
    assert list(result) == ["status", "account", "source_folder", "destination_folder", "method", "requested",
                            "moved", "not_found", "failed", "duplicated"]
    assert result == {"status": "ok", "account": "alpha@example.test", "source_folder": "INBOX",
                      "destination_folder": "Archive", "method": "move", "requested": 2,
                      "moved": ["2", "3"], "not_found": [], "failed": [], "duplicated": []}
    assert mailbox.uids_of("INBOX") == [1, 4, 5]
    assert mailbox.commands_named("MOVE") == [("uid", "MOVE", "2,3", '"Archive"')]


def test_missing_uids_do_not_stop_the_others(mailbox):
    bulk_seed(mailbox)
    result = move_many(["2", 99, 3])
    assert result["status"] == "partial" and result["requested"] == 3
    assert result["moved"] == ["2", "3"] and result["not_found"] == ["99"]
    assert "error" not in result
    assert_foreign_message_untouched(mailbox)


def test_nothing_found_means_failed_and_nothing_changes(mailbox):
    bulk_seed(mailbox)
    result = move_many([98, 99])
    assert result["status"] == "failed" and result["moved"] == [] and result["not_found"] == ["98", "99"]
    assert mailbox.mutating() == []


def test_uid_list_spellings_and_duplicates(mailbox):
    bulk_seed(mailbox)
    result = move_many("2, 3,2")
    assert result["requested"] == 2 and result["moved"] == ["2", "3"]


def test_five_hundred_uids_go_in_one_short_command(mailbox):
    for uid in range(1, 501):
        mailbox.add_message("INBOX", make_message(), uid=1_000_000_000 + uid)
    uids = [1_000_000_000 + uid for uid in range(1, 501)]
    result = move_many(uids)
    assert result["status"] == "ok" and result["requested"] == 500
    (command,) = mailbox.commands_named("MOVE")
    assert len(command[2]) < 8192 - 100
    assert mailbox.uids_of("INBOX") == []


def test_more_than_five_hundred_distinct_uids_are_rejected_without_touching_the_server():
    result = json.loads(call("move", {"uid": list(range(1, 502)), "destination_folder": "Archive", "account": "alpha"}))
    entry = next(p for p in result["problems"] if p["param"] == "uid")
    assert entry == {"param": "uid", "problem": "too_many", "max": 500, "got": 501}


def test_five_hundred_distinct_uids_pass_validation():
    values, problems = simple_email_mcp._validate_params("move", {"uid": list(range(1, 501)), "destination_folder": "A"})
    assert problems == [] and len(values["uid"]) == 500


@pytest.mark.parametrize("raw,expected", [
    (42, [42]), ("42", [42]), ("101, 102,103", [101, 102, 103]), ([1, "2", "2", 1], [1, 2]),
])
def test_uid_list_coercion(raw, expected):
    assert simple_email_mcp._validate_params("mark", {"uid": raw, "action": "read"})[0]["uid"] == expected


@pytest.mark.parametrize("raw", ["1:5", "*", "", [], [0], [1, "x"], [[1]], True, "1,,2", "1, *", [True], {"a": 1}])
def test_uid_list_rejects_ranges_and_junk(raw):
    assert simple_email_mcp._validate_params("mark", {"uid": raw, "action": "read"})[1]


def test_schema_describes_the_list_and_the_limit():
    assert "500" in json.loads(call("move"))["params"]["uid"]
    assert "list" in json.loads(call("mark"))["params"]["uid"]


def test_partial_move_command_reports_what_stayed_behind(mailbox):
    bulk_seed(mailbox)
    mailbox.fail_on("MOVE", apply_first=1)
    result = move_many([2, 3])
    assert result["status"] == "partial" and result["moved"] == ["2"] and result["failed"] == ["3"]
    assert result["error"]
    assert_foreign_message_untouched(mailbox)


def test_partial_copy_expunge_reports_the_duplicate_and_cleans_its_flag(mailbox):
    mailbox.server_capabilities = {"IMAP4REV1", "UIDPLUS"}
    bulk_seed(mailbox)
    mailbox.fail_on("EXPUNGE", apply_first=1)
    result = move_many([2, 3])
    assert result["status"] == "partial" and result["moved"] == ["2"] and result["duplicated"] == ["3"]
    assert result["method"] == "copy_expunge"
    assert "\\Deleted" not in mailbox.flags_of("INBOX", 3)
    assert "\\Deleted" in mailbox.flags_of("INBOX", 1)
    assert mailbox.commands_named("EXPUNGE") == [("uid", "EXPUNGE", "2,3")]


# ── bulk mark ─────────────────────────────────────────────────────────────────

def mark_many(uid, action="flag", **extra):
    return json.loads(call("mark", {"uid": uid, "action": action, "account": "alpha", **extra}))


def test_mark_result_format(mailbox):
    bulk_seed(mailbox)
    result = mark_many([2, 3, 99])
    assert result == {"status": "partial", "account": "alpha@example.test", "folder": "INBOX", "mark": "flag",
                      "requested": 3, "marked": ["2", "3"], "not_found": ["99"], "failed": []}
    assert "\\Flagged" in mailbox.flags_of("INBOX", 2) and "\\Flagged" in mailbox.flags_of("INBOX", 3)
    assert mailbox.commands_named("STORE") == [("uid", "STORE", "2,3", "+FLAGS.SILENT", "(\\Flagged)")]


@pytest.mark.parametrize("action,flag,op", [("read", "\\Seen", "+"), ("unread", "\\Seen", "-"),
                                            ("flag", "\\Flagged", "+"), ("unflag", "\\Flagged", "-")])
def test_mark_flags(mailbox, action, flag, op):
    bulk_seed(mailbox, 3)
    if op == "-":
        for uid in (2, 3):
            mailbox.folders["INBOX"].messages[uid]["flags"].add(flag)
    result = mark_many([2, 3], action)
    assert result["status"] == "ok" and result["mark"] == action
    assert all((flag in mailbox.flags_of("INBOX", uid)) == (op == "+") for uid in (2, 3))
    assert flag not in mailbox.flags_of("INBOX", 1) or op == "-"


def test_mark_with_nothing_found_is_failed(mailbox):
    bulk_seed(mailbox)
    result = mark_many([98])
    assert result["status"] == "failed" and result["marked"] == [] and result["not_found"] == ["98"]
    assert mailbox.mutating() == []


def test_mark_checks_the_flags_after_a_refused_store(mailbox):
    bulk_seed(mailbox)
    mailbox.fail_on("STORE", apply_first=1)
    result = mark_many([2, 3])
    assert result["status"] == "partial" and result["marked"] == ["2"] and result["failed"] == ["3"]
    assert result["error"]
    assert mailbox.commands_named("FETCH") == [("uid", "FETCH", "2,3", "(FLAGS)")]


def test_mark_store_raising_bad_is_handled_like_no(mailbox):
    bulk_seed(mailbox)
    mailbox.fail_on("STORE", bad=True)
    result = mark_many([2])
    assert result["status"] == "failed" and result["failed"] == ["2"]


def test_mark_opens_the_folder_writable(mailbox):
    bulk_seed(mailbox)
    mark_many([2])
    assert ("select", '"INBOX"', False) in mailbox.commands


def test_foreign_deleted_message_is_untouched_by_marking(mailbox):
    bulk_seed(mailbox)
    mark_many([2, 3], "read")
    assert_foreign_message_untouched(mailbox)


# ── recovery when even the follow-up lookup fails ─────────────────────────────

def test_move_reports_everything_failed_when_the_state_cannot_be_verified(mailbox):
    bulk_seed(mailbox)
    mailbox.fail_on("MOVE")
    mailbox.fail_on("SEARCH", skip=1)
    result = move_many([2, 3])
    assert result["status"] == "failed" and result["failed"] == ["2", "3"] and result["moved"] == []
    assert "could not verify" in result["error"]


def test_copy_path_still_cleans_up_when_the_state_cannot_be_verified(mailbox):
    mailbox.server_capabilities = {"IMAP4REV1", "UIDPLUS"}
    bulk_seed(mailbox)
    mailbox.fail_on("EXPUNGE")
    mailbox.fail_on("SEARCH", skip=1)
    result = move_many([2, 3])
    assert result["duplicated"] == ["2", "3"] and result["moved"] == []
    assert "could not verify" in result["error"]
    assert "\\Deleted" not in mailbox.flags_of("INBOX", 2) and "\\Deleted" not in mailbox.flags_of("INBOX", 3)
    assert_foreign_message_untouched(mailbox)


def test_a_flag_the_user_set_before_the_move_is_not_cleared_by_the_cleanup(mailbox):
    mailbox.server_capabilities = {"IMAP4REV1", "UIDPLUS"}
    bulk_seed(mailbox)
    mailbox.folders["INBOX"].messages[2]["flags"].add("\\Deleted")
    mailbox.fail_on("EXPUNGE")
    result = move_many([2, 3])
    assert result["duplicated"] == ["2", "3"]
    assert "\\Deleted" in mailbox.flags_of("INBOX", 2)
    assert "\\Deleted" not in mailbox.flags_of("INBOX", 3)


def test_mark_flag_comparison_ignores_case(mailbox):
    bulk_seed(mailbox)
    mailbox.fail_on("STORE")
    mailbox.folders["INBOX"].messages[2]["flags"].add("\\flagged")
    assert mark_many([2], "flag")["marked"] == ["2"]


def test_server_error_text_is_truncated(mailbox):
    bulk_seed(mailbox)
    mailbox.fail_on("MOVE")
    original = mailbox._uid_move
    mailbox._uid_move = lambda *a: ("NO", [b"x" * 5000])
    assert len(move_many([2])["error"]) <= 300
    mailbox._uid_move = original
