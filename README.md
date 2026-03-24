# proxy-pool

`proxy-pool` is a small service for:

- importing common `v2ray/v2raya` subscription formats
- generating a warm local pool of `SOCKS5` ports with `sing-box`
- health checking bound proxies
- returning one healthy proxy URL from `GET /api/proxy/next`
- exposing a lightweight WebUI

## Supported inputs

- Base64-encoded URI list
- Plain URI list
- Clash YAML

## Supported node types

- VMess
- VLESS
- Trojan
- Shadowsocks
- SOCKS5
- HTTP/HTTPS

## Run

```bash
docker build -t proxy-pool .
docker run --rm -p 9080:9080 -v $(pwd)/data:/app/data proxy-pool
```

Then open `http://127.0.0.1:9080`.

For `codex-console`, point dynamic proxy API to:

```text
http://proxy-pool:9080/api/proxy/next
```

and use result field:

```text
proxy_url
```

The API returns:

```json
{
  "success": true,
  "node_id": "....",
  "name": "example",
  "protocol": "vmess",
  "proxy_url": "socks5://proxy-pool:20001",
  "bound_port": 20001,
  "healthy": true,
  "last_latency_ms": 842,
  "last_checked_at": "2026-03-24T00:00:00+00:00"
}
```

## GHCR

This service is intended to be published from its own repository to:

```text
ghcr.io/csomepro/proxy-pools
```

Included GitHub Actions:

- [ci.yml](/home/csome/project/multicodex/proxy-pool/.github/workflows/ci.yml): build and smoke-test on pull requests
- [docker-publish.yml](/home/csome/project/multicodex/proxy-pool/.github/workflows/docker-publish.yml): publish to GHCR on `main` and `v*` tags

Typical tags:

- `latest` on `main`
- `main`
- `sha-<commit>`
- `v0.1.0`
- `0.1`
