# email-mcp

MCP server exposing email tools over streamable HTTP, packaged as a Docker image.

## Origin

[simple_email_mcp.py](simple_email_mcp.py) is vendored from [simple-email-mcp](https://github.com/mexican75/simple-email-mcp) 2.2.0 by Ramon Ramirez (MIT, see [LICENSE-simple-email-mcp](LICENSE-simple-email-mcp)) and developed further in this repo. It is no longer installed from PyPI, so upstream releases are not picked up automatically.

## Image

Built and published automatically by [.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml) on every push to `main`:

```
ghcr.io/davidwuest/email-mcp:latest
```

## Configuration

The server reads mail accounts from the JSON file at `ACCOUNTS_FILE`:

```json
{
  "send_code": "optional code required to send mail",
  "accounts": [
    {
      "name": "personal",
      "address": "me@example.com",
      "password": "your-app-password",
      "provider": "gmail"
    }
  ]
}
```

`provider` fills in IMAP/SMTP hosts for `gmail`, `outlook`, `purelymail` and `domainfactory`; for any other server set `imap_host`, `imap_port`, `smtp_host`, `smtp_port` and `smtp_security` (`ssl` or `starttls`) instead. Optional per account: `send_as`, `display_name`, `description`.

| Env var             | Default                                      | Description                                                        |
|---------------------|----------------------------------------------|--------------------------------------------------------------------|
| `ACCOUNTS_FILE`     | `accounts.json` next to `simple_email_mcp.py` | Path to the accounts config                                        |
| `MCP_TRANSPORT`     | `streamable-http`                            | MCP transport mode                                                 |
| `MCP_HOST`          | `0.0.0.0`                                    | Bind host                                                          |
| `MCP_PORT`          | `8000`                                       | Bind port                                                          |
| `MCP_ALLOWED_HOSTS` | (unset: only `localhost` / `127.0.0.1`)      | Comma-separated hostnames accepted in the `Host` header (`host:*` allows any port); replaces the localhost default |

In the container, mount the file and point `ACCOUNTS_FILE` at it:

```sh
docker run -p 8000:8000 \
  -v ./accounts.json:/etc/email-mcp/accounts.json:ro \
  -e ACCOUNTS_FILE=/etc/email-mcp/accounts.json \
  ghcr.io/davidwuest/email-mcp:latest
```

The server has no authentication of its own yet: when exposing it beyond localhost, put it behind something that authenticates requests.

## Actions

One tool, `email`, takes `action` and `params`. Call it with just `action` to get that action's parameters (type, required, default). Parameters are validated before anything happens: unknown names, wrong types and out-of-range values are rejected with every problem listed and, where possible, a suggestion ("did you mean 'destination_folder'?"). Nothing is connected, read or sent while the call is invalid.

- **Folders:** any folder parameter takes the name shown by `list_folders` (`name`) or its `raw_name`; no manual quoting. Names are compared Unicode-normalised. An unknown folder returns similar names; folders are never created, renamed or deleted.
- **reply / reply_all:** optional `cc` and `bcc` are added to the automatically determined recipients (duplicates and the sending account's own addresses are removed and listed under `dropped`). `bcc` never appears in a header.
- **Reply-To:** `send`, `reply` and `reply_all` take an optional `reply_to` address list for the `Reply-To` header. (`reply_to_uid`/`reply_to_folder` of `send` only thread the mail.) `forward` takes none of `cc`, `bcc`, `reply_to`.
- **move / mark:** `uid` is one UID or a list (up to 500). UIDs that do not exist do not stop the others. The answer has `status` (`ok`, `partial`, `failed`) and the lists `moved` or `marked`, `not_found`, `failed` (`move` also `duplicated`), plus `error` when something failed.
- **move** uses `UID MOVE` when the server offers `MOVE` or `IMAP4rev2`, otherwise `UID COPY` + `UID STORE \Deleted` + `UID EXPUNGE` of exactly those UIDs (`UIDPLUS`). Without either it refuses and changes nothing, because a plain `EXPUNGE` would delete every message marked `\Deleted` in the folder.

## Tests

Tests run on Python 3.12, like the image:

```sh
uv venv --python 3.12 .venv && uv pip install --python .venv -r requirements-dev.txt
.venv/bin/python -m pytest -q                    # unit tests, no network
.venv/bin/python -m pytest -q -m integration     # needs Docker: throwaway Dovecot servers and a local SMTP sink
```

The integration tests never touch a real mailbox; they create their own test certificates at run time.

