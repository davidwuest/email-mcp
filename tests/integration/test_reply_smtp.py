import email

import pytest

from helpers import call_json
from fakes import make_message


@pytest.fixture
def sent_folder(mailbox):
    if mailbox.kind == "dovecot23":
        mailbox.create("Sent")
    return "Sent"


def test_reply_all_reaches_every_recipient_exactly_once(mailbox, smtp_sink, sent_folder):
    original = make_message(sender="Sender <sender@example.test>", to=f"{mailbox.user}, bob@example.test",
                            cc="carol@example.test", message_id="<orig-1@example.test>")
    mailbox.append("INBOX", original.replace(b"\n", b"\r\n"))
    result = call_json("reply_all", {
        "uid": "1", "body": "Thanks", "account": "test",
        "cc": "erin@example.test, carol@example.test", "bcc": "frank@example.test",
        "reply_to": "desk@example.test"})
    assert result["status"] == "sent" and "warning" not in result
    assert result["dropped"] == [{"address": "carol@example.test", "field": "cc", "reason": "duplicate"}]

    (delivered,) = smtp_sink.messages
    assert sorted(delivered["rcpt_tos"]) == sorted([
        "sender@example.test", "bob@example.test", "carol@example.test", "erin@example.test", "frank@example.test"])
    message = email.message_from_bytes(delivered["content"])
    assert message["Bcc"] is None and b"frank@example.test" not in delivered["content"].split(b"\n\n", 1)[0]
    assert message["Cc"] == "bob@example.test, carol@example.test, erin@example.test"
    assert message["Reply-To"] == "desk@example.test"
    assert message["In-Reply-To"] == "<orig-1@example.test>"
    assert len(mailbox.uids(sent_folder)) == 1


def test_reply_with_bcc_only_does_not_leak_the_bcc(mailbox, smtp_sink, sent_folder):
    mailbox.append("INBOX", make_message(sender="sender@example.test", to=mailbox.user).replace(b"\n", b"\r\n"))
    call_json("reply", {"uid": "1", "body": "x", "bcc": "frank@example.test", "account": "test"})
    (delivered,) = smtp_sink.messages
    assert sorted(delivered["rcpt_tos"]) == ["frank@example.test", "sender@example.test"]
    assert b"frank@example.test" not in delivered["content"]
