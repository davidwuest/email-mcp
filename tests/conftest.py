import json
import sys

import pytest

import simple_email_mcp


def pytest_configure(config):
    if sys.version_info[:2] != (3, 12):
        raise pytest.UsageError(
            f"Tests must run on Python 3.12 like the image (found {sys.version.split()[0]}): "
            "imaplib differs in quoting and capability handling between versions."
        )


def write_accounts(path, send_code=None):
    accounts = {
        "accounts": [
            {
                "name": "alpha", "address": "alpha@example.test", "password": "pw-alpha",
                "send_as": "team@example.test", "display_name": "Alpha Team",
                "imap_host": "imap.invalid", "imap_port": 993,
                "smtp_host": "smtp.invalid", "smtp_port": 465, "smtp_security": "ssl",
            },
            {
                "name": "beta", "address": "beta@example.test", "password": "pw-beta",
                "imap_host": "imap.invalid", "imap_port": 993,
                "smtp_host": "smtp.invalid", "smtp_port": 465, "smtp_security": "ssl",
            },
        ]
    }
    if send_code is not None:
        accounts["send_code"] = send_code
    path.write_text(json.dumps(accounts))
    return path


@pytest.fixture(autouse=True)
def dummy_accounts(request, tmp_path, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return
    for var in ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "SEND_CODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ACCOUNTS_FILE", str(write_accounts(tmp_path / "accounts.json")))


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return

    def blocked(*args, **kwargs):
        raise AssertionError("network access in unit test")

    monkeypatch.setattr(simple_email_mcp, "_imap_connect", blocked)
    monkeypatch.setattr(simple_email_mcp, "_smtp_send", blocked)

    class NoSleep:
        sleep = staticmethod(lambda seconds: None)
        time = staticmethod(simple_email_mcp.time.time)

    monkeypatch.setattr(simple_email_mcp, "time", NoSleep)


@pytest.fixture
def set_send_code(tmp_path, monkeypatch):
    def apply(code):
        monkeypatch.setenv("ACCOUNTS_FILE", str(write_accounts(tmp_path / "accounts.json", send_code=code)))
    return apply

