"""Throwaway IMAP/SMTP servers for end-to-end tests. Nothing here touches a real mailbox."""
import imaplib
import json
import os
import socket
import ssl
import subprocess
import time
import uuid

import pytest

DOVECOT_24 = "dovecot/dovecot:2.4.4@sha256:723e3392fe16c6fad8ddc605ea767cc01b4bad9cd9f13eb1dbac15e79c89b2d4"
DOVECOT_23 = "dovecot/dovecot:2.3.21.1@sha256:547d28f9a893c93a1d2f6dc164880cd7bfa74009106dfa46b36a6a2ee636d921"
PASSWORDS = {"dovecot24": "testpass", "dovecot23": "pass"}


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)


def _docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        pytest.fail(f"docker {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def tls(tmp_path_factory):
    import trustme
    directory = tmp_path_factory.mktemp("tls")
    ca = trustme.CA()
    cert = ca.issue_cert("localhost", "127.0.0.1")
    paths = {"ca": directory / "ca.pem", "cert": directory / "cert.pem", "key": directory / "key.pem",
             "listen": directory / "listen.conf"}
    paths["listen"].write_text("listen = *\n")  # the sandbox may lack IPv6; Dovecot 2.4 binds :: by default
    ca.cert_pem.write_to_path(str(paths["ca"]))
    cert.cert_chain_pems[0].write_to_path(str(paths["cert"]))
    cert.private_key_pem.write_to_path(str(paths["key"]))
    for path in paths.values():
        path.chmod(0o644)
    directory.chmod(0o755)
    previous = os.environ.get("SSL_CERT_FILE")
    os.environ["SSL_CERT_FILE"] = str(paths["ca"])
    yield paths
    if previous is None:
        os.environ.pop("SSL_CERT_FILE", None)
    else:
        os.environ["SSL_CERT_FILE"] = previous


def _wait_for_login(port, user, password, timeout):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            conn = imaplib.IMAP4_SSL("127.0.0.1", port, ssl_context=ssl.create_default_context())
            conn.login(user, password)
            conn.logout()
            return
        except Exception as exc:
            last = exc
            time.sleep(1)
    pytest.fail(f"Dovecot did not become ready within {timeout}s: {last}")


def _start_dovecot(kind, tls):
    name = f"mhp-{kind}-{uuid.uuid4().hex[:8]}"
    label = ["--label", "email-mcp-test=1"]
    if kind == "dovecot24":
        _docker("run", "-d", "--rm", *label, "--name", name, "-e", "USER_PASSWORD={PLAIN}testpass",
                "-p", "127.0.0.1::31993",
                "-v", f"{tls['cert']}:/etc/dovecot/ssl/tls.crt:ro",
                "-v", f"{tls['key']}:/etc/dovecot/ssl/tls.key:ro",
                "-v", f"{tls['listen']}:/etc/dovecot/conf.d/zz-listen.conf:ro",
                "--tmpfs", "/srv/vmail:uid=1000,gid=1000", DOVECOT_24)
        internal, timeout = "31993/tcp", 60
    else:
        _docker("run", "-d", "--rm", *label, "--platform", "linux/amd64", "--name", name,
                "-p", "127.0.0.1::993",
                "-v", f"{tls['cert']}:/etc/dovecot/cert.pem:ro",
                "-v", f"{tls['key']}:/etc/dovecot/key.pem:ro", DOVECOT_23)
        internal, timeout = "993/tcp", 120
    try:
        port = int(_docker("port", name, internal).splitlines()[0].rsplit(":", 1)[1])
        _wait_for_login(port, "probe@example.test", PASSWORDS[kind], timeout)
    except BaseException:
        _docker("rm", "-f", name, check=False)
        raise
    return name, port


@pytest.fixture(scope="session")
def dovecot24(tls):
    name, port = _start_dovecot("dovecot24", tls)
    yield {"kind": "dovecot24", "port": port}
    _docker("rm", "-f", name, check=False)


@pytest.fixture(scope="session")
def dovecot23(tls):
    name, port = _start_dovecot("dovecot23", tls)
    yield {"kind": "dovecot23", "port": port}
    _docker("rm", "-f", name, check=False)


@pytest.fixture(params=["dovecot24", "dovecot23"])
def imap_server(request):
    return request.getfixturevalue(request.param)


@pytest.fixture(scope="session")
def smtp_sink(tls):
    from aiosmtpd.controller import Controller

    class Recorder:
        def __init__(self):
            self.messages = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append({"mail_from": envelope.mail_from, "rcpt_tos": list(envelope.rcpt_tos),
                                  "content": envelope.content})
            return "250 OK"

    def authenticator(server, session, envelope, mechanism, auth_data):
        from aiosmtpd.smtp import AuthResult
        return AuthResult(success=True)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tls["cert"]), str(tls["key"]))
    recorder = Recorder()
    port = _free_port()
    controller = Controller(recorder, hostname="127.0.0.1", port=port, ssl_context=context,
                            authenticator=authenticator, auth_require_tls=False,
                            server_hostname="localhost")
    controller.start()
    recorder.port = port
    yield recorder
    controller.stop()


class Admin:
    """Raw imaplib access for test setup. Only test code creates folders."""

    def __init__(self, port, user, password):
        self.conn = imaplib.IMAP4_SSL("127.0.0.1", port, ssl_context=ssl.create_default_context())
        self.conn.login(user, password)

    def create(self, raw_name):
        status, data = self.conn.create(f'"{raw_name}"')
        assert status == "OK", data

    def append(self, folder, message, flags=""):
        status, data = self.conn.append(f'"{folder}"', flags, None, message)
        assert status == "OK", data

    def _selected(self, folder):
        status, data = self.conn.select(f'"{folder}"')
        assert status == "OK", data

    def uids(self, folder):
        self._selected(folder)
        status, data = self.conn.uid("search", None, "ALL")
        return [int(u) for u in data[0].split()]

    def flags(self, folder, uid):
        self._selected(folder)
        status, data = self.conn.uid("fetch", str(uid), "(FLAGS)")
        text = data[0].decode() if isinstance(data[0], bytes) else data[0][0].decode()
        return set(part for part in text.split("FLAGS (")[1].split(")")[0].split())

    def list_names(self):
        status, data = self.conn.list()
        return [line for line in data if isinstance(line, bytes)]

    def close(self):
        try:
            self.conn.logout()
        except Exception:
            pass


@pytest.fixture
def mailbox(imap_server, smtp_sink, tmp_path, monkeypatch):
    """A fresh mailbox on the chosen server, wired into the server code via ACCOUNTS_FILE."""
    user = f"u{uuid.uuid4().hex[:8]}@example.test"
    password = PASSWORDS[imap_server["kind"]]
    accounts = {"accounts": [{
        "name": "test", "address": user, "password": password, "send_as": "team@example.test",
        "imap_host": "127.0.0.1", "imap_port": imap_server["port"],
        "smtp_host": "127.0.0.1", "smtp_port": smtp_sink.port, "smtp_security": "ssl"}]}
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(accounts))
    for var in ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "SEND_CODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ACCOUNTS_FILE", str(path))
    smtp_sink.messages.clear()
    admin = Admin(imap_server["port"], user, password)
    admin.user = user
    admin.kind = imap_server["kind"]
    yield admin
    admin.close()
