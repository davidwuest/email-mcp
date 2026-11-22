#!/usr/bin/env python3
"""
MCP Server for multi-account Email (IMAP/SMTP).

A provider-agnostic email MCP server that works with any IMAP/SMTP email provider.
Supports multiple accounts, attachments (file-based and base64 inline), calendar
invites, HTML emails, and optional send confirmation gates.

Configuration via accounts.json:
{
  "send_code": "OPTIONAL_CODE",
  "accounts": [
    {
      "name": "my-account",
      "address": "me@example.com",
      "password": "app-password",
      "provider": "purelymail"
    }
  ]
}
"""

import os, json, email, imaplib, smtplib, ssl, re, time, base64, mimetypes, unicodedata, difflib
from contextlib import contextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime, formatdate, make_msgid, getaddresses, formataddr
from dataclasses import dataclass
from typing import Optional, Dict, Any, NamedTuple, Callable
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("simple-email-mcp")

PROVIDER_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "purelymail": {"imap_host": "imap.purelymail.com", "imap_port": 993, "smtp_host": "smtp.purelymail.com", "smtp_port": 465, "smtp_security": "ssl"},
    "domainfactory": {"imap_host": "sslin.de", "imap_port": 993, "smtp_host": "sslout.de", "smtp_port": 465, "smtp_security": "ssl"},
    "gmail": {"imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_security": "ssl"},
    "outlook": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls"},
}

_accounts: Dict[str, Dict[str, Any]] = {}
_send_code: Optional[str] = None
_sent_folder_cache: Dict[str, str] = {}

def _int_config(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _load_accounts() -> None:
    global _accounts, _send_code, _sent_folder_cache
    _accounts = {}
    _send_code = None
    _sent_folder_cache = {}
    config_path = os.environ.get("ACCOUNTS_FILE", str(Path(__file__).parent / "accounts.json"))
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        code = raw.get("send_code", "")
        _send_code = code if code else None
        for acct in raw.get("accounts", []):
            if not isinstance(acct, dict) or not all(acct.get(field) for field in ("name", "address", "password")):
                continue
            name = acct["name"]
            provider = acct.get("provider", "")
            defaults = PROVIDER_DEFAULTS.get(provider, {})
            smtp_port = _int_config(acct.get("smtp_port", defaults.get("smtp_port", 465)), 465)
            smtp_security = acct.get("smtp_security", defaults.get("smtp_security", "starttls" if smtp_port == 587 else "ssl"))
            address = acct["address"]
            _accounts[name] = {
                "address": address, "password": acct["password"],
                "send_as": acct.get("send_as") or address,
                "display_name": acct.get("display_name") or acct.get("from_name") or "",
                "description": acct.get("description") or "",
                "imap_host": acct.get("imap_host", defaults.get("imap_host", "imap.example.com")),
                "imap_port": _int_config(acct.get("imap_port", defaults.get("imap_port", 993)), 993),
                "smtp_host": acct.get("smtp_host", defaults.get("smtp_host", "smtp.example.com")),
                "smtp_port": smtp_port,
                "smtp_security": smtp_security,
            }
        return
    _send_code = os.environ.get("SEND_CODE") or None
    addr = os.environ.get("EMAIL_ADDRESS", "")
    pwd = os.environ.get("EMAIL_PASSWORD", "")
    if addr and pwd:
        smtp_port = _int_config(os.environ.get("SMTP_PORT", "465"), 465)
        _accounts["default"] = {
            "address": addr, "password": pwd,
            "send_as": os.environ.get("SEND_AS", addr),
            "display_name": os.environ.get("EMAIL_DISPLAY_NAME", ""),
            "description": os.environ.get("EMAIL_DESCRIPTION", ""),
            "imap_host": os.environ.get("IMAP_HOST", "imap.example.com"),
            "imap_port": _int_config(os.environ.get("IMAP_PORT", "993"), 993),
            "smtp_host": os.environ.get("SMTP_HOST", "smtp.example.com"),
            "smtp_port": smtp_port,
            "smtp_security": os.environ.get("SMTP_SECURITY", "starttls" if smtp_port == 587 else "ssl"),
        }

_load_accounts()

def _refresh_runtime_config() -> None:
    _load_accounts()

def _check_confirmation_code(confirmation_code: Optional[str]) -> Optional[str]:
    _refresh_runtime_config()
    if _send_code:
        if not confirmation_code:
            return "BLOCKED: A confirmation code is required. Show the draft to the user and ask for their code."
        if confirmation_code.strip() != _send_code.strip():
            return "BLOCKED: Invalid confirmation code. The email was NOT sent."
    return None

def _looks_like_email(value: str) -> bool:
    _, addr = parseaddr(value or "")
    return bool(addr and "@" in addr and "." in addr.rsplit("@", 1)[-1])

def _coerce_port(value: Any, field: str, account_label: str, errors: list[str]) -> Optional[int]:
    try:
        port = int(value)
    except (TypeError, ValueError):
        errors.append(f"{account_label}: {field} must be an integer.")
        return None
    if not 1 <= port <= 65535:
        errors.append(f"{account_label}: {field} must be between 1 and 65535.")
        return None
    return port

def _validate_account_record(acct: Dict[str, Any], index: int, seen_names: set[str], errors: list[str], warnings: list[str]) -> Optional[dict]:
    label = f"accounts[{index}]"
    if not isinstance(acct, dict):
        errors.append(f"{label}: account must be an object.")
        return None

    name = str(acct.get("name", "")).strip()
    account_label = name or label
    if not name:
        errors.append(f"{label}: missing required field 'name'.")
    elif name.lower() in seen_names:
        errors.append(f"{account_label}: duplicate account name.")
    else:
        seen_names.add(name.lower())

    address = str(acct.get("address", "")).strip()
    if not address:
        errors.append(f"{account_label}: missing required field 'address'.")
    elif not _looks_like_email(address):
        errors.append(f"{account_label}: address does not look like an email address.")

    if not str(acct.get("password", "")).strip():
        errors.append(f"{account_label}: missing required field 'password'.")

    send_as = str(acct.get("send_as") or address).strip()
    if send_as and not _looks_like_email(send_as):
        errors.append(f"{account_label}: send_as does not look like an email address.")

    provider = str(acct.get("provider", "")).strip()
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    if provider and provider not in PROVIDER_DEFAULTS:
        warnings.append(f"{account_label}: unknown provider '{provider}', using explicit host/port values or placeholders.")

    imap_host = str(acct.get("imap_host", defaults.get("imap_host", "imap.example.com"))).strip()
    smtp_host = str(acct.get("smtp_host", defaults.get("smtp_host", "smtp.example.com"))).strip()
    if not imap_host:
        errors.append(f"{account_label}: imap_host is empty.")
    if not smtp_host:
        errors.append(f"{account_label}: smtp_host is empty.")
    if not provider and imap_host == "imap.example.com":
        warnings.append(f"{account_label}: no provider or imap_host configured; IMAP will use placeholder host.")
    if not provider and smtp_host == "smtp.example.com":
        warnings.append(f"{account_label}: no provider or smtp_host configured; SMTP will use placeholder host.")

    imap_port = _coerce_port(acct.get("imap_port", defaults.get("imap_port", 993)), "imap_port", account_label, errors)
    smtp_port = _coerce_port(acct.get("smtp_port", defaults.get("smtp_port", 465)), "smtp_port", account_label, errors)

    smtp_security = str(acct.get("smtp_security", defaults.get("smtp_security", "starttls" if smtp_port == 587 else "ssl"))).strip().lower()
    if smtp_security not in {"ssl", "starttls"}:
        errors.append(f"{account_label}: smtp_security must be 'ssl' or 'starttls'.")

    summary = {
        "name": name,
        "address": address,
        "send_as": send_as or address,
        "provider": provider or None,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_security": smtp_security,
        "password_configured": bool(str(acct.get("password", "")).strip()),
    }
    if acct.get("display_name") or acct.get("from_name"):
        summary["display_name"] = acct.get("display_name") or acct.get("from_name")
    if acct.get("description"):
        summary["description"] = acct.get("description")
    return summary

def _validate_config() -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    config_path = os.environ.get("ACCOUNTS_FILE", str(Path(__file__).parent / "accounts.json"))

    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            return {"ok": False, "source": "accounts_file", "config_path": config_path, "errors": [f"Invalid JSON: {e}"], "warnings": [], "accounts": []}
        except OSError as e:
            return {"ok": False, "source": "accounts_file", "config_path": config_path, "errors": [f"Could not read config file: {e}"], "warnings": [], "accounts": []}

        if not isinstance(raw, dict):
            return {"ok": False, "source": "accounts_file", "config_path": config_path, "errors": ["Config root must be an object."], "warnings": [], "accounts": []}

        raw_accounts = raw.get("accounts", [])
        if not isinstance(raw_accounts, list):
            errors.append("'accounts' must be a list.")
            raw_accounts = []
        if not raw_accounts:
            errors.append("No accounts configured.")

        seen_names: set[str] = set()
        accounts = []
        for index, acct in enumerate(raw_accounts):
            summary = _validate_account_record(acct, index, seen_names, errors, warnings)
            if summary:
                accounts.append(summary)

        return {
            "ok": not errors,
            "source": "accounts_file",
            "config_path": config_path,
            "send_code_configured": bool(raw.get("send_code")),
            "accounts": accounts,
            "errors": errors,
            "warnings": warnings,
        }

    addr = os.environ.get("EMAIL_ADDRESS", "")
    pwd = os.environ.get("EMAIL_PASSWORD", "")
    smtp_port = os.environ.get("SMTP_PORT", "465")
    env_account = {
        "name": "default",
        "address": addr,
        "password": pwd,
        "send_as": os.environ.get("SEND_AS", addr),
        "display_name": os.environ.get("EMAIL_DISPLAY_NAME", ""),
        "description": os.environ.get("EMAIL_DESCRIPTION", ""),
        "imap_host": os.environ.get("IMAP_HOST", "imap.example.com"),
        "imap_port": os.environ.get("IMAP_PORT", "993"),
        "smtp_host": os.environ.get("SMTP_HOST", "smtp.example.com"),
        "smtp_port": smtp_port,
        "smtp_security": os.environ.get("SMTP_SECURITY", "starttls" if smtp_port == "587" else "ssl"),
    }
    if not addr and not pwd:
        errors.append("No accounts.json found and EMAIL_ADDRESS/EMAIL_PASSWORD are not configured.")

    seen_names: set[str] = set()
    summary = _validate_account_record(env_account, 0, seen_names, errors, warnings)
    return {
        "ok": not errors,
        "source": "environment",
        "config_path": config_path,
        "send_code_configured": bool(os.environ.get("SEND_CODE")),
        "accounts": [summary] if summary else [],
        "errors": errors,
        "warnings": warnings,
    }

def _resolve_account(account: Optional[str]) -> Dict[str, Any]:
    _refresh_runtime_config()
    if not _accounts:
        raise RuntimeError("No email accounts configured.")
    if not account:
        if len(_accounts) == 1:
            return next(iter(_accounts.values()))
        available = ", ".join(f"'{n}' ({c['address']})" for n, c in _accounts.items())
        raise ValueError(f"Multiple accounts configured. You must specify which one: {available}")
    key = account.lower().strip()
    for name, cfg in _accounts.items():
        if key == name.lower() or key == cfg["address"].lower():
            return cfg
    partial_matches = []
    for name, cfg in _accounts.items():
        if key in name.lower() or key in cfg["address"].lower():
            partial_matches.append((name, cfg))
    if len(partial_matches) == 1:
        return partial_matches[0][1]
    if len(partial_matches) > 1:
        available = ", ".join(f"'{name}' ({cfg['address']})" for name, cfg in partial_matches)
        raise ValueError(f"Account '{account}' is ambiguous. Matches: {available}")
    available = ", ".join(f"'{n}'" for n in _accounts)
    raise ValueError(f"Account '{account}' not found. Available: {available}")

def _imap_connect(acct: Dict[str, Any]) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(acct["imap_host"], acct["imap_port"], ssl_context=ctx)
    conn.login(acct["address"], acct["password"])
    return conn

def _imap_capabilities(conn: imaplib.IMAP4_SSL) -> frozenset:
    # imaplib 3.12 keeps the capability list from before the login, so ask again.
    status, data = conn.capability()
    if status != "OK" or not data:
        raise RuntimeError("Could not query server capabilities.")
    last = data[-1]
    text = last.decode("ascii", errors="replace") if isinstance(last, bytes) else str(last)
    return frozenset(text.upper().split())

def _move_method(capabilities: frozenset) -> Optional[str]:
    if "MOVE" in capabilities or "IMAP4REV2" in capabilities:
        return "move"
    if "UIDPLUS" in capabilities:
        return "copy_expunge"
    return None

@contextmanager
def _imap_session(acct: Dict[str, Any]):
    last_err = None
    for attempt in range(3):
        try:
            conn = _imap_connect(acct)
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
    else:
        raise last_err
    try:
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass

def _decode_imap_utf7(s: str) -> str:
    result = []
    i = 0
    while i < len(s):
        if s[i] == '&':
            j = s.index('-', i + 1) if '-' in s[i + 1:] else len(s)
            if j == i + 1:
                result.append('&')
            else:
                encoded = s[i + 1:j].replace(',', '/')
                encoded += '=' * (4 - len(encoded) % 4) if len(encoded) % 4 else ''
                try:
                    result.append(base64.b64decode(encoded).decode('utf-16-be'))
                except Exception:
                    result.append(s[i:j + 1])
            i = j + 1
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)

class FolderError(Exception):
    def __init__(self, payload: dict):
        super().__init__(payload["error"])
        self.payload = payload

    def to_json(self) -> str:
        return json.dumps(self.payload, indent=2, ensure_ascii=False)

_NOT_SELECTABLE = ("\\noselect", "\\nonexistent")

def _read_quoted(text: str) -> tuple[str, str]:
    out = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
        elif ch == '"':
            return "".join(out), text[i + 1:]
        else:
            out.append(ch)
            i += 1
    raise ValueError("unterminated quoted string")

def _parse_list_line(head: bytes, literal: Optional[bytes]) -> Optional[dict]:
    text = head.decode("utf-8", errors="replace")
    m = re.match(r"\s*\(([^)]*)\)\s+", text)
    if not m:
        return None
    flags = tuple(m.group(1).split())
    rest = text[m.end():]
    if rest.startswith('"'):
        delimiter, rest = _read_quoted(rest)
    elif rest[:3].upper() == "NIL" and (len(rest) == 3 or rest[3] == " "):
        delimiter, rest = None, rest[3:]
    else:
        return None
    rest = rest.lstrip()
    if literal is not None:
        name = literal.decode("utf-8", errors="replace")
    elif rest.startswith('"'):
        name, _ = _read_quoted(rest)
    else:
        name = rest.split(" ", 1)[0]
    if not name:
        return None
    if not name.isascii():
        return None  # only possible with UTF8=ACCEPT, which is never enabled; don't let one odd entry break the rest
    return {"name": unicodedata.normalize("NFC", _decode_imap_utf7(name)), "raw_name": name, "delimiter": delimiter, "flags": flags}

def _parse_imap_list_response(raw_list: list) -> list[dict]:
    # imaplib hands out a literal as a tuple and the rest of that line as a separate element,
    # and it also collects unsolicited LIST responses from earlier commands, hence the dedup.
    folders: Dict[str, dict] = {}
    after_literal = False
    for item in raw_list:
        if isinstance(item, tuple):
            parsed = _parse_list_line(item[0], item[1])
            after_literal = True
        elif isinstance(item, bytes):
            if after_literal:
                after_literal = False
                if not item.strip() or item[:1] == b" ":
                    continue
            parsed = _parse_list_line(item, None)
        else:
            continue
        if parsed:
            folders[parsed["raw_name"]] = parsed
    return list(folders.values())

def _has_flag(folder: dict, flag: str) -> bool:
    return flag.lower() in (f.lower() for f in folder["flags"])

def _quote_mailbox(raw_name: str) -> str:
    try:
        raw_name.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("Mailbox name must be the ASCII raw_name reported by the server.")
    if any(ch in raw_name for ch in "\r\n\x00"):
        raise ValueError("Mailbox name contains a control character.")
    return '"' + raw_name.replace("\\", "\\\\").replace('"', '\\"') + '"'

def _folder_suggestions(folders: list[dict], value: str) -> list[str]:
    folders = [f for f in folders if not any(_has_flag(f, flag) for flag in _NOT_SELECTABLE)]
    key = unicodedata.normalize("NFC", value).casefold()
    suggestions: list[str] = []
    for f in folders:
        last = f["name"].split(f["delimiter"])[-1] if f["delimiter"] else f["name"]
        if last.casefold() == key and f["name"] not in suggestions:
            suggestions.append(f["name"])
    by_key = {f["name"].casefold(): f["name"] for f in folders}
    for close in difflib.get_close_matches(key, list(by_key), n=3, cutoff=0.6):
        if by_key[close] not in suggestions:
            suggestions.append(by_key[close])
    return suggestions[:3]

def _match_folder(folders: list[dict], value: str, param: str) -> dict:
    nfc = unicodedata.normalize("NFC", value)
    matches = [f for f in folders if f["raw_name"] == value]
    if not matches:
        matches = [f for f in folders if f["name"] == nfc]
    if len(matches) > 1:
        candidates = [{"name": f["name"], "raw_name": f["raw_name"]} for f in matches]
        raise FolderError({
            "error": f"Folder name for '{param}' matches several folders; pass the raw_name of one of them.",
            "param": param, "problem": "folder_ambiguous", "candidates": candidates})
    if not matches:
        suggestions = _folder_suggestions(folders, value)
        hint = " Did you mean: " + ", ".join(f"'{n}'" for n in suggestions) + "?" if suggestions else " Use list_folders to see the available folders."
        raise FolderError({"error": f"Unknown folder for '{param}'." + hint, "param": param,
                           "problem": "folder_not_found", "did_you_mean": suggestions})
    folder = matches[0]
    if any(_has_flag(folder, flag) for flag in _NOT_SELECTABLE):
        raise FolderError({"error": f"Folder '{folder['name']}' for '{param}' cannot hold messages.",
                           "param": param, "problem": "folder_not_selectable"})
    return folder

def _resolve_folders(conn: imaplib.IMAP4_SSL, wanted: Dict[str, str]) -> Dict[str, dict]:
    resolved: Dict[str, dict] = {}
    pending: Dict[str, str] = {}
    for param, value in wanted.items():
        if value.upper() == "INBOX":
            resolved[param] = {"name": "INBOX", "raw_name": "INBOX", "delimiter": None, "flags": ()}
        else:
            pending[param] = value
    if pending:
        status, data = conn.list()
        if status != "OK":
            raise RuntimeError("Could not retrieve folder list.")
        folders = _parse_imap_list_response(data)
        for param, value in pending.items():
            resolved[param] = _match_folder(folders, value, param)
    return {param: resolved[param] for param in wanted}

def _select(conn: imaplib.IMAP4_SSL, folder: dict, readonly: bool) -> None:
    status, data = conn.select(_quote_mailbox(folder["raw_name"]), readonly=readonly)
    if status != "OK":
        detail = ""
        if data and data[0]:
            detail = data[0].decode("utf-8", errors="replace") if isinstance(data[0], bytes) else str(data[0])
        raise RuntimeError(f"Could not select folder '{folder['name']}'" + (f": {detail}" if detail else ""))

def _open_folder(conn: imaplib.IMAP4_SSL, value: str, param: str = "folder", readonly: bool = True) -> dict:
    folder = _resolve_folders(conn, {param: value})[param]
    _select(conn, folder, readonly)
    return folder

def _decode_header_value(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            decoded.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(fragment)
    return " ".join(decoded)

def _is_attachment_part(part: email.message.Message) -> bool:
    if part.is_multipart():
        return False
    if "attachment" in str(part.get("Content-Disposition", "")):
        return True
    return part.get_filename() is not None

def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        text_part = html_part = None
        for part in msg.walk():
            if _is_attachment_part(part):
                continue
            ct = part.get_content_type()
            if ct == "text/plain" and text_part is None:
                text_part = part
            elif ct == "text/html" and html_part is None:
                html_part = part
        chosen = text_part or html_part
        if chosen:
            payload = chosen.get_payload(decode=True)
            charset = chosen.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace") if payload else ""
        return ""
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""

def _list_attachments(msg: email.message.Message) -> list[dict]:
    attachments = []
    if not msg.is_multipart():
        return attachments
    for idx, part in enumerate(msg.walk()):
        if not _is_attachment_part(part):
            continue
        disp = str(part.get("Content-Disposition", "")).lower()
        disposition = "attachment" if "attachment" in disp else ("inline" if "inline" in disp else "")
        filename = _decode_header_value(part.get_filename())
        size = len(part.get_payload(decode=True) or b"")
        attachments.append({"index": idx, "filename": filename or "(unnamed)", "size_bytes": size, "content_type": part.get_content_type(), "disposition": disposition})
    return attachments

def _msg_to_summary(msg: email.message.Message, uid: str) -> dict:
    date_str = msg.get("Date", "")
    try:
        date_formatted = parsedate_to_datetime(date_str).strftime("%Y-%m-%d %H:%M")
    except Exception:
        date_formatted = date_str
    # list_emails/search only fetch RFC822.HEADER, so parts can't be walked here;
    # fall back to the top-level Content-Type as a heuristic.
    has_attachments = msg.get_content_type() in ("multipart/mixed", "multipart/related")
    return {"uid": uid, "from": _decode_header_value(msg.get("From")), "to": _decode_header_value(msg.get("To")), "subject": _decode_header_value(msg.get("Subject")), "date": date_formatted, "has_attachments": has_attachments}

def _find_sent_folder(conn: imaplib.IMAP4_SSL, acct: Dict[str, Any]) -> str:
    cache_key = acct["address"]
    if cache_key in _sent_folder_cache:
        return _sent_folder_cache[cache_key]
    try:
        status, folder_list = conn.list()
        if status == "OK" and folder_list:
            parsed = _parse_imap_list_response(folder_list)
            for f in parsed:
                if _has_flag(f, "\\Sent"):
                    _sent_folder_cache[cache_key] = f["raw_name"]
                    return f["raw_name"]
            for f in parsed:
                if f["raw_name"].lower() in ("sent", "inbox.sent", "sent items", "sent messages"):
                    _sent_folder_cache[cache_key] = f["raw_name"]
                    return f["raw_name"]
    except Exception:
        pass
    return "Sent"

def _save_to_sent(acct: Dict[str, Any], mime_message: str) -> Optional[str]:
    """Returns a warning string on failure, None on success."""
    try:
        with _imap_session(acct) as conn:
            sent_folder = _find_sent_folder(conn, acct)
            date_time = imaplib.Time2Internaldate(time.time())
            status, response = conn.append(_quote_mailbox(sent_folder), "\\Seen", date_time, mime_message.encode("utf-8"))
            if status != "OK":
                return f"Email sent, but failed to save to Sent folder ({status})"
    except Exception as e:
        return f"Email sent, but failed to save to Sent folder ({e})"
    return None

def _smtp_send(acct: Dict[str, Any], sender: str, recipients: list[str], mime_str: str) -> None:
    ctx = ssl.create_default_context()
    if acct.get("smtp_security") == "starttls":
        with smtplib.SMTP(acct["smtp_host"], acct["smtp_port"]) as server:
            server.starttls(context=ctx)
            server.login(acct["address"], acct["password"])
            server.sendmail(sender, recipients, mime_str)
    else:
        with smtplib.SMTP_SSL(acct["smtp_host"], acct["smtp_port"], context=ctx) as server:
            server.login(acct["address"], acct["password"])
            server.sendmail(sender, recipients, mime_str)

def _quote_body(body: str, sender: str, date: str) -> str:
    quoted = "\n".join(f"> {line}" for line in body.splitlines())
    return f"On {date}, {sender} wrote:\n{quoted}"

def _parse_address_list(*fields: Optional[str]) -> list[str]:
    """Return deduplicated SMTP mailbox addresses from RFC-style fields.

    Outlook sometimes emits an invalid but common form such as
    ``Doe, Jane <jane@example.com>`` (an unquoted comma in the display name).
    ``parseaddr`` rejects the whole value, while ``getaddresses`` still finds
    the angle-bracket mailbox alongside a bogus display-name fragment.  Keep
    only syntactically plausible mailbox values so those fragments can never
    reach the SMTP envelope.
    """
    addresses = []
    seen = set()
    unfolded_fields = [re.sub(r"\r?\n[ \t]*", " ", field) for field in fields if field]
    for _, addr in getaddresses(unfolded_fields):
        if not _looks_like_email(addr):
            continue
        normalized = addr.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            addresses.append(addr.strip())
    return addresses

def _own_addresses(acct: Dict[str, Any]) -> set:
    return {acct["address"].lower(), _sender_address(acct).lower()}

def _format_address_header(pairs: list) -> str:
    return ", ".join(formataddr(pair) for pair in pairs)

def _merge_reply_recipients(to_addr: str, auto_cc: list, cc: list, bcc: list, own: set) -> tuple[list, list, list]:
    """Combine automatic and caller-supplied reply recipients; each address is mailed once (To > Cc > Bcc)."""
    taken = {to_addr.lower()}
    dropped: list = []
    merged_cc: list = []
    merged_bcc: list = []
    for name, addr in auto_cc:
        if addr.lower() in own or addr.lower() in taken:
            continue
        taken.add(addr.lower())
        merged_cc.append((name, addr))
    for pairs, field, target in ((cc, "cc", merged_cc), (bcc, "bcc", merged_bcc)):
        for name, addr in pairs:
            key = addr.lower()
            if key in own:
                dropped.append({"address": addr, "field": field, "reason": "own_address"})
            elif key in taken:
                dropped.append({"address": addr, "field": field, "reason": "duplicate"})
            else:
                taken.add(key)
                target.append((name, addr))
    return merged_cc, merged_bcc, dropped

def _caller_pairs(p: Dict[str, Any], name: str) -> list:
    return list(p[name].pairs) if p.get(name) else []

def _reply_address(msg: Message) -> Optional[str]:
    """Resolve Reply-To/From to one safe SMTP mailbox address."""
    candidates = _parse_address_list(
        _decode_header_value(msg.get("Reply-To")),
        _decode_header_value(msg.get("From")),
    )
    return candidates[0] if candidates else None

def _split_attachment_paths(file_attachments: Optional[str]) -> list[str]:
    if not file_attachments:
        return []
    return [filepath.strip() for filepath in file_attachments.split(",") if filepath.strip()]

def _collect_attachment_metadata(file_attachments: Optional[str]) -> tuple[list[dict], int]:
    attachment_info = []
    total_size = 0
    for filepath in _split_attachment_paths(file_attachments):
        resolved_path = str(Path(filepath).expanduser().resolve(strict=False))
        exists = os.path.isfile(filepath)
        size_bytes = os.path.getsize(filepath) if exists else None
        content_type, _ = mimetypes.guess_type(filepath)
        if content_type is None:
            content_type = "application/octet-stream"
        entry = {
            "path": filepath,
            "resolved_path": resolved_path,
            "filename": os.path.basename(filepath),
            "exists": exists,
            "size_bytes": size_bytes,
            "content_type": content_type,
        }
        attachment_info.append(entry)
        if size_bytes is not None:
            total_size += size_bytes
    return attachment_info, total_size

def _sender_address(acct: Dict[str, Any]) -> str:
    return acct.get("send_as") or acct["address"]

def _format_from_header(acct: Dict[str, Any]) -> str:
    sender = _sender_address(acct)
    display_name = (acct.get("display_name") or "").strip()
    if display_name:
        return formataddr((display_name, sender))
    return sender

def _compose_and_send(
    acct: Dict[str, Any], to: str, subject: str, body: str,
    body_html: Optional[str] = None, cc: Optional[str] = None, bcc: Optional[str] = None,
    in_reply_to: Optional[str] = None, references: Optional[str] = None,
    file_attachments: Optional[str] = None, inline_attachments: Optional[str] = None,
    calendar_ics: Optional[str] = None, forwarded_parts: Optional[list] = None,
    reply_to: Optional[str] = None,
) -> dict:
    has_attachments = bool(file_attachments and file_attachments.strip()) or bool(inline_attachments) or bool(forwarded_parts)
    # Build body part
    if calendar_ics:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body, "plain", "utf-8"))
        ics_part = MIMEText(calendar_ics, "calendar", "utf-8")
        ics_part.replace_header("Content-Type", "text/calendar; method=REQUEST; charset=utf-8")
        body_part.attach(ics_part)
    elif body_html:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body, "plain", "utf-8"))
        body_part.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        body_part = MIMEText(body, "plain", "utf-8")
    # Only wrap in multipart/mixed if there are attachments
    if has_attachments:
        mime = MIMEMultipart("mixed")
        mime.attach(body_part)
    else:
        mime = body_part
    sender = _sender_address(acct)
    mime["From"] = _format_from_header(acct)
    mime["To"] = to
    mime["Subject"] = subject
    mime["Date"] = formatdate(localtime=True)
    mime["Message-ID"] = make_msgid(domain=sender.split("@", 1)[1])
    if cc: mime["Cc"] = cc
    if reply_to: mime["Reply-To"] = reply_to
    if in_reply_to: mime["In-Reply-To"] = in_reply_to
    if references: mime["References"] = references
    if file_attachments:
        for filepath in file_attachments.split(","):
            filepath = filepath.strip()
            if not filepath: continue
            if not os.path.isfile(filepath):
                return {"error": f"Attachment not found: {filepath}"}
            filename = os.path.basename(filepath)
            ctype, _ = mimetypes.guess_type(filepath)
            if ctype is None: ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(filepath, "rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            mime.attach(part)
    if inline_attachments:
        try:
            for att in json.loads(inline_attachments):
                filename = att.get("filename", "attachment")
                content_type = att.get("content_type", "application/octet-stream")
                maintype, subtype = content_type.split("/", 1)
                file_data = base64.b64decode(att["content_base64"])
                part = MIMEBase(maintype, subtype)
                part.set_payload(file_data)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=filename)
                mime.attach(part)
        except json.JSONDecodeError:
            return {"error": "attachments_inline must be a valid JSON array."}
        except Exception as e:
            return {"error": f"Error processing inline attachment: {e}"}
    if forwarded_parts:
        for part in forwarded_parts:
            mime.attach(part)
    to_recipients = _parse_address_list(to)
    if not to_recipients:
        return {"error": "No valid recipient address found in 'to'."}
    recipients = _parse_address_list(to, cc, bcc)
    mime_str = mime.as_string()
    _smtp_send(acct, sender, recipients, mime_str)
    sent_warning = _save_to_sent(acct, mime_str)
    result = {"status": "sent", "from": sender, "to": to, "subject": subject}
    if cc: result["cc"] = cc
    if bcc: result["bcc"] = bcc
    if reply_to: result["reply_to"] = reply_to
    if sent_warning: result["warning"] = sent_warning
    return result

# ─── Action schemas and parameter validation ─────────────────────────────────

@dataclass(frozen=True)
class Param:
    kind: str
    description: str
    required: bool = False
    default: Any = None
    min: Optional[int] = None
    max: Optional[int] = None
    choices: tuple = ()
    single_line: bool = False
    blank_is_absent: bool = False
    type_hint: str = ""

@dataclass(frozen=True)
class ActionSchema:
    description: str
    params: Dict[str, Param]
    rules: tuple = ()  # callables (values) -> list of problem dicts, run after the single-field checks

class Addresses(NamedTuple):
    raw: str
    pairs: tuple

_ATTACHMENT_PATHS = "comma-separated file paths"
_INLINE_JSON = '[{"filename":"f.pdf","content_base64":"...","content_type":"..."}]'

def _account_param() -> Param:
    return Param("str", "account name or address; required when several accounts are configured")

def _folder_param(description: str = "folder name as shown by list_folders ('name' or 'raw_name')") -> Param:
    return Param("folder", description, default="INBOX")

_UID = Param("uid", "UID of the email", required=True)
_UID_LIST = Param("uid_list", "UID of the email, or a list of UIDs (up to 500, also accepted as '101, 102')", required=True, max=500)
_BODY_HTML = Param("str", "HTML body", blank_is_absent=True)
_ATTACHMENTS = Param("str", _ATTACHMENT_PATHS, blank_is_absent=True)
_ATTACHMENTS_INLINE = Param("json_array", "JSON array " + _INLINE_JSON, blank_is_absent=True)
_REPLY_TO = Param("addresses", "Reply-To header address(es): where answers to this mail should go", blank_is_absent=True)
_CC = Param("addresses", "Cc address(es)", blank_is_absent=True)
_BCC = Param("addresses", "Bcc address(es)", blank_is_absent=True)
_CONFIRMATION = Param("str", "required if a send_code is configured", single_line=True, type_hint="pass the code as a string")

ACTION_SCHEMAS: Dict[str, ActionSchema] = {
    "validate_config": ActionSchema("Validate accounts.json or environment configuration without logging into IMAP/SMTP.", {}),
    "list_accounts": ActionSchema("List configured email accounts.", {}),
    "list_folders": ActionSchema("List IMAP folders.", {"account": _account_param()}),
    "list_emails": ActionSchema("List recent emails.", {
        "account": _account_param(), "folder": _folder_param(),
        "limit": Param("int", "number of emails", default=20, min=1, max=100)}),
    "search": ActionSchema("Search emails (IMAP: FROM, TO, SUBJECT, BODY, SINCE, BEFORE, UNSEEN, SEEN, FLAGGED). Example: 'FROM john SINCE 01-Feb-2025'", {
        "query": Param("str", "IMAP search query", required=True, single_line=True), "account": _account_param(),
        "folder": _folder_param(), "limit": Param("int", "number of emails", default=20, min=1)}),
    "read": ActionSchema("Read full email by UID.", {"uid": _UID, "account": _account_param(), "folder": _folder_param()}),
    "send": ActionSchema("Send email. confirmation_code required if send_code configured.", {
        "to": Param("addresses", "recipient address(es)", required=True),
        "subject": Param("str", "subject line", required=True, single_line=True),
        "body": Param("str", "plain text body", required=True),
        "account": _account_param(),
        "cc": _CC, "bcc": _BCC, "reply_to": _REPLY_TO,
        "body_html": _BODY_HTML, "attachments": _ATTACHMENTS, "attachments_inline": _ATTACHMENTS_INLINE,
        "calendar_ics": Param("str", "iCalendar text to attach as an invite", blank_is_absent=True),
        "confirmation_code": _CONFIRMATION,
        "reply_to_uid": Param("uid", "thread this mail as a reply to the message with this UID (sets In-Reply-To/References); not the Reply-To header, see reply_to"),
        "reply_to_folder": _folder_param("folder holding reply_to_uid"),
    }),
    "reply": ActionSchema("Reply to an email.", {
        "uid": _UID, "body": Param("str", "plain text reply", required=True), "account": _account_param(),
        "folder": _folder_param(), "body_html": _BODY_HTML, "attachments": _ATTACHMENTS,
        "attachments_inline": _ATTACHMENTS_INLINE, "confirmation_code": _CONFIRMATION,
        "cc": _CC, "bcc": _BCC, "reply_to": _REPLY_TO}),
    "reply_all": ActionSchema("Reply to all recipients.", {
        "uid": _UID, "body": Param("str", "plain text reply", required=True), "account": _account_param(),
        "folder": _folder_param(), "body_html": _BODY_HTML, "attachments": _ATTACHMENTS,
        "attachments_inline": _ATTACHMENTS_INLINE, "confirmation_code": _CONFIRMATION,
        "cc": _CC, "bcc": _BCC, "reply_to": _REPLY_TO}),
    "forward": ActionSchema("Forward an email.", {
        "uid": _UID, "to": Param("addresses", "recipient address(es)", required=True), "account": _account_param(),
        "folder": _folder_param(), "body": Param("str", "text added above the forwarded message"),
        "body_html": _BODY_HTML, "include_attachments": Param("bool", "forward the original attachments", default=True),
        "attachments": _ATTACHMENTS, "attachments_inline": _ATTACHMENTS_INLINE, "confirmation_code": _CONFIRMATION}),
    "move": ActionSchema("Move one or more emails to another folder.", {
        "uid": _UID_LIST, "destination_folder": Param("folder", "target folder", required=True), "account": _account_param(),
        "source_folder": _folder_param("folder the email is in")}),
    "mark": ActionSchema("Mark one or more emails read/unread/flag/unflag.", {
        "uid": _UID_LIST, "action": Param("choice", "what to set", required=True, choices=("read", "unread", "flag", "unflag")),
        "account": _account_param(), "folder": _folder_param()}),
    "save_attachment": ActionSchema("Save attachment to disk.", {
        "uid": _UID, "attachment_index": Param("int", "index from read", required=True, min=0),
        "save_path": Param("str", "target path on disk", required=True), "account": _account_param(),
        "folder": _folder_param(), "overwrite": Param("bool", "replace an existing file", default=False)}),
    "get_attachment": ActionSchema("Get attachment as base64 (prefer save_attachment).", {
        "uid": _UID, "attachment_index": Param("int", "index from read", required=True, min=0),
        "account": _account_param(), "folder": _folder_param()}),
    "prepare_attachments": ActionSchema("Inspect local files before attaching.", {
        "attachments": Param("str", _ATTACHMENT_PATHS, required=True)}),
}

# ─── Validation (the only place where caller input is checked) ───────────────

_MAX_UID = 4294967295

class _Invalid(Exception):
    def __init__(self, problem: str, **extra: Any):
        super().__init__(problem)
        self.problem = problem
        self.extra = extra

def _json_type(value: Any) -> str:
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, (int, float)): return "number"
    if isinstance(value, str): return "string"
    if isinstance(value, list): return "array"
    if isinstance(value, dict): return "object"
    return "other"

def _wrong_type(param: Param, value: Any, **extra: Any) -> _Invalid:
    if param.type_hint:
        extra["hint"] = param.type_hint
    return _Invalid("wrong_type", expected=param.kind, got=_json_type(value), **extra)

def _range_check(number: int, low: Optional[int], high: Optional[int]) -> int:
    if (low is not None and number < low) or (high is not None and number > high):
        extra = {key: bound for key, bound in (("min", low), ("max", high)) if bound is not None}
        raise _Invalid("out_of_range", **extra)
    return number

def _to_int(value: Any, param: Param) -> int:
    if isinstance(value, bool):
        raise _wrong_type(param, value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        if len(value) > 20:
            raise _Invalid("out_of_range", **{key: bound for key, bound in (("min", param.min), ("max", param.max)) if bound is not None})
        return int(value)
    raise _wrong_type(param, value)

_HEADER_UNSAFE = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")

def _coerce_str(value: Any, param: Param) -> str:
    if not isinstance(value, str):
        raise _wrong_type(param, value)
    if param.single_line and _HEADER_UNSAFE.search(value):
        raise _Invalid("invalid_value", hint="must be a single line")
    return value

def _coerce_int(value: Any, param: Param) -> int:
    return _range_check(_to_int(value, param), param.min, param.max)

def _coerce_uid(value: Any, param: Param) -> int:
    return _range_check(_to_int(value, param), 1, _MAX_UID)

def _coerce_uid_list(value: Any, param: Param) -> list:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    if not items:
        raise _Invalid("invalid_value", hint="at least one UID is required")
    uids = list(dict.fromkeys(_coerce_uid(item.strip() if isinstance(item, str) else item, param) for item in items))
    if param.max is not None and len(uids) > param.max:
        raise _Invalid("too_many", max=param.max, got=len(uids))
    return uids

def _coerce_bool(value: Any, param: Param) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise _wrong_type(param, value)

def _coerce_strict_bool(value: Any, param: Param) -> bool:
    if isinstance(value, bool):
        return value
    raise _wrong_type(param, value)

def _coerce_folder(value: Any, param: Param) -> str:
    if not isinstance(value, str):
        raise _wrong_type(param, value)
    if not value or re.search(r"[\r\n\x00]", value):
        raise _Invalid("invalid_value", hint="must be a non-empty folder name without line breaks")
    return value

def _coerce_addresses(value: Any, param: Param) -> Addresses:
    if not isinstance(value, str):
        raise _wrong_type(param, value)
    if _HEADER_UNSAFE.search(value):
        raise _Invalid("invalid_value", hint="must not contain line breaks or control characters")
    pairs = [(name, addr.strip()) for name, addr in getaddresses([value])]
    for index, (_, addr) in enumerate(pairs):
        if not _looks_like_email(addr) or re.search(r"\s", addr):
            raise _Invalid("invalid_address", index=index)
    if not pairs:
        raise _Invalid("invalid_address")
    return Addresses(value, tuple(pairs))

def _coerce_json_array(value: Any, param: Param) -> str:
    if not isinstance(value, str):
        raise _wrong_type(param, value)
    return value

def _coerce_choice(value: Any, param: Param) -> str:
    if not isinstance(value, str):
        raise _wrong_type(param, value)
    if value.lower() in param.choices:
        return value.lower()
    raise _Invalid("invalid_value", allowed=list(param.choices),
                   did_you_mean=difflib.get_close_matches(value.lower(), param.choices, n=3, cutoff=0.6))

_COERCERS: Dict[str, Callable[[Any, Param], Any]] = {
    "str": _coerce_str, "int": _coerce_int, "bool": _coerce_bool, "strict_bool": _coerce_strict_bool,
    "uid": _coerce_uid, "uid_list": _coerce_uid_list, "folder": _coerce_folder, "addresses": _coerce_addresses,
    "json_array": _coerce_json_array, "choice": _coerce_choice,
}

_PARAM_SYNONYMS: Dict[str, tuple] = {
    "target_folder": ("destination_folder",), "to_folder": ("destination_folder",),
    "dest": ("destination_folder",), "dest_folder": ("destination_folder",),
    "destination": ("destination_folder",), "folder": ("source_folder", "destination_folder"),
    "html": ("body_html",), "html_body": ("body_html",),
    "reply_to_address": ("reply_to",), "replyto": ("reply_to",), "uids": ("uid",),
}

def _snake_case(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).replace("-", "_").lower()

def _suggest_param(name: str, action: str) -> dict:
    valid = list(ACTION_SCHEMAS[action].params)
    normalized = _snake_case(name)
    if normalized in valid:
        return {"did_you_mean": [normalized]}
    if normalized in ("from", "sender") and "account" in valid:
        return {"hint": "the sender is the account; use 'account'"}
    synonyms = [target for target in _PARAM_SYNONYMS.get(normalized, ()) if target in valid]
    if synonyms:
        return {"did_you_mean": synonyms}
    close = difflib.get_close_matches(normalized, valid, n=3, cutoff=0.6)
    return {"did_you_mean": close} if close else {}

def _suggest_action(name: str) -> list:
    return difflib.get_close_matches(_snake_case(str(name)).strip(), list(ACTION_SCHEMAS), n=3, cutoff=0.6)

def _validate_params(action: str, params: Dict[str, Any]) -> tuple[Dict[str, Any], list]:
    schema = ACTION_SCHEMAS[action]
    values: Dict[str, Any] = {}
    problems: list = []
    for name in params:
        if name not in schema.params:
            problems.append({"param": name, "problem": "unknown_param", **_suggest_param(name, action)})
    for name, param in schema.params.items():
        value = params.get(name)
        if value is None or (param.blank_is_absent and value == ""):
            if param.required:
                problems.append({"param": name, "problem": "missing"})
            elif param.default is not None:
                values[name] = param.default
            continue
        try:
            values[name] = _COERCERS[param.kind](value, param)
        except _Invalid as invalid:
            extra = dict(invalid.extra)
            if (name == "reply_to" and invalid.problem == "invalid_address" and "reply_to_uid" in schema.params
                    and isinstance(value, str) and value.strip().isdigit()):
                extra["did_you_mean"] = ["reply_to_uid"]
            problems.append({"param": name, "problem": invalid.problem, **extra})
    if not problems:
        for rule in schema.rules:
            problems.extend(rule(values))
    return values, problems

_KIND_LABELS = {"str": "str", "int": "int", "bool": "bool", "strict_bool": "bool", "uid": "int", "uid_list": "int or list of int",
                "folder": "str", "addresses": "str (comma-separated addresses)", "json_array": "str (JSON array)"}

def _describe_param(param: Param) -> str:
    label = "|".join(param.choices) if param.kind == "choice" else _KIND_LABELS[param.kind]
    parts = [label, "required" if param.required else "optional"]
    if param.default is not None:
        parts.append(f"default={str(param.default).lower() if isinstance(param.default, bool) else param.default}")
    if param.kind == "uid_list":
        parts.append(f"max {param.max} per call")
    elif param.min is not None or param.max is not None:
        parts.append(f"{'' if param.min is None else param.min}-{'' if param.max is None else param.max}")
    return f"{param.description} ({', '.join(parts)})"

def _schema_view(action: str) -> dict:
    schema = ACTION_SCHEMAS[action]
    return {"action": action, "description": schema.description,
            "params": {name: _describe_param(param) for name, param in schema.params.items()}}

def _problem_text(action: str, problem: dict) -> str:
    name, kind = problem["param"], problem["problem"]
    if kind == "unknown_param":
        if problem.get("did_you_mean"):
            return f"unknown param '{name}' (did you mean {' or '.join(repr(n) for n in problem['did_you_mean'])}?)"
        if problem.get("hint"):
            return f"unknown param '{name}' ({problem['hint']})"
        return f"unknown param '{name}' (valid params: {', '.join(ACTION_SCHEMAS[action].params) or 'none'})"
    if kind == "missing":
        return f"missing required param '{name}'"
    if kind == "wrong_type":
        text = f"'{name}' must be of type {problem['expected']} (got {problem['got']})"
        return text + (f"; {problem['hint']}" if problem.get("hint") else "")
    if kind == "out_of_range":
        return f"'{name}' is out of range ({problem.get('min', '')}-{problem.get('max', '')})"
    if kind == "too_many":
        return f"'{name}' has {problem['got']} entries, max is {problem['max']}"
    if kind == "invalid_value":
        text = f"'{name}' has an invalid value"
        if problem.get("allowed"):
            text += f" (allowed: {', '.join(problem['allowed'])})"
        if problem.get("did_you_mean"):
            text += f" (did you mean {' or '.join(repr(n) for n in problem['did_you_mean'])}?)"
        return text + (f"; {problem['hint']}" if problem.get("hint") else "")
    if kind == "invalid_address":
        return f"'{name}' contains an invalid email address"
    if kind == "conflict":
        return f"'{name}' cannot be combined with '{problem.get('with')}'"
    return f"'{name}' is invalid ({kind})"

def _validation_error(action: str, problems: list) -> str:
    texts = "; ".join(_problem_text(action, p) for p in problems)
    return json.dumps({"error": f"Invalid params for action '{action}': {texts}.", "problems": problems, **_schema_view(action)})

def _unknown_action(action: Any) -> str:
    suggestions = _suggest_action(action)
    text = "Unknown action"
    if suggestions:
        text += f" (did you mean {' or '.join(repr(n) for n in suggestions)}?)"
    problems = [{"param": "action", "problem": "invalid_value", "did_you_mean": suggestions}]
    return json.dumps({"error": text, "problems": problems, "available_actions": list(ACTION_SCHEMAS)})

# ─── Internal handlers ───────────────────────────────────────────────────────

async def _do_list_accounts() -> str:
    _refresh_runtime_config()
    if not _accounts:
        return "No accounts configured. See README for setup."
    accounts_info = []
    for name, cfg in _accounts.items():
        info = {
            "name": name,
            "address": cfg["address"],
            "send_as": _sender_address(cfg),
            "imap_host": cfg["imap_host"],
            "smtp_host": cfg["smtp_host"],
        }
        if cfg.get("display_name"): info["display_name"] = cfg["display_name"]
        if cfg.get("description"): info["description"] = cfg["description"]
        accounts_info.append(info)
    return json.dumps({"accounts": accounts_info, "count": len(accounts_info)}, indent=2)

async def _do_validate_config() -> str:
    return json.dumps(_validate_config(), indent=2)

async def _do_list_folders(account: Optional[str] = None) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct) as conn:
            status, folder_list = conn.list()
            if status != "OK":
                return "Error: Could not retrieve folder list."
            parsed = _parse_imap_list_response(folder_list)
            folder_info = []
            for f in parsed:
                entry = {"name": f["name"], "raw_name": f["raw_name"]}
                if _has_flag(f, "\\Sent"): entry["type"] = "sent"
                elif _has_flag(f, "\\Trash") or _has_flag(f, "\\Deleted"): entry["type"] = "trash"
                elif _has_flag(f, "\\Drafts"): entry["type"] = "drafts"
                elif _has_flag(f, "\\Junk") or _has_flag(f, "\\Spam"): entry["type"] = "spam"
                elif _has_flag(f, "\\Archive"): entry["type"] = "archive"
                elif f["raw_name"].upper() == "INBOX": entry["type"] = "inbox"
                folder_info.append(entry)
            return json.dumps({"account": acct["address"], "folders": folder_info, "count": len(folder_info), "hint": "Use 'name' or 'raw_name' in folder parameters."}, indent=2)
    except Exception as e:
        return f"Error listing folders: {e}"

async def _do_list_emails(account: Optional[str], folder: str, limit: int) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct) as conn:
            opened = _open_folder(conn, folder)
            status, data = conn.uid("search", None, "ALL")
            if status != "OK":
                return f"Error: Could not search folder '{opened['name']}'."
            uids = data[0].split() if data[0] else []
            uids = uids[-limit:]
            uids.reverse()
            results = []
            for uid_bytes in uids:
                uid = uid_bytes.decode()
                status, msg_data = conn.uid("fetch", uid, "(RFC822.HEADER)")
                if status != "OK" or not msg_data or not msg_data[0]: continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if isinstance(raw, bytes):
                    results.append(_msg_to_summary(email.message_from_bytes(raw), uid))
            return json.dumps({"account": acct["address"], "folder": opened["name"], "count": len(results), "emails": results}, indent=2)
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error listing emails: {e}"

async def _do_search(account: Optional[str], query: str, folder: str, limit: int) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct) as conn:
            opened = _open_folder(conn, folder)
            try:
                status, data = conn.uid("search", None, query)
            except imaplib.IMAP4.error as e:
                return f"Error: Invalid IMAP search query '{query}': {e}"
            if status != "OK":
                return f"Error: Search failed for query '{query}'."
            uids = data[0].split() if data[0] else []
            uids = uids[-limit:]
            uids.reverse()
            results = []
            for uid_bytes in uids:
                uid = uid_bytes.decode()
                status, msg_data = conn.uid("fetch", uid, "(RFC822.HEADER)")
                if status != "OK" or not msg_data or not msg_data[0]: continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if isinstance(raw, bytes):
                    results.append(_msg_to_summary(email.message_from_bytes(raw), uid))
            return json.dumps({"account": acct["address"], "query": query, "folder": opened["name"], "count": len(results), "emails": results}, indent=2)
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error searching emails: {e}"

async def _do_read(account: Optional[str], uid: str, folder: str) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct) as conn:
            _open_folder(conn, folder)
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            msg = email.message_from_bytes(raw)
            body = _extract_body(msg)
            attachments = _list_attachments(msg)
            date_str = msg.get("Date", "")
            try:
                date_formatted = parsedate_to_datetime(date_str).strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                date_formatted = date_str
            return json.dumps({"account": acct["address"], "uid": uid, "from": _decode_header_value(msg.get("From")), "to": _decode_header_value(msg.get("To")), "cc": _decode_header_value(msg.get("Cc")), "subject": _decode_header_value(msg.get("Subject")), "date": date_formatted, "message_id": msg.get("Message-ID", ""), "in_reply_to": msg.get("In-Reply-To", ""), "body": body[:50000], "attachments": attachments}, indent=2, ensure_ascii=False)
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error reading email: {e}"

def _reply_to_header(p: Dict[str, Any]) -> Optional[str]:
    return _format_address_header(p["reply_to"].pairs) if p.get("reply_to") else None

def _raw_addresses(p: Dict[str, Any], name: str) -> Optional[str]:
    return p[name].raw if p.get(name) else None

async def _do_send(p: Dict[str, Any]) -> str:
    blocked = _check_confirmation_code(p.get("confirmation_code"))
    if blocked:
        return blocked
    try:
        acct = _resolve_account(p.get("account"))
        in_reply_to = references = None
        if p.get("reply_to_uid"):
            with _imap_session(acct) as conn:
                opened = _open_folder(conn, p["reply_to_folder"], "reply_to_folder")
                status, msg_data = conn.uid("fetch", str(p["reply_to_uid"]), "(RFC822.HEADER)")
                raw = msg_data[0][1] if status == "OK" and msg_data and isinstance(msg_data[0], tuple) else (msg_data[0] if status == "OK" and msg_data else None)
                if not isinstance(raw, bytes):
                    return f"Error: reply_to_uid {p['reply_to_uid']} not found in folder '{opened['name']}'; nothing was sent."
                orig_id = email.message_from_bytes(raw).get("Message-ID", "")
                if orig_id:
                    in_reply_to = orig_id
                    references = orig_id
        result = _compose_and_send(acct, p["to"].raw, p["subject"], p["body"], body_html=p.get("body_html"), cc=_raw_addresses(p, "cc"), bcc=_raw_addresses(p, "bcc"), in_reply_to=in_reply_to, references=references, file_attachments=p.get("attachments"), inline_attachments=p.get("attachments_inline"), calendar_ics=p.get("calendar_ics"), reply_to=_reply_to_header(p))
        if "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error sending email: {e}"

async def _do_reply(p: Dict[str, Any]) -> str:
    blocked = _check_confirmation_code(p.get("confirmation_code"))
    if blocked:
        return blocked
    try:
        acct = _resolve_account(p.get("account"))
        uid = str(p["uid"])
        with _imap_session(acct) as conn:
            _open_folder(conn, p["folder"])
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            orig = email.message_from_bytes(raw)
        orig_from = _decode_header_value(orig.get("From"))
        orig_subject = _decode_header_value(orig.get("Subject"))
        orig_date = orig.get("Date", "")
        orig_body = _extract_body(orig)
        orig_msg_id = orig.get("Message-ID", "")
        reply_addr = _reply_address(orig)
        if not reply_addr:
            return "Error: Original email has no valid Reply-To or From address."
        subject = orig_subject if re.match(r'(?i)^Re:\s', orig_subject) else f"Re: {orig_subject}"
        full_body = f"{p['body']}\n\n{_quote_body(orig_body, orig_from, orig_date)}"
        full_body_html = None
        if p.get("body_html"):
            quoted_html = orig_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            full_body_html = f"{p['body_html']}<br><br><div style='border-left:2px solid #ccc;padding-left:10px;color:#555;'>On {orig_date}, {orig_from} wrote:<br>{quoted_html}</div>"
        cc_pairs, bcc_pairs, dropped = _merge_reply_recipients(reply_addr, [], _caller_pairs(p, "cc"), _caller_pairs(p, "bcc"), _own_addresses(acct))
        result = _compose_and_send(acct, reply_addr, subject, full_body, body_html=full_body_html, cc=_format_address_header(cc_pairs) or None, bcc=", ".join(addr for _, addr in bcc_pairs) or None, in_reply_to=orig_msg_id, references=orig_msg_id, file_attachments=p.get("attachments"), inline_attachments=p.get("attachments_inline"), reply_to=_reply_to_header(p))
        if dropped and "error" not in result:
            result["dropped"] = dropped
        if "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error replying: {e}"

async def _do_reply_all(p: Dict[str, Any]) -> str:
    blocked = _check_confirmation_code(p.get("confirmation_code"))
    if blocked:
        return blocked
    try:
        acct = _resolve_account(p.get("account"))
        uid = str(p["uid"])
        with _imap_session(acct) as conn:
            _open_folder(conn, p["folder"])
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            orig = email.message_from_bytes(raw)
        orig_from = _decode_header_value(orig.get("From"))
        orig_to = _decode_header_value(orig.get("To"))
        orig_cc_h = _decode_header_value(orig.get("Cc"))
        orig_subject = _decode_header_value(orig.get("Subject"))
        orig_date = orig.get("Date", "")
        orig_body = _extract_body(orig)
        orig_msg_id = orig.get("Message-ID", "")
        reply_addr = _reply_address(orig)
        if not reply_addr:
            return "Error: Original email has no valid Reply-To or From address."
        auto_cc = [("", addr) for addr in _parse_address_list(orig_to, orig_cc_h)]
        subject = orig_subject if re.match(r'(?i)^Re:\s', orig_subject) else f"Re: {orig_subject}"
        full_body = f"{p['body']}\n\n{_quote_body(orig_body, orig_from, orig_date)}"
        full_body_html = None
        if p.get("body_html"):
            quoted_html = orig_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            full_body_html = f"{p['body_html']}<br><br><div style='border-left:2px solid #ccc;padding-left:10px;color:#555;'>On {orig_date}, {orig_from} wrote:<br>{quoted_html}</div>"
        cc_pairs, bcc_pairs, dropped = _merge_reply_recipients(reply_addr, auto_cc, _caller_pairs(p, "cc"), _caller_pairs(p, "bcc"), _own_addresses(acct))
        result = _compose_and_send(acct, reply_addr, subject, full_body, body_html=full_body_html, cc=_format_address_header(cc_pairs) or None, bcc=", ".join(addr for _, addr in bcc_pairs) or None, in_reply_to=orig_msg_id, references=orig_msg_id, file_attachments=p.get("attachments"), inline_attachments=p.get("attachments_inline"), reply_to=_reply_to_header(p))
        if dropped and "error" not in result:
            result["dropped"] = dropped
        if "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error replying all: {e}"

async def _do_forward(p: Dict[str, Any]) -> str:
    blocked = _check_confirmation_code(p.get("confirmation_code"))
    if blocked:
        return blocked
    try:
        acct = _resolve_account(p.get("account"))
        uid = str(p["uid"])
        with _imap_session(acct) as conn:
            _open_folder(conn, p["folder"])
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            orig = email.message_from_bytes(raw)
        orig_from = _decode_header_value(orig.get("From"))
        orig_to = _decode_header_value(orig.get("To"))
        orig_subject = _decode_header_value(orig.get("Subject"))
        orig_date = orig.get("Date", "")
        orig_body = _extract_body(orig)
        subject = orig_subject if re.match(r'(?i)^Fwd?:\s', orig_subject) else f"Fwd: {orig_subject}"
        fwd_header = f"---------- Forwarded message ----------\nFrom: {orig_from}\nDate: {orig_date}\nSubject: {orig_subject}\nTo: {orig_to}\n\n"
        body = p.get("body", "")
        full_body = f"{body}\n\n{fwd_header}{orig_body}" if body else f"{fwd_header}{orig_body}"
        full_body_html = None
        if p.get("body_html"):
            fwd_header_html = f"<b>---------- Forwarded message ----------</b><br>From: {orig_from}<br>Date: {orig_date}<br>Subject: {orig_subject}<br>To: {orig_to}<br><br>"
            orig_body_html = orig_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            full_body_html = f"{p['body_html']}<br><br>{fwd_header_html}{orig_body_html}"
        forwarded_parts = []
        if p.get("include_attachments", True) and orig.is_multipart():
            for part in orig.walk():
                if _is_attachment_part(part):
                    forwarded_parts.append(part)
        result = _compose_and_send(acct, p["to"].raw, subject, full_body, body_html=full_body_html, file_attachments=p.get("attachments"), inline_attachments=p.get("attachments_inline"), forwarded_parts=forwarded_parts)
        if "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error forwarding: {e}"

def _uid_set(uids: list) -> str:
    return ",".join(str(uid) for uid in uids)

def _server_text(data: Any) -> str:
    parts = []
    for item in data or []:
        if isinstance(item, bytes):
            parts.append(item.decode("utf-8", errors="replace"))
        elif isinstance(item, str):
            parts.append(item)
    return " ".join(parts).strip()[:200]

def _uid_command(conn: imaplib.IMAP4_SSL, *args: str) -> tuple[str, list]:
    # imaplib raises for BAD but returns NO; treat both alike.
    try:
        return conn.uid(*args)
    except imaplib.IMAP4.error as e:
        return "BAD", [str(e).encode("utf-8", errors="replace")]

def _existing_uids(conn: imaplib.IMAP4_SSL, uids: list) -> tuple[list, list]:
    status, data = conn.uid("SEARCH", None, "UID", _uid_set(uids))
    if status != "OK":
        raise RuntimeError("Could not look up the UIDs in the folder.")
    present = {int(u) for u in (data[0].split() if data and data[0] else [])}
    return [u for u in uids if u in present], [u for u in uids if u not in present]

def _still_in_folder(conn: imaplib.IMAP4_SSL, uids: list) -> Optional[set]:
    """UIDs still present after a failed command, or None if even that cannot be established."""
    try:
        return set(_existing_uids(conn, uids)[0])
    except Exception:
        return None

def _bulk_result(base: dict, requested: int, done_key: str, done: list, not_found: list, failed: list,
                 duplicated: Optional[list] = None, error: Optional[str] = None) -> str:
    status = "ok" if len(done) == requested else ("failed" if not done else "partial")
    result = {"status": status, **base, "requested": requested, done_key: [str(u) for u in done],
              "not_found": [str(u) for u in not_found], "failed": [str(u) for u in failed]}
    if duplicated is not None:
        result["duplicated"] = [str(u) for u in duplicated]
    if error and (failed or duplicated):
        result["error"] = error
    return json.dumps(result, indent=2)

async def _do_move(account: Optional[str], uids: list, source_folder: str, destination_folder: str) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct) as conn:
            resolved = _resolve_folders(conn, {"source_folder": source_folder, "destination_folder": destination_folder})
            src, dst = resolved["source_folder"], resolved["destination_folder"]
            if src["raw_name"] == dst["raw_name"]:
                raise FolderError({"error": "Source and destination folder are the same.", "param": "destination_folder", "problem": "same_folder"})
            _select(conn, src, readonly=False)
            quoted_destination = _quote_mailbox(dst["raw_name"])
            method = _move_method(_imap_capabilities(conn))
            if method is None:
                return "Error: server supports neither MOVE nor UIDPLUS; refusing to move because a plain EXPUNGE could delete other messages."
            found, not_found = _existing_uids(conn, uids)
            base = {"account": acct["address"], "source_folder": src["name"], "destination_folder": dst["name"], "method": method}
            moved, failed, duplicated, error = [], [], [], None
            if found:
                uid_set = _uid_set(found)
                if method == "move":
                    status, data = _uid_command(conn, "MOVE", uid_set, quoted_destination)
                    if status == "OK":
                        moved = found
                    else:
                        error = _server_text(data) or "MOVE failed"
                        left = _still_in_folder(conn, found)
                        if left is None:
                            error += "; could not verify which emails were moved"
                            left = set(found)
                        failed = [u for u in found if u in left]
                        moved = [u for u in found if u not in left]
                else:
                    status, data = _uid_command(conn, "COPY", uid_set, quoted_destination)
                    if status != "OK":
                        error = _server_text(data) or "COPY failed"
                        failed = found
                    else:
                        fetched, snapshot = _uid_command(conn, "FETCH", uid_set, "(FLAGS)")
                        flags_before = _flags_by_uid(snapshot) if fetched == "OK" else {}
                        already_deleted = {u for u, flags in flags_before.items() if "\\Deleted" in flags} | (set(found) - set(flags_before))
                        status, data = _uid_command(conn, "STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
                        if status == "OK":
                            status, data = _uid_command(conn, "EXPUNGE", uid_set)
                        if status == "OK":
                            moved = found
                        else:
                            error = _server_text(data) or "removing the originals failed"
                            left = _still_in_folder(conn, found)
                            if left is None:
                                error += "; could not verify which emails were moved"
                                left = set(found)
                            duplicated = [u for u in found if u in left]
                            moved = [u for u in found if u not in left]
                            to_clear = [u for u in duplicated if u not in already_deleted]
                            if to_clear:
                                status, _ = _uid_command(conn, "STORE", _uid_set(to_clear), "-FLAGS.SILENT", "(\\Deleted)")
                                if status != "OK":
                                    error += "; could not remove the \\Deleted flag again"
            return _bulk_result(base, len(uids), "moved", moved, not_found, failed, duplicated, error)
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error moving emails: {e}"

_FLAG_CHANGES = {"read": ("+", "\\Seen"), "unread": ("-", "\\Seen"), "flag": ("+", "\\Flagged"), "unflag": ("-", "\\Flagged")}

def _flags_by_uid(data: list) -> Dict[int, set]:
    states: Dict[int, set] = {}
    for item in data or []:
        text = (item[0] if isinstance(item, tuple) else item)
        if not isinstance(text, bytes):
            continue
        line = text.decode("utf-8", errors="replace")
        uid_match, flags_match = re.search(r"UID (\d+)", line), re.search(r"FLAGS \(([^)]*)\)", line)
        if uid_match and flags_match:
            states[int(uid_match.group(1))] = set(flags_match.group(1).split())
    return states

async def _do_mark(account: Optional[str], uids: list, folder: str, action: str) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct) as conn:
            opened = _open_folder(conn, folder, readonly=False)
            found, not_found = _existing_uids(conn, uids)
            base = {"account": acct["address"], "folder": opened["name"], "mark": action}
            marked, failed, error = [], [], None
            if found:
                sign, flag = _FLAG_CHANGES[action]
                uid_set = _uid_set(found)
                status, data = _uid_command(conn, "STORE", uid_set, f"{sign}FLAGS.SILENT", f"({flag})")
                if status == "OK":
                    marked = found
                else:
                    error = _server_text(data) or "STORE failed"
                    status, data = _uid_command(conn, "FETCH", uid_set, "(FLAGS)")
                    states = _flags_by_uid(data) if status == "OK" else {}
                    marked = [u for u in found if u in states and ((flag.lower() in {f.lower() for f in states[u]}) == (sign == "+"))]
                    failed = [u for u in found if u not in marked]
            return _bulk_result(base, len(uids), "marked", marked, not_found, failed, None, error)
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error marking emails: {e}"

async def _do_save_attachment(account: Optional[str], uid: str, folder: str, attachment_index: int, save_path: str, overwrite: bool) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct) as conn:
            _open_folder(conn, folder)
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            msg = email.message_from_bytes(raw)
            for idx, part in enumerate(msg.walk()):
                if idx == attachment_index:
                    if not _is_attachment_part(part):
                        return f"Error: Part at index {idx} is not an attachment."
                    filename = _decode_header_value(part.get_filename()) or "(unnamed)"
                    payload = part.get_payload(decode=True) or b""
                    save_dir = os.path.dirname(save_path)
                    if save_dir and not os.path.isdir(save_dir):
                        os.makedirs(save_dir, exist_ok=True)
                    existed_before = os.path.exists(save_path)
                    if existed_before and not overwrite:
                        return f"Error: File already exists at '{save_path}'. Re-run with overwrite=true to replace."
                    with open(save_path, "wb") as f:
                        f.write(payload)
                    return json.dumps({"saved": save_path, "filename": filename, "content_type": part.get_content_type(), "size_bytes": len(payload), "overwritten": existed_before}, indent=2)
            return f"Error: No part found at index {attachment_index}."
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error saving attachment: {e}"

async def _do_get_attachment(account: Optional[str], uid: str, folder: str, attachment_index: int) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct) as conn:
            _open_folder(conn, folder)
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            msg = email.message_from_bytes(raw)
            for idx, part in enumerate(msg.walk()):
                if idx == attachment_index:
                    if not _is_attachment_part(part):
                        return f"Error: Part at index {idx} is not an attachment."
                    filename = _decode_header_value(part.get_filename()) or "(unnamed)"
                    payload = part.get_payload(decode=True) or b""
                    return json.dumps({"filename": filename, "content_type": part.get_content_type(), "size_bytes": len(payload), "content_base64": base64.b64encode(payload).decode("ascii")}, indent=2)
            return f"Error: No part found at index {attachment_index}."
    except FolderError as e:
        return e.to_json()
    except Exception as e:
        return f"Error getting attachment: {e}"

async def _do_prepare_attachments(attachments: str) -> str:
    try:
        attachment_info, total_size = _collect_attachment_metadata(attachments)
        missing = [item["path"] for item in attachment_info if not item["exists"]]
        return json.dumps({"attachments": attachment_info, "count": len(attachment_info), "existing_count": sum(1 for item in attachment_info if item["exists"]), "missing_count": len(missing), "missing": missing, "total_size_bytes": total_size}, indent=2)
    except Exception as e:
        return f"Error preparing attachments: {e}"

# ─── Single dispatcher tool ──────────────────────────────────────────────────

@mcp.tool(name="email")
async def email_dispatcher(action: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Email client (IMAP/SMTP). Call with just action to discover its parameters. Actions: validate_config, list_accounts, list_folders, list_emails, search, read, send, reply, reply_all, forward, move, mark, save_attachment, get_attachment, prepare_attachments."""
    if action not in ACTION_SCHEMAS:
        return _unknown_action(action)
    if params is None:
        return json.dumps(_schema_view(action))
    p, problems = _validate_params(action, params)
    if problems:
        return _validation_error(action, problems)
    if action == "validate_config":
        return await _do_validate_config()
    elif action == "list_accounts":
        return await _do_list_accounts()
    elif action == "list_folders":
        return await _do_list_folders(p.get("account"))
    elif action == "list_emails":
        return await _do_list_emails(p.get("account"), p["folder"], p["limit"])
    elif action == "search":
        return await _do_search(p.get("account"), p["query"], p["folder"], p["limit"])
    elif action == "read":
        return await _do_read(p.get("account"), str(p["uid"]), p["folder"])
    elif action == "send":
        return await _do_send(p)
    elif action == "reply":
        return await _do_reply(p)
    elif action == "reply_all":
        return await _do_reply_all(p)
    elif action == "forward":
        return await _do_forward(p)
    elif action == "move":
        return await _do_move(p.get("account"), p["uid"], p["source_folder"], p["destination_folder"])
    elif action == "mark":
        return await _do_mark(p.get("account"), p["uid"], p["folder"], p["action"])
    elif action == "save_attachment":
        return await _do_save_attachment(p.get("account"), str(p["uid"]), p["folder"], p["attachment_index"], p["save_path"], p["overwrite"])
    elif action == "get_attachment":
        return await _do_get_attachment(p.get("account"), str(p["uid"]), p["folder"], p["attachment_index"])
    else:
        return await _do_prepare_attachments(p["attachments"])

def main():
    mcp.run()

if __name__ == "__main__":
    main()
