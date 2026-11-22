import dataclasses
import json
import os

import pytest

import simple_email_mcp as m
from helpers import call, call_json

SEND = {"to": "dave@example.test", "subject": "Hi", "body": "Text"}

EXPECTED_PARAMS = {
    "validate_config": set(),
    "list_accounts": set(),
    "list_folders": {"account"},
    "list_emails": {"account", "folder", "limit"},
    "search": {"query", "account", "folder", "limit"},
    "read": {"uid", "account", "folder"},
    "send": {"to", "subject", "body", "account", "cc", "bcc", "body_html", "attachments",
             "attachments_inline", "calendar_ics", "confirmation_code", "reply_to_uid", "reply_to_folder", "reply_to"},
    "reply": {"uid", "body", "account", "folder", "body_html", "attachments", "attachments_inline",
              "confirmation_code", "cc", "bcc", "reply_to"},
    "reply_all": {"uid", "body", "account", "folder", "body_html", "attachments", "attachments_inline",
                  "confirmation_code", "cc", "bcc", "reply_to"},
    "forward": {"uid", "to", "account", "folder", "body", "body_html", "include_attachments",
                "attachments", "attachments_inline", "confirmation_code"},
    "move": {"uid", "destination_folder", "account", "source_folder"},
    "mark": {"uid", "action", "account", "folder"},
    "save_attachment": {"uid", "attachment_index", "save_path", "account", "folder", "overwrite"},
    "get_attachment": {"uid", "attachment_index", "account", "folder"},
    "prepare_attachments": {"attachments"},
}


def rejected(action, params):
    result = call_json(action, params)
    assert "problems" in result, result
    return result


def problem(result, name):
    return next(p for p in result["problems"] if p["param"] == name)


# ── schema query (R1, R6) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("action", sorted(EXPECTED_PARAMS))
def test_schema_query_lists_every_parameter(action):
    result = call_json(action)
    assert set(result["params"]) == EXPECTED_PARAMS[action]
    assert set(result) == {"action", "description", "params"}
    assert all(isinstance(text, str) and text for text in result["params"].values())


def test_schema_descriptions_are_generated_from_the_schema():
    params = call_json("list_emails")["params"]
    assert "20" in params["limit"] and "100" in params["limit"]
    assert "INBOX" in params["folder"]


# ── unknown names (R2) ────────────────────────────────────────────────────────

@pytest.mark.parametrize("action,params,name,suggestion", [
    ("move", {"uid": 1, "target_folder": "x"}, "target_folder", "destination_folder"),
    ("move", {"uid": 1, "destination": "x"}, "destination", "destination_folder"),
    ("move", {"uid": 1, "dest": "x"}, "dest", "destination_folder"),
    ("move", {"uid": 1, "destinationFolder": "x"}, "destinationFolder", "destination_folder"),
    ("send", {**SEND, "CC": "a@example.test"}, "CC", "cc"),
    ("send", {**SEND, "html": "<p>x</p>"}, "html", "body_html"),
    ("read", {"uids": 1}, "uids", "uid"),
    ("list_emails", {"limt": 5}, "limt", "limit"),
])
def test_unknown_names_get_a_suggestion(action, params, name, suggestion):
    result = rejected(action, params)
    entry = problem(result, name)
    assert entry["problem"] == "unknown_param"
    assert entry["did_you_mean"][0] == suggestion
    assert f"did you mean '{suggestion}'" in result["error"]


def test_folder_on_move_suggests_both_folder_parameters():
    entry = problem(rejected("move", {"uid": 1, "destination_folder": "a", "folder": "b"}), "folder")
    assert set(entry["did_you_mean"]) == {"source_folder", "destination_folder"}


def test_suggestions_only_name_parameters_the_action_knows():
    entry = problem(rejected("forward", {"uid": 1, "to": "a@example.test", "CC": "b@example.test"}), "CC")
    assert "cc" not in entry.get("did_you_mean", [])


def test_sender_gets_the_account_hint():
    entry = problem(rejected("send", {**SEND, "from": "x@example.test"}), "from")
    assert "account" in entry["hint"]


def test_without_a_suggestion_the_error_lists_the_valid_parameters():
    result = rejected("read", {"uid": 1, "zzzzqq": 1})
    assert "did_you_mean" not in problem(result, "zzzzqq")
    for name in EXPECTED_PARAMS["read"]:
        assert name in result["error"]


# ── actions (R5) ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("params", [None, {"uid": 1, "body": "x"}])
def test_unknown_action_gets_a_suggestion(params):
    result = call_json("replyall", params)
    assert "did you mean 'reply_all'" in result["error"]
    assert "reply_all" in result["available_actions"]


# ── types (R3) ────────────────────────────────────────────────────────────────

def values_of(action, params):
    values, problems = m._validate_params(action, params)
    assert problems == [], problems
    return values


def problems_of(action, params):
    return m._validate_params(action, params)[1]


SAVE = {"uid": 1, "attachment_index": 0, "save_path": "/tmp/x"}


@pytest.mark.parametrize("raw,expected", [("false", False), ("FALSE", False), ("true", True),
                                          ("True", True), (True, True), (False, False)])
def test_bool_accepts_unambiguous_spellings(raw, expected):
    assert values_of("save_attachment", {**SAVE, "overwrite": raw})["overwrite"] is expected


@pytest.mark.parametrize("raw", ["yes", "no", 0, 1, "1", "", [], {}])
def test_bool_rejects_everything_else(raw):
    problems = problems_of("forward", {"uid": 1, "to": "a@example.test", "include_attachments": raw})
    assert problems and problems[0]["problem"] == "wrong_type"


@pytest.mark.parametrize("raw,expected", [(20, 20), ("20", 20), (100, 100), (1, 1)])
def test_limit_accepts_integers_and_digit_strings(raw, expected):
    assert values_of("list_emails", {"limit": raw})["limit"] == expected


@pytest.mark.parametrize("raw", [0, 101, -1, "0", "101"])
def test_limit_is_range_checked_for_list_emails(raw):
    problems = problems_of("list_emails", {"limit": raw})
    assert problems[0]["problem"] == "out_of_range"
    assert problems[0]["min"] == 1 and problems[0]["max"] == 100


@pytest.mark.parametrize("raw", [True, 1.5, "abc", "+5", " 5", [5], {}])
def test_limit_rejects_other_types(raw):
    assert problems_of("list_emails", {"limit": raw})[0]["problem"] == "wrong_type"


def test_search_has_no_upper_limit_but_a_lower_one():
    assert values_of("search", {"query": "ALL", "limit": 500})["limit"] == 500
    assert problems_of("search", {"query": "ALL", "limit": 0})[0]["problem"] == "out_of_range"


@pytest.mark.parametrize("raw,expected", [("42", 42), (42, 42), (4294967295, 4294967295), ("1", 1)])
def test_uid_accepts_a_number_or_digit_string(raw, expected):
    assert values_of("read", {"uid": raw})["uid"] == expected


@pytest.mark.parametrize("raw", ["abc", "1:5", "*", "1,2", 0, "0", True, [1], 4294967296, -3, 2.5])
def test_uid_rejects_ranges_lists_and_junk(raw):
    assert problems_of("read", {"uid": raw})


def test_attachment_index_must_not_be_negative():
    assert problems_of("get_attachment", {"uid": 1, "attachment_index": -1})[0]["problem"] == "out_of_range"
    assert values_of("get_attachment", {"uid": 1, "attachment_index": "3"})["attachment_index"] == 3


def test_confirmation_code_must_be_a_string():
    problems = problems_of("send", {**SEND, "confirmation_code": 1234})
    assert problems[0]["problem"] == "wrong_type"
    assert "string" in problems[0]["hint"]


@pytest.mark.parametrize("field", ["subject", "confirmation_code"])
def test_single_line_fields_reject_line_breaks(field):
    problems = problems_of("send", {**SEND, field: "a\r\nBcc: evil@example.test"})
    assert problems[0]["problem"] == "invalid_value"


_CONTROL_CHARS = ["\x0b", "\x0c", "\x85", "\u2028", "\u2029", "\x1f", "\x7f", "\x00", "\r", "\n"]


@pytest.mark.parametrize("char", _CONTROL_CHARS)
@pytest.mark.parametrize("field", ["subject", "confirmation_code", "to", "cc", "bcc", "reply_to"])
def test_header_fields_reject_control_characters(field, char):
    value = "Anna%s <a@example.test>" % char if field in ("to", "cc", "bcc", "reply_to") else "a%sb" % char
    params = {**SEND, field: value}
    result = problems_of("send", params)
    assert result and result[0]["problem"] == "invalid_value"
    assert result[0]["param"] == field


def test_header_fields_accept_ordinary_text_and_the_body_stays_unrestricted():
    values = values_of("send", {**SEND, "subject": "Grüße \U0001F600 café",
                                "to": "Jürgen Müller <j@example.test>",
                                "body": "a\tb\x0bc\x85d\u2028e\r\nf"})
    assert values["subject"] == "Grüße \U0001F600 café"
    assert values["to"].pairs == (("Jürgen Müller", "j@example.test"),)
    assert values["body"] == "a\tb\x0bc\x85d\u2028e\r\nf"


@pytest.mark.parametrize("char", ["\r", "\n", "\x00", "\x0b", "\x1f", "\x7f"])
def test_search_query_rejects_control_characters(char):
    result = problems_of("search", {"query": "ALL%s" % char})
    assert result and result[0]["problem"] == "invalid_value" and result[0]["param"] == "query"


def test_search_query_rejects_an_injected_command_sequence():
    query = "ALL\r\nX1 UID STORE 1:* +FLAGS (\\Deleted)\r\nX2 EXPUNGE"
    assert problems_of("search", {"query": query})[0]["problem"] == "invalid_value"


def test_normal_search_query_is_accepted():
    assert values_of("search", {"query": "FROM john SINCE 01-Feb-2025"})["query"] == "FROM john SINCE 01-Feb-2025"


def test_strings_must_be_strings():
    assert problems_of("send", {**SEND, "body": 5})[0]["problem"] == "wrong_type"
    assert problems_of("search", {"query": ["ALL"]})[0]["problem"] == "wrong_type"


def test_defaults_are_filled_in():
    values = values_of("list_emails", {})
    assert values["folder"] == "INBOX" and values["limit"] == 20
    assert values_of("forward", {"uid": 1, "to": "a@example.test"})["include_attachments"] is True
    assert values_of("get_attachment", {"uid": 1, "attachment_index": 0})["folder"] == "INBOX"


def test_null_means_not_given():
    assert values_of("list_emails", {"limit": None, "folder": None})["limit"] == 20
    assert problems_of("read", {"uid": None})[0]["problem"] == "missing"


def test_blank_strings_count_as_absent_where_documented():
    values = values_of("send", {**SEND, "cc": "", "bcc": "", "body_html": "", "attachments": ""})
    assert not any(values.get(name) for name in ("cc", "bcc", "body_html", "attachments"))


def test_missing_required_parameters_are_all_reported():
    result = rejected("send", {})
    assert {p["param"] for p in result["problems"] if p["problem"] == "missing"} == {"to", "subject", "body"}


# ── addresses (R20) ───────────────────────────────────────────────────────────

def test_valid_address_lists_are_parsed():
    values = values_of("send", {**SEND, "to": "Anna <a@example.test>, b@example.test"})
    assert values["to"].pairs == (("Anna", "a@example.test"), ("", "b@example.test"))
    assert values["to"].raw == "Anna <a@example.test>, b@example.test"


@pytest.mark.parametrize("raw", ["Müller, Hans <h@example.test>", "a@example.test,, b@example.test",
                                 "not-an-address", "", "a@example.test; b@example.test"])
def test_invalid_address_lists_are_rejected_as_a_whole(raw):
    problems = problems_of("send", {**SEND, "to": raw})
    assert problems and problems[0]["problem"] == "invalid_address"


def test_line_breaks_in_addresses_are_rejected():
    assert problems_of("send", {**SEND, "to": "a@example.test\r\nBcc: x@example.test"})


# ── enumerations ──────────────────────────────────────────────────────────────

def test_mark_action_is_case_insensitive_and_normalized():
    assert values_of("mark", {"uid": 1, "action": "FLAG"})["action"] == "flag"


def test_mark_action_gets_a_suggestion():
    entry = problems_of("mark", {"uid": 1, "action": "flagg"})[0]
    assert entry["problem"] == "invalid_value"
    assert set(entry["allowed"]) == {"read", "unread", "flag", "unflag"}
    assert entry["did_you_mean"][0] == "flag"


# ── order and side effects (R4, R30, R7, R8) ──────────────────────────────────

def test_validation_reports_every_problem_at_once():
    result = rejected("read", {"uid": "abc", "folder": 5, "bogus": 1})
    assert {p["param"] for p in result["problems"]} == {"uid", "folder", "bogus"}


def test_nothing_happens_before_validation_has_passed(monkeypatch, tmp_path):
    touched = []
    for name in ("_check_confirmation_code", "_resolve_account", "_imap_connect", "_smtp_send", "_compose_and_send"):
        monkeypatch.setattr(m, name, lambda *a, _n=name, **k: touched.append(_n))
    monkeypatch.setattr(os.path, "isfile", lambda path: touched.append("isfile"))
    monkeypatch.setattr(os.path, "getsize", lambda path: touched.append("getsize"))
    result = call_json("send", {**SEND, "attachments": str(tmp_path / "x.txt"), "bogus": 1})
    assert "problems" in result
    assert touched == []
    assert "problems" in call_json("prepare_attachments", {"attachments": str(tmp_path / "x.txt"), "bogus": 1})
    assert touched == []


def test_send_code_comes_after_validation(set_send_code):
    set_send_code("4711")
    assert call("send", {**SEND, "account": "alpha"}).startswith("BLOCKED")
    assert "problems" in call_json("send", {**SEND, "account": "alpha", "bogus": 1})


def test_account_values_are_never_checked_or_suggested():
    assert problems_of("list_folders", {"account": "salse"}) == []
    result = rejected("list_folders", {"account": "alpha", "acount": "beta"})
    assert "beta" not in json.dumps(result)


def test_error_format_is_uniform_and_repeats_no_caller_values():
    for action, params in [("read", {"uid": "SECRET-VALUE"}), ("send", {**SEND, "subject": "SECRET-VALUE\r\n"}),
                           ("list_emails", {"limit": "SECRET-VALUE"})]:
        result = rejected(action, params)
        assert {"error", "problems", "action", "description", "params"} <= set(result)
        assert result["action"] == action
        assert "SECRET-VALUE" not in json.dumps(result)
        assert all(p["problem"] in {"unknown_param", "missing", "wrong_type", "out_of_range", "too_many",
                                    "invalid_value", "invalid_address", "conflict"} for p in result["problems"])


def test_wrong_type_names_the_expected_and_the_received_type():
    entry = problems_of("list_emails", {"limit": [1]})[0]
    assert entry["expected"] == "int" and entry["got"] == "array"


# ── extensibility (R32) ───────────────────────────────────────────────────────

def test_draft_switch_and_cross_field_rule_are_plain_schema_entries(monkeypatch):
    schema = m.ACTION_SCHEMAS["send"]

    def bcc_with_draft(values):
        if values.get("draft") and values.get("bcc"):
            return [{"param": "bcc", "problem": "conflict", "with": "draft"}]
        return []

    extended = dataclasses.replace(
        schema,
        params={**schema.params, "draft": m.Param("strict_bool", "save as draft instead of sending", default=False)},
        rules=schema.rules + (bcc_with_draft,))
    monkeypatch.setitem(m.ACTION_SCHEMAS, "send", extended)
    assert problems_of("send", {**SEND, "draft": "true"})[0]["problem"] == "wrong_type"
    conflict = problems_of("send", {**SEND, "draft": True, "bcc": "x@example.test"})[0]
    assert conflict["problem"] == "conflict" and conflict["with"] == "draft"
    assert values_of("send", {**SEND, "draft": True})["draft"] is True
    assert "draft" in call_json("send")["params"]


@pytest.mark.parametrize("action,params", [("read", {"uid": "9" * 5000}), ("list_emails", {"limit": "9" * 5000}),
                                           ("get_attachment", {"uid": 1, "attachment_index": "9" * 5000})])
def test_oversized_digit_strings_are_rejected_not_crashing(action, params):
    assert problems_of(action, params)[0]["problem"] == "out_of_range"


def test_empty_uid_is_rejected():
    assert problems_of("read", {"uid": ""})


@pytest.mark.parametrize("raw", ["a@example.test\x00", "a b@example.test"])
def test_addresses_with_nul_or_whitespace_are_rejected(raw):
    assert problems_of("send", {**SEND, "to": raw})[0]["problem"] in ("invalid_address", "invalid_value")


def test_subject_rejects_unicode_line_separators():
    assert problems_of("send", {**SEND, "subject": "a\u2028b"})[0]["problem"] == "invalid_value"


def test_unknown_action_does_not_echo_the_caller_string():
    assert "SECRET-ACTION" not in call("SECRET-ACTION")


@pytest.mark.parametrize("char", ["\x0b", "\x1f", "\x7f", "\x85", "\r", "\n", "\x00"])
def test_folder_names_reject_control_characters(char):
    problems = problems_of("list_emails", {"folder": "In%sbox" % char})
    assert problems and problems[0]["problem"] == "invalid_value" and problems[0]["param"] == "folder"


def test_folder_names_with_umlauts_are_accepted():
    assert values_of("list_emails", {"folder": "INBOX/Flüge Ärger"})["folder"] == "INBOX/Flüge Ärger"
