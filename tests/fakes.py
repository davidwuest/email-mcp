"""In-memory stand-ins for the IMAP connection and the SMTP transport.

FakeIMAP replaces the object returned by simple_email_mcp._imap_connect, i.e. a
logged-in connection. It mimics the parts of imaplib's behaviour that matter for
the server code: argument types, mailbox quoting, BAD as an exception, NO as a
status, and it records every command.
"""
import imaplib
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Optional

_ATOM_SPECIALS = re.compile(r'[\s(){%*"\\\]\x00-\x1f]')


def make_message(subject="Hello", sender="Sender <sender@example.test>", to="alpha@example.test",
                 cc=None, message_id="<orig-1@example.test>", body="Original body",
                 reply_to=None, date="Mon, 05 Oct 2026 10:00:00 +0000") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg.as_bytes()


@dataclass
class Failure:
    status: str = "NO"
    apply_first: int = 0
    bad: bool = False
    times: Optional[int] = None  # None: fail every time; n: fail the next n calls only
    skip: int = 0  # let this many calls pass before failing


@dataclass
class Folder:
    flags: tuple = ()
    delimiter: str = "/"
    messages: dict = field(default_factory=dict)
    next_uid: int = 1


class FakeIMAP:
    instances: list = []

    def __init__(self, capabilities=("IMAP4REV1", "UIDPLUS", "MOVE", "SPECIAL-USE")):
        FakeIMAP.instances.append(self)
        self.server_capabilities = {c.upper() for c in capabilities}
        self.capabilities = ("IMAP4REV1",)  # like imaplib 3.12: the stale pre-login list
        self.capability_data = None
        self.folders: dict[str, Folder] = {}
        self.list_override = None
        self.selected = None
        self.readonly = False
        self.commands: list[tuple] = []
        self.failures: dict[str, Failure] = {}
        self.bare_expunges = 0
        self.add_folder("INBOX")

    # ── test setup ──────────────────────────────────────────────────────────
    def add_folder(self, raw_name, flags=(), delimiter="/"):
        self.folders[raw_name] = Folder(flags=tuple(flags), delimiter=delimiter)

    def add_message(self, raw_folder, raw=None, flags=(), uid=None):
        folder = self.folders[raw_folder]
        if uid is None:
            uid = folder.next_uid
        folder.next_uid = max(folder.next_uid, uid + 1)
        folder.messages[uid] = {"flags": set(flags), "raw": raw if raw is not None else make_message()}
        return uid

    def fail_on(self, command, status="NO", apply_first=0, bad=False, times=None, skip=0):
        self.failures[command.upper()] = Failure(status, apply_first, bad, times, skip)

    def flags_of(self, raw_folder, uid):
        return set(self.folders[raw_folder].messages[uid]["flags"])

    def uids_of(self, raw_folder):
        return sorted(self.folders[raw_folder].messages)

    def commands_named(self, *names):
        wanted = {n.upper() for n in names}
        return [c for c in self.commands if c[0] == "uid" and c[1] in wanted]

    def mutating(self):
        return [c for c in self.commands
                if c[0] in ("expunge", "append")
                or (c[0] == "uid" and c[1] in {"MOVE", "COPY", "STORE", "EXPUNGE"})]

    # ── helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def _check_args(args):
        for arg in args:
            if arg is not None and not isinstance(arg, (str, bytes)):
                raise TypeError(f"can't concat {type(arg).__name__} to bytes")
            if isinstance(arg, str):
                arg.encode("ascii")  # imaplib 3.12 sends str arguments as ASCII

    def _lookup(self, argument, command):
        """Resolve a mailbox argument the way a server parses it; returns the raw name or None."""
        if isinstance(argument, bytes):
            argument = argument.decode()
        if len(argument) >= 2 and argument.startswith('"') and argument.endswith('"'):
            name = re.sub(r'\\(["\\])', r"\1", argument[1:-1])
        else:
            if not argument or _ATOM_SPECIALS.search(argument):
                raise imaplib.IMAP4.error(f"{command} command error: BAD [b'Error in IMAP command: Invalid arguments']")
            name = argument
        if name.upper() == "INBOX":
            return "INBOX"
        return name if name in self.folders else None

    @staticmethod
    def _uid_set(text):
        if isinstance(text, bytes):
            text = text.decode()
        uids = []
        for part in text.split(","):
            if not part.isdigit():
                raise AssertionError(f"unexpected UID set {text!r}")
            uids.append(int(part))
        return uids

    def _selected_folder(self):
        assert self.selected is not None, "no folder selected"
        return self.folders[self.selected]

    def _failure(self, command, uids):
        failure = self.failures.get(command)
        if failure is None or failure.times == 0:
            return None, uids
        if failure.skip > 0:
            failure.skip -= 1
            return None, uids
        if failure.times is not None:
            failure.times -= 1
        return failure, uids[:failure.apply_first]

    def _raise_or_status(self, failure, command):
        if failure.bad:
            raise imaplib.IMAP4.error(f"{command} command error: BAD [b'injected']")
        return failure.status, [b"injected failure"]

    # ── imaplib surface ─────────────────────────────────────────────────────
    def capability(self):
        self.commands.append(("capability",))
        if self.capability_data is not None:
            return "OK", list(self.capability_data)
        return "OK", [" ".join(sorted(self.server_capabilities)).encode()]

    def list(self, directory='""', pattern="*"):
        self.commands.append(("list",))
        if self.list_override is not None:
            return "OK", list(self.list_override)
        lines = []
        for raw_name, folder in self.folders.items():
            flags = " ".join(folder.flags) if folder.flags else "\\HasNoChildren"
            name = raw_name if raw_name == "INBOX" else '"' + raw_name.replace("\\", "\\\\").replace('"', '\\"') + '"'
            lines.append(f'({flags}) "{folder.delimiter}" {name}'.encode())
        return "OK", lines

    def select(self, mailbox="INBOX", readonly=False):
        self.commands.append(("select", mailbox, readonly))
        self._check_args([mailbox])
        raw_name = self._lookup(mailbox, "SELECT")
        if raw_name is None:
            return "NO", [b"[NONEXISTENT] Mailbox doesn't exist"]
        if any(flag.lower() in ("\\noselect", "\\nonexistent") for flag in self.folders[raw_name].flags):
            return "NO", [b"Mailbox is not selectable"]
        self.selected = raw_name
        self.readonly = readonly
        return "OK", [str(len(self.folders[raw_name].messages)).encode()]

    def append(self, mailbox, flags, date_time, message):
        self.commands.append(("append", mailbox, flags))
        self._check_args([mailbox])
        raw_name = self._lookup(mailbox, "APPEND")
        if raw_name is None:
            return "NO", [b"[TRYCREATE] Mailbox doesn't exist"]
        flag_set = set(re.findall(r"\\\w+", flags or ""))
        uid = self.add_message(raw_name, bytes(message), flag_set)
        return "OK", [f"[APPENDUID 1 {uid}] Append completed".encode()]

    def expunge(self):
        self.commands.append(("expunge",))
        self.bare_expunges += 1
        folder = self._selected_folder()
        for uid in [u for u, m in folder.messages.items() if "\\Deleted" in m["flags"]]:
            del folder.messages[uid]
        return "OK", [None]

    def logout(self):
        self.commands.append(("logout",))
        return "BYE", [b"Logging out"]

    def create(self, *args, **kwargs):
        raise AssertionError("server code must never create folders")

    def rename(self, *args, **kwargs):
        raise AssertionError("server code must never rename folders")

    def delete(self, *args, **kwargs):
        raise AssertionError("server code must never delete folders")

    def uid(self, command, *args):
        self._check_args(args)
        cmd = command.upper()
        self.commands.append(("uid", cmd, *args))
        handler = getattr(self, f"_uid_{cmd.lower()}", None)
        assert handler is not None, f"FakeIMAP does not implement UID {cmd}"
        return handler(*args)

    def _uid_search(self, charset, *criteria):
        folder = self._selected_folder()
        failure, _ = self._failure("SEARCH", [])
        if failure:
            return self._raise_or_status(failure, "SEARCH")
        uids = sorted(folder.messages)
        if criteria and criteria[0] == "UID":
            wanted = set(self._uid_set(criteria[1]))
            uids = [u for u in uids if u in wanted]
        return "OK", [" ".join(map(str, uids)).encode()]

    def _uid_fetch(self, uid_set, items):
        folder = self._selected_folder()
        order = sorted(folder.messages)
        data = []
        for uid in self._uid_set(uid_set):
            if uid not in folder.messages:
                continue
            seq = order.index(uid) + 1
            message = folder.messages[uid]
            if "FLAGS" in items and "RFC822" not in items and "BODY" not in items:
                flags = " ".join(sorted(message["flags"]))
                data.append(f"{seq} (UID {uid} FLAGS ({flags}))".encode())
                continue
            raw = message["raw"]
            if "HEADER" in items:
                raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
            data.append((f"{seq} (UID {uid} RFC822 {{{len(raw)}}}".encode(), raw))
            data.append(b")")
        return "OK", data or [None]

    def _uid_store(self, uid_set, op, flags):
        if self.readonly:
            return "NO", [b"[READ-ONLY] Mailbox is read-only"]
        folder = self._selected_folder()
        uids = self._uid_set(uid_set)
        failure, uids_to_apply = self._failure("STORE", uids)
        flag_set = set(re.findall(r"\\\w+", flags))
        for uid in uids_to_apply:
            if uid in folder.messages:
                if op.startswith("+"):
                    folder.messages[uid]["flags"] |= flag_set
                elif op.startswith("-"):
                    folder.messages[uid]["flags"] -= flag_set
                else:
                    folder.messages[uid]["flags"] = set(flag_set)
        if failure:
            return self._raise_or_status(failure, "STORE")
        return "OK", [None]

    def _uid_copy(self, uid_set, mailbox):
        folder = self._selected_folder()
        uids = self._uid_set(uid_set)
        destination = self._lookup(mailbox, "COPY")
        if destination is None:
            return "NO", [b"[TRYCREATE] Mailbox doesn't exist"]
        failure, _ = self._failure("COPY", uids)
        if failure:
            return self._raise_or_status(failure, "COPY")
        for uid in uids:
            if uid in folder.messages:
                message = folder.messages[uid]
                self.add_message(destination, message["raw"], message["flags"] - {"\\Recent"})
        return "OK", [b"[COPYUID] Copy completed"]

    def _uid_move(self, uid_set, mailbox):
        if not self.server_capabilities & {"MOVE", "IMAP4REV2"}:
            raise imaplib.IMAP4.error("UID command error: BAD [b'Unknown command']")
        if self.readonly:
            return "NO", [b"[READ-ONLY] Mailbox is read-only"]
        folder = self._selected_folder()
        uids = self._uid_set(uid_set)
        destination = self._lookup(mailbox, "MOVE")
        if destination is None:
            return "NO", [b"[TRYCREATE] Mailbox doesn't exist"]
        failure, uids_to_apply = self._failure("MOVE", uids)
        for uid in uids_to_apply:
            if uid in folder.messages:
                message = folder.messages.pop(uid)
                self.add_message(destination, message["raw"], message["flags"] - {"\\Recent"})
        if failure:
            return self._raise_or_status(failure, "MOVE")
        return "OK", [b"[COPYUID] Move completed"]

    def _uid_expunge(self, uid_set):
        if not self.server_capabilities & {"UIDPLUS", "IMAP4REV2"}:
            raise imaplib.IMAP4.error("UID command error: BAD [b'Unknown command']")
        folder = self._selected_folder()
        uids = self._uid_set(uid_set)
        failure, uids_to_apply = self._failure("EXPUNGE", uids)
        for uid in uids_to_apply:
            if uid in folder.messages and "\\Deleted" in folder.messages[uid]["flags"]:
                del folder.messages[uid]
        if failure:
            return self._raise_or_status(failure, "EXPUNGE")
        return "OK", [None]


class SmtpRecorder:
    def __init__(self):
        self.sent = []

    def __call__(self, acct, sender, recipients, mime_str):
        self.sent.append({"sender": sender, "recipients": list(recipients), "mime": mime_str})
