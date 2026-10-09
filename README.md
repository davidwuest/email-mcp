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
