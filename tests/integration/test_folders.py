import unicodedata

import pytest

from helpers import call_json
from fakes import make_message

NFC_FLUEGE = unicodedata.normalize("NFC", "Flüge")


def crlf(message):
    return message.replace(b"\n", b"\r\n")


def test_nfd_folder_is_found_by_nfc_name_and_by_raw_name(mailbox):
    if mailbox.kind != "dovecot23":
        pytest.skip("only Dovecot 2.3 keeps NFD folder names like the production server")
    mailbox.create("INBOX/Flu&Awg-ge")
    for subject in ("a", "b"):
        mailbox.append("INBOX/Flu&Awg-ge", crlf(make_message(subject=subject)))
    by_nfc = call_json("list_emails", {"folder": "INBOX/" + NFC_FLUEGE, "account": "test"})
    by_raw = call_json("list_emails", {"folder": "INBOX/Flu&Awg-ge", "account": "test"})
    assert [e["uid"] for e in by_nfc["emails"]] == [e["uid"] for e in by_raw["emails"]] == ["2", "1"]
    found = call_json("search", {"query": "ALL", "folder": "INBOX/" + NFC_FLUEGE, "account": "test"})
    assert found["count"] == 2


def test_nfc_and_nfd_twins_are_reported_as_ambiguous(mailbox):
    if mailbox.kind != "dovecot23":
        pytest.skip("only Dovecot 2.3 keeps NFC and NFD names apart")
    mailbox.create("INBOX/Flu&Awg-ge")
    mailbox.create("INBOX/Fl&APw-ge")
    result = call_json("list_emails", {"folder": "INBOX/" + NFC_FLUEGE, "account": "test"})
    assert result["problem"] == "folder_ambiguous"
    assert {c["raw_name"] for c in result["candidates"]} == {"INBOX/Flu&Awg-ge", "INBOX/Fl&APw-ge"}


def test_folder_with_spaces_works_without_manual_quoting(mailbox):
    mailbox.create("WIP - In Arbeit")
    mailbox.append("INBOX", crlf(make_message(subject="job")))
    listed = call_json("list_emails", {"folder": "WIP - In Arbeit", "account": "test", "limit": 5})
    assert listed["count"] == 0
    moved = call_json("move", {"uid": "1", "destination_folder": "WIP - In Arbeit", "account": "test"})
    assert moved["status"] == "moved" and moved["to_folder"] == "WIP - In Arbeit"
    assert mailbox.uids("WIP - In Arbeit") == [1]
    assert mailbox.uids("INBOX") == []


def test_manually_quoted_name_is_rejected_with_the_right_suggestion(mailbox):
    mailbox.create("WIP - In Arbeit")
    result = call_json("list_emails", {"folder": '"WIP - In Arbeit"', "account": "test"})
    assert result["problem"] == "folder_not_found"
    assert result["did_you_mean"] == ["WIP - In Arbeit"]


def test_unknown_folder_creates_nothing(mailbox):
    before = mailbox.list_names()
    result = call_json("move", {"uid": "1", "destination_folder": "Does Not Exist", "account": "test"})
    assert result["problem"] == "folder_not_found"
    assert mailbox.list_names() == before


def test_implicit_parent_folder_is_not_selectable(mailbox):
    mailbox.create("Parent/Child")
    result = call_json("list_emails", {"folder": "Parent", "account": "test"})
    assert result["problem"] == "folder_not_selectable"
    moved = call_json("move", {"uid": "1", "destination_folder": "Parent", "account": "test"})
    assert moved["problem"] == "folder_not_selectable"


def test_list_folders_reports_name_raw_name_and_hint(mailbox):
    mailbox.create("INBOX/Fl&APw-ge")
    result = call_json("list_folders", {"account": "test"})
    entry = next(f for f in result["folders"] if f["raw_name"] == "INBOX/Fl&APw-ge")
    assert entry["name"] == "INBOX/" + NFC_FLUEGE
    assert result["hint"] == "Use 'name' or 'raw_name' in folder parameters."
