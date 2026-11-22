
from helpers import call_json


def test_list_folders_reaches_the_server(mailbox):
    result = call_json("list_folders", {"account": "test"})
    assert "INBOX" in [folder["raw_name"] for folder in result["folders"]]
