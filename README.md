# email-mcp

MCP server exposing email tools (via [simple-email-mcp](https://pypi.org/project/simple-email-mcp/)) over streamable HTTP, packaged for Kubernetes.

## Image

Built and published automatically by [.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml) on every push to `main`:

```
ghcr.io/davidwuest/email-mcp:latest
```

## Configuration

The server reads account credentials from the file at `ACCOUNTS_FILE` (default `/etc/email-mcp/accounts.json`). See [k8s/secret.yaml.sample](k8s/secret.yaml.sample) for the expected format.

| Env var         | Default                          | Description                  |
|-----------------|-----------------------------------|-------------------------------|
| `ACCOUNTS_FILE` | `/etc/email-mcp/accounts.json`   | Path to accounts config       |
| `MCP_TRANSPORT` | `streamable-http`                | MCP transport mode            |
| `MCP_HOST`      | `0.0.0.0`                         | Bind host                     |
| `MCP_PORT`      | `8000`                            | Bind port                     |

## Deploying to Kubernetes

1. Copy `k8s/secret.yaml.sample` to `k8s/secret.yaml`, fill in your real account credentials under `stringData`, and apply it (it's gitignored so it won't be committed).
2. Apply the rest of the manifests:

```sh
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```
