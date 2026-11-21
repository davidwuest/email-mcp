import json
import unicodedata

import pytest

import simple_email_mcp as m
from helpers import call, call_json
from fakes import FakeIMAP, SmtpRecorder, make_message

NFC_FLUEGE = unicodedata.normalize("NFC", "Flüge")


def folder(raw_name, flags=(), delimiter="/"):
    return {"name": unicodedata.normalize("NFC", m._decode_imap_utf7(raw_name)), "raw_name": raw_name,
            "delimiter": delimiter, "flags": tuple(flags)}


# ── LIST parser ───────────────────────────────────────────────────────────────

def test_parser_reads_the_migadu_style_line():
    parsed = m._parse_imap_list_response([b'(\\HasNoChildren) "/" "INBOX/Flu&Awg-ge"'])
    assert parsed[0]["raw_name"] == "INBOX/Flu&Awg-ge"
    assert parsed[0]["delimiter"] == "/"
    assert parsed[0]["flags"] == ("\\HasNoChildren",)
    assert parsed[0]["name"] == "INBOX/" + NFC_FLUEGE
    assert unicodedata.is_normalized("NFC", parsed[0]["name"])


def test_parser_reads_atoms_without_quotes():
    parsed = m._parse_imap_list_response([b'() "/" INBOX', b'(\\Sent) "." Sent'])
    assert [(f["raw_name"], f["delimiter"], f["flags"]) for f in parsed] == [
        ("INBOX", "/", ()), ("Sent", ".", ("\\Sent",))]


def test_parser_unescapes_quoted_names():
    parsed = m._parse_imap_list_response([rb'() "/" "Quo\"te\\Back"'])
    assert parsed[0]["raw_name"] == 'Quo"te\\Back'


def test_parser_reads_literals_and_skips_the_rest_element():
    raw = [(b'(\\HasNoChildren) "/" {8}', b"Lit eral"), b"", b'() "/" INBOX']
    parsed = m._parse_imap_list_response(raw)
    assert [f["raw_name"] for f in parsed] == ["Lit eral", "INBOX"]


def test_parser_accepts_nil_delimiter():
    parsed = m._parse_imap_list_response([b"(\\Noselect) NIL Shared"])
    assert parsed[0]["delimiter"] is None
    assert parsed[0]["raw_name"] == "Shared"


def test_parser_keeps_the_last_duplicate():
    parsed = m._parse_imap_list_response([b'(\\A) "/" Dup', b'(\\B) "/" Dup'])
    assert len(parsed) == 1 and parsed[0]["flags"] == ("\\B",)


def test_parser_skips_a_non_ascii_name_and_keeps_the_rest():
    parsed = m._parse_imap_list_response(['() "/" "Flüge"'.encode("utf-8"), b'() "/" INBOX'])
    assert [f["raw_name"] for f in parsed] == ["INBOX"]


def test_parser_keeps_extension_data_out_of_the_record():
    parsed = m._parse_imap_list_response([b'(\\X) "/" INBOX/Fl&APw-ge ("OLDNAME" (INBOX/Flu&Awg-ge))'])
    assert parsed == [{"name": "INBOX/Flüge", "raw_name": "INBOX/Fl&APw-ge", "delimiter": "/", "flags": ("\\X",)}]


def test_parser_handles_a_literal_followed_by_extension_data():
    raw = [(b'(\\X) "/" {3}', b"Abc"), b' ("OLDNAME" (Old))', b'() "/" INBOX']
    assert [f["raw_name"] for f in m._parse_imap_list_response(raw)] == ["Abc", "INBOX"]


# ── modified UTF-7 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("encoded,decoded", [
    ("&U,BTFw-", "台北"), ("&ZeVnLIqe-", "日本語"), ("&-", "&"),
    ("Flu&Awg-ge", "Flüge"), ("INBOX/Fl&APw-ge", "INBOX/Flüge"),
])
def test_modified_utf7_vectors(encoded, decoded):
    assert m._decode_imap_utf7(encoded) == decoded


# ── quoting ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,quoted", [
    ("INBOX", '"INBOX"'), ("WIP - In Arbeit", '"WIP - In Arbeit"'),
    ('Quo"te', '"Quo\\"te"'), ("back\\slash", '"back\\\\slash"'),
])
def test_quote_mailbox(raw, quoted):
    assert m._quote_mailbox(raw) == quoted


@pytest.mark.parametrize("raw", ["a\rb", "a\nb", "a\x00b", "Flüge"])
def test_quote_mailbox_refuses_unrepresentable_names(raw):
    with pytest.raises(ValueError):
        m._quote_mailbox(raw)


# ── matching ──────────────────────────────────────────────────────────────────

FOLDERS = [
    folder("INBOX"), folder("Archive", ("\\Archive",)), folder("WIP - In Arbeit"),
    folder("INBOX/Flu&Awg-ge"), folder("Parent", ("\\Noselect",)), folder("Parent/Child"),
]


def error_of(value, folders=FOLDERS, param="folder"):
    with pytest.raises(m.FolderError) as info:
        m._match_folder(folders, value, param)
    return info.value.payload


def test_nfc_input_matches_an_nfd_server_name():
    assert m._match_folder(FOLDERS, "INBOX/" + NFC_FLUEGE, "folder")["raw_name"] == "INBOX/Flu&Awg-ge"


def test_raw_name_matches_directly():
    assert m._match_folder(FOLDERS, "INBOX/Flu&Awg-ge", "folder")["raw_name"] == "INBOX/Flu&Awg-ge"


def test_raw_name_takes_precedence_over_a_display_name():
    folders = [folder("Foo&AOQ-"), folder("Foo&-AOQ-")]
    assert folders[1]["name"] == "Foo&AOQ-"
    assert m._match_folder(folders, "Foo&AOQ-", "folder")["raw_name"] == "Foo&AOQ-"


def test_nfc_and_nfd_twins_are_ambiguous():
    folders = [folder("INBOX/Flu&Awg-ge"), folder("INBOX/Fl&APw-ge")]
    payload = error_of("INBOX/" + NFC_FLUEGE, folders)
    assert payload["problem"] == "folder_ambiguous"
    assert {c["raw_name"] for c in payload["candidates"]} == {"INBOX/Flu&Awg-ge", "INBOX/Fl&APw-ge"}
    assert m._match_folder(folders, "INBOX/Fl&APw-ge", "folder")["raw_name"] == "INBOX/Fl&APw-ge"


def test_display_names_are_case_sensitive():
    assert error_of("archive")["problem"] == "folder_not_found"


def test_unknown_folder_suggests_the_last_level_first():
    payload = error_of(NFC_FLUEGE)
    assert payload["problem"] == "folder_not_found"
    assert payload["did_you_mean"][0] == "INBOX/" + NFC_FLUEGE
    assert payload["param"] == "folder"


def test_manually_quoted_input_suggests_the_real_name():
    assert error_of('"WIP - In Arbeit"')["did_you_mean"] == ["WIP - In Arbeit"]


def test_suggestions_are_capped_at_three():
    many = [folder(f"Projekt {n}") for n in range(8)]
    assert len(error_of("Projekt", many)["did_you_mean"]) <= 3


def test_noselect_folders_are_rejected():
    assert error_of("Parent")["problem"] == "folder_not_selectable"


def test_nonexistent_folders_are_rejected_and_never_suggested():
    folders = [folder("Gone", ("\\NonExistent",)), folder("Gone2")]
    assert error_of("Gone", folders)["problem"] == "folder_not_selectable"
    assert error_of("Gon", folders)["did_you_mean"] == ["Gone2"]


def test_error_does_not_repeat_the_input():
    assert "SECRET-NAME" not in error_of("SECRET-NAME")["error"]


def test_has_flag_compares_whole_attributes_case_insensitively():
    assert m._has_flag(folder("X", ("\\SENT",)), "\\Sent")
    assert not m._has_flag(folder("X", ("\\Sentinel",)), "\\Sent")


# ── resolution against a connection ───────────────────────────────────────────

@pytest.fixture
def fake(monkeypatch):
    fake = FakeIMAP()
    fake.add_folder("Archive", ("\\Archive",))
    fake.add_folder("WIP - In Arbeit")
    fake.add_folder("INBOX/Flu&Awg-ge")
    fake.add_folder("Sent Items", ("\\Sent",))
    fake.add_folder("Parent", ("\\Noselect",))
    monkeypatch.setattr(m, "_imap_connect", lambda acct: fake)
    return fake


@pytest.mark.parametrize("spelling", ["INBOX", "inbox", "Inbox"])
def test_inbox_needs_no_list_command(fake, spelling):
    call("list_emails", {"folder": spelling, "account": "alpha"})
    assert ("list",) not in fake.commands
    assert fake.selected == "INBOX"


def test_select_receives_the_quoted_raw_name(fake):
    result = call_json("list_emails", {"folder": "WIP - In Arbeit", "account": "alpha"})
    assert result["folder"] == "WIP - In Arbeit"
    assert ("select", '"WIP - In Arbeit"', True) in fake.commands


def test_nfc_name_reaches_the_nfd_folder(fake):
    fake.add_message("INBOX/Flu&Awg-ge", make_message(), uid=7)
    result = call_json("list_emails", {"folder": "INBOX/" + NFC_FLUEGE, "account": "alpha"})
    assert [e["uid"] for e in result["emails"]] == ["7"]
    assert ("select", '"INBOX/Flu&Awg-ge"', True) in fake.commands


def test_unknown_folder_returns_the_error_object_without_retries(fake, monkeypatch):
    connects = []
    monkeypatch.setattr(m, "_imap_connect", lambda acct: connects.append(1) or fake)
    result = call_json("search", {"query": "ALL", "folder": "Nope", "account": "alpha"})
    assert result["problem"] == "folder_not_found"
    assert len(connects) == 1


def test_noselect_folder_is_not_selected(fake):
    result = call_json("list_emails", {"folder": "Parent", "account": "alpha"})
    assert result["problem"] == "folder_not_selectable"
    assert not [c for c in fake.commands if c[0] == "select"]


def test_no_folder_is_ever_created_renamed_or_deleted(fake):
    for action, params in [("list_emails", {"folder": "Nope"}), ("list_folders", {}),
                           ("move", {"uid": "1", "destination_folder": "Nope"}),
                           ("list_emails", {"folder": "Archive"})]:
        call(action, {**params, "account": "alpha"})
    assert fake.mutating() == []
    assert not [c for c in fake.commands if c[0] in ("create", "rename", "delete")]


@pytest.mark.parametrize("action,params,readonly", [
    ("read", {"uid": "1"}, True),
    ("mark", {"uid": "1", "action": "read"}, False),
    ("reply", {"uid": "1", "body": "x"}, True),
    ("reply_all", {"uid": "1", "body": "x"}, True),
    ("forward", {"uid": "1", "to": "d@example.test"}, True),
    ("save_attachment", {"uid": "1", "attachment_index": 0, "save_path": "/nonexistent-dir/x"}, True),
    ("get_attachment", {"uid": "1", "attachment_index": 0}, True),
])
def test_every_folder_taking_action_resolves_and_quotes(fake, monkeypatch, action, params, readonly):
    monkeypatch.setattr(m, "_smtp_send", SmtpRecorder())
    fake.add_message("WIP - In Arbeit", make_message(), uid=1)
    call(action, {**params, "folder": "WIP - In Arbeit", "account": "alpha"})
    assert ("select", '"WIP - In Arbeit"', readonly) in fake.commands


def test_connection_failures_are_retried_but_folder_errors_are_not(fake, monkeypatch):
    attempts = []

    def flaky(acct):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("down")
        return fake

    monkeypatch.setattr(m, "_imap_connect", flaky)
    assert json.loads(call("list_emails", {"account": "alpha"}))["count"] == 0
    assert len(attempts) == 3


def test_a_non_ascii_folder_in_the_list_does_not_break_other_folders(fake):
    fake.list_override = ['() "/" "Flüge"'.encode("utf-8"), b'() "/" "WIP - In Arbeit"', b'() "/" INBOX']
    assert call_json("list_emails", {"folder": "WIP - In Arbeit", "account": "alpha"})["folder"] == "WIP - In Arbeit"


def test_list_folders_shows_nfc_names_and_the_new_hint(fake):
    result = call_json("list_folders", {"account": "alpha"})
    names = {f["raw_name"]: f["name"] for f in result["folders"]}
    assert names["INBOX/Flu&Awg-ge"] == "INBOX/" + NFC_FLUEGE
    assert result["hint"] == "Use 'name' or 'raw_name' in folder parameters."
    assert next(f for f in result["folders"] if f["raw_name"] == "Sent Items")["type"] == "sent"


# ── move ──────────────────────────────────────────────────────────────────────

def seed_inbox(fake):
    fake.add_message("INBOX", make_message(), uid=2)


def test_move_resolves_destination_with_spaces(fake):
    seed_inbox(fake)
    result = call_json("move", {"uid": "2", "destination_folder": "WIP - In Arbeit", "account": "alpha"})
    assert result["to_folder"] == "WIP - In Arbeit"
    assert ("uid", "MOVE", "2", '"WIP - In Arbeit"') in fake.commands
    assert fake.uids_of("INBOX") == []


def test_move_to_an_unknown_destination_changes_nothing(fake):
    seed_inbox(fake)
    result = call_json("move", {"uid": "2", "destination_folder": "Nope", "account": "alpha"})
    assert result["problem"] == "folder_not_found" and result["param"] == "destination_folder"
    assert fake.mutating() == []


@pytest.mark.parametrize("destination", ["INBOX", "inbox"])
def test_move_to_the_same_folder_changes_nothing(fake, destination):
    seed_inbox(fake)
    result = call_json("move", {"uid": "2", "destination_folder": destination, "account": "alpha"})
    assert result["problem"] == "same_folder"
    assert fake.mutating() == []


def test_move_to_a_noselect_folder_changes_nothing(fake):
    seed_inbox(fake)
    result = call_json("move", {"uid": "2", "destination_folder": "Parent", "account": "alpha"})
    assert result["problem"] == "folder_not_selectable"
    assert fake.mutating() == []


def test_move_with_unknown_source_changes_nothing(fake):
    result = call_json("move", {"uid": "2", "source_folder": "Nope", "destination_folder": "Archive", "account": "alpha"})
    assert result["param"] == "source_folder"
    assert fake.mutating() == []


# ── sent folder and send ──────────────────────────────────────────────────────

def test_sent_copy_is_appended_with_a_quoted_folder_name(fake, monkeypatch):
    smtp = SmtpRecorder()
    monkeypatch.setattr(m, "_smtp_send", smtp)
    result = call_json("send", {"to": "dave@example.test", "subject": "Hi", "body": "Text", "account": "alpha"})
    assert result["status"] == "sent" and "warning" not in result
    assert ("append", '"Sent Items"', "\\Seen") in fake.commands
    assert len(fake.folders["Sent Items"].messages) == 1


def test_send_with_an_unknown_reply_to_uid_sends_nothing(fake, monkeypatch):
    smtp = SmtpRecorder()
    monkeypatch.setattr(m, "_smtp_send", smtp)
    result = call("send", {"to": "dave@example.test", "subject": "Hi", "body": "Text",
                           "reply_to_uid": "999", "account": "alpha"})
    assert result.startswith("Error:") and "999" in result
    assert smtp.sent == []


def test_send_with_an_unknown_reply_to_folder_sends_nothing(fake, monkeypatch):
    smtp = SmtpRecorder()
    monkeypatch.setattr(m, "_smtp_send", smtp)
    result = call_json("send", {"to": "dave@example.test", "subject": "Hi", "body": "Text",
                                "reply_to_uid": "1", "reply_to_folder": "Nope", "account": "alpha"})
    assert result["problem"] == "folder_not_found" and result["param"] == "reply_to_folder"
    assert smtp.sent == []


def test_send_threads_onto_a_message_in_a_named_folder(fake, monkeypatch):
    smtp = SmtpRecorder()
    monkeypatch.setattr(m, "_smtp_send", smtp)
    fake.add_message("WIP - In Arbeit", make_message(message_id="<thread-1@example.test>"), uid=5)
    call("send", {"to": "dave@example.test", "subject": "Hi", "body": "Text", "reply_to_uid": "5",
                  "reply_to_folder": "WIP - In Arbeit", "account": "alpha"})
    assert "In-Reply-To: <thread-1@example.test>" in smtp.sent[0]["mime"]
