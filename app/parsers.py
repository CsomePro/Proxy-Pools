from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
import uuid
from typing import Any, Iterable

import yaml

from .models import NodeRecord


SUPPORTED_SCHEMES = {"vmess", "vless", "trojan", "ss", "socks", "socks5", "http", "https"}


def _pad_base64(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _transport_from_mapping(network: str | None, values: dict[str, Any]) -> dict[str, Any] | None:
    network = (network or "").lower()
    if not network or network == "tcp":
        return None
    if network == "ws":
        transport = {"type": "ws"}
        path = values.get("path") or values.get("ws-path")
        host = values.get("host") or values.get("ws-headers-host")
        if path:
            transport["path"] = path
        if host:
            transport["headers"] = {"Host": host}
        return transport
    if network == "grpc":
        service_name = values.get("serviceName") or values.get("service_name")
        transport = {"type": "grpc"}
        if service_name:
            transport["service_name"] = service_name
        return transport
    if network == "http":
        transport = {"type": "http"}
        path = values.get("path")
        host = _split_csv(values.get("host"))
        if path:
            transport["path"] = path
        if host:
            transport["host"] = host
        return transport
    if network == "httpupgrade":
        transport = {"type": "httpupgrade"}
        if values.get("host"):
            transport["host"] = values["host"]
        if values.get("path"):
            transport["path"] = values["path"]
        return transport
    return None


def _tls_from_mapping(values: dict[str, Any], default_server_name: str | None = None) -> dict[str, Any] | None:
    security = str(values.get("security") or values.get("tls") or "").lower()
    has_reality = bool(values.get("pbk") or values.get("public-key"))
    enabled = security in {"tls", "reality"} or _parse_bool(values.get("tls"), False) or has_reality
    if not enabled:
        return None

    tls: dict[str, Any] = {"enabled": True}
    server_name = values.get("sni") or values.get("servername") or values.get("server_name") or values.get("host") or default_server_name
    if server_name:
        tls["server_name"] = server_name
    if values.get("alpn"):
        tls["alpn"] = _split_csv(values["alpn"])
    if values.get("fp"):
        tls["utls"] = {"enabled": True, "fingerprint": values["fp"]}
    if values.get("allowInsecure") is not None or values.get("skip-cert-verify") is not None:
        tls["insecure"] = _parse_bool(values.get("allowInsecure", values.get("skip-cert-verify")))
    if security == "reality" or has_reality:
        tls["reality"] = {
            "enabled": True,
            "public_key": values.get("pbk") or values.get("public-key"),
            "short_id": values.get("sid") or values.get("short-id", ""),
        }
    return tls


def _node_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record(subscription_id: str | None, name: str, protocol: str, outbound: dict[str, Any], source: str) -> NodeRecord:
    outbound = dict(outbound)
    payload = {"protocol": protocol, "name": name, "outbound": outbound, "source": source}
    node_hash = _node_hash(payload)
    node_id = uuid.uuid5(uuid.NAMESPACE_URL, node_hash).hex
    return NodeRecord(
        id=node_id,
        subscription_id=subscription_id,
        name=name or f"{protocol}-{node_id[:8]}",
        protocol=protocol,
        outbound=outbound,
        source=source,
        hash=node_hash,
    )


def _parse_vmess_uri(uri: str, subscription_id: str | None) -> NodeRecord:
    payload = uri.split("://", 1)[1].split("#", 1)[0]
    decoded = base64.b64decode(_pad_base64(payload)).decode("utf-8")
    data = json.loads(decoded)
    name = urllib.parse.unquote(uri.split("#", 1)[1]) if "#" in uri else data.get("ps") or data.get("add") or "vmess"
    outbound: dict[str, Any] = {
        "type": "vmess",
        "server": data["add"],
        "server_port": int(data["port"]),
        "uuid": data["id"],
        "security": data.get("scy") or "auto",
        "alter_id": int(data.get("aid") or 0),
    }
    tls = _tls_from_mapping(data, default_server_name=data.get("sni") or data.get("host") or data.get("add"))
    if tls:
        outbound["tls"] = tls
    transport = _transport_from_mapping(data.get("net"), data)
    if transport:
        outbound["transport"] = transport
    return _record(subscription_id, name, "vmess", outbound, uri)


def _parse_vless_or_trojan_uri(uri: str, scheme: str, subscription_id: str | None) -> NodeRecord:
    parsed = urllib.parse.urlparse(uri)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    name = urllib.parse.unquote(parsed.fragment or parsed.hostname or scheme)
    outbound: dict[str, Any] = {
        "type": scheme,
        "server": parsed.hostname,
        "server_port": parsed.port or 443,
    }
    if scheme == "vless":
        outbound["uuid"] = urllib.parse.unquote(parsed.username or "")
        if query.get("flow"):
            outbound["flow"] = query["flow"]
    else:
        outbound["password"] = urllib.parse.unquote(parsed.username or "")
    tls = _tls_from_mapping(query, default_server_name=parsed.hostname)
    if tls:
        outbound["tls"] = tls
    transport = _transport_from_mapping(query.get("type"), query)
    if transport:
        outbound["transport"] = transport
    return _record(subscription_id, name, scheme, outbound, uri)


def _parse_ss_uri(uri: str, subscription_id: str | None) -> NodeRecord:
    parsed = urllib.parse.urlparse(uri)
    name = urllib.parse.unquote(parsed.fragment or parsed.hostname or "shadowsocks")
    if parsed.username:
        userinfo = f"{urllib.parse.unquote(parsed.username)}:{urllib.parse.unquote(parsed.password or '')}"
        host = parsed.hostname
        port = parsed.port
    else:
        payload = parsed.netloc
        hostport = ""
        if "@" in payload:
            encoded, hostport = payload.split("@", 1)
            userinfo = base64.b64decode(_pad_base64(encoded)).decode("utf-8")
        else:
            decoded = base64.b64decode(_pad_base64(payload)).decode("utf-8")
            if "@" in decoded:
                userinfo, hostport = decoded.split("@", 1)
            else:
                raise ValueError("unsupported shadowsocks URI format")
        host, port_text = hostport.rsplit(":", 1)
        port = int(port_text)
    method, password = userinfo.split(":", 1)
    outbound = {
        "type": "shadowsocks",
        "server": host,
        "server_port": int(port),
        "method": method,
        "password": password,
    }
    return _record(subscription_id, name, "shadowsocks", outbound, uri)


def _parse_socks_or_http_uri(uri: str, scheme: str, subscription_id: str | None) -> NodeRecord:
    parsed = urllib.parse.urlparse(uri)
    protocol = "socks" if scheme in {"socks", "socks5"} else "http"
    name = urllib.parse.unquote(parsed.fragment or parsed.hostname or protocol)
    outbound: dict[str, Any] = {
        "type": protocol,
        "server": parsed.hostname,
        "server_port": parsed.port or (1080 if protocol == "socks" else 80),
    }
    if parsed.username:
        outbound["username"] = urllib.parse.unquote(parsed.username)
    if parsed.password:
        outbound["password"] = urllib.parse.unquote(parsed.password)
    if scheme == "https":
        outbound["tls"] = {"enabled": True, "server_name": parsed.hostname}
    return _record(subscription_id, name, protocol, outbound, uri)


def parse_proxy_uri(uri: str, subscription_id: str | None = None) -> NodeRecord:
    uri = uri.strip()
    scheme = uri.split("://", 1)[0].lower()
    if scheme == "vmess":
        return _parse_vmess_uri(uri, subscription_id)
    if scheme in {"vless", "trojan"}:
        return _parse_vless_or_trojan_uri(uri, scheme, subscription_id)
    if scheme == "ss":
        return _parse_ss_uri(uri, subscription_id)
    if scheme in {"socks", "socks5", "http", "https"}:
        return _parse_socks_or_http_uri(uri, scheme, subscription_id)
    raise ValueError(f"unsupported scheme: {scheme}")


def parse_uri_list(content: str, subscription_id: str | None = None) -> list[NodeRecord]:
    nodes: list[NodeRecord] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        scheme = line.split("://", 1)[0].lower()
        if scheme not in SUPPORTED_SCHEMES:
            continue
        nodes.append(parse_proxy_uri(line, subscription_id=subscription_id))
    return nodes


def parse_clash_yaml(content: str, subscription_id: str | None = None) -> list[NodeRecord]:
    payload = yaml.safe_load(content) or {}
    proxies = payload.get("proxies") or []
    nodes: list[NodeRecord] = []
    for item in proxies:
        if not isinstance(item, dict):
            continue
        proxy_type = str(item.get("type") or "").lower()
        name = str(item.get("name") or item.get("server") or proxy_type)
        if proxy_type == "vmess":
            outbound: dict[str, Any] = {
                "type": "vmess",
                "server": item["server"],
                "server_port": int(item["port"]),
                "uuid": item["uuid"],
                "security": item.get("cipher") or "auto",
                "alter_id": int(item.get("alterId") or item.get("alter_id") or 0),
            }
            tls = _tls_from_mapping(item, default_server_name=item.get("servername") or item.get("server"))
            if tls:
                outbound["tls"] = tls
            transport = _transport_from_mapping(item.get("network"), item)
            if transport:
                outbound["transport"] = transport
            nodes.append(_record(subscription_id, name, "vmess", outbound, json.dumps(item, sort_keys=True)))
        elif proxy_type == "vless":
            outbound = {
                "type": "vless",
                "server": item["server"],
                "server_port": int(item["port"]),
                "uuid": item["uuid"],
            }
            if item.get("flow"):
                outbound["flow"] = item["flow"]
            tls = _tls_from_mapping(item, default_server_name=item.get("servername") or item.get("server"))
            if tls:
                outbound["tls"] = tls
            transport = _transport_from_mapping(item.get("network"), item)
            if transport:
                outbound["transport"] = transport
            nodes.append(_record(subscription_id, name, "vless", outbound, json.dumps(item, sort_keys=True)))
        elif proxy_type == "trojan":
            outbound = {
                "type": "trojan",
                "server": item["server"],
                "server_port": int(item["port"]),
                "password": item["password"],
            }
            tls = _tls_from_mapping(item, default_server_name=item.get("sni") or item.get("server"))
            if tls:
                outbound["tls"] = tls
            transport = _transport_from_mapping(item.get("network"), item)
            if transport:
                outbound["transport"] = transport
            nodes.append(_record(subscription_id, name, "trojan", outbound, json.dumps(item, sort_keys=True)))
        elif proxy_type == "ss":
            outbound = {
                "type": "shadowsocks",
                "server": item["server"],
                "server_port": int(item["port"]),
                "method": item["cipher"],
                "password": item["password"],
            }
            nodes.append(_record(subscription_id, name, "shadowsocks", outbound, json.dumps(item, sort_keys=True)))
        elif proxy_type in {"socks5", "socks"}:
            outbound = {
                "type": "socks",
                "server": item["server"],
                "server_port": int(item["port"]),
            }
            if item.get("username"):
                outbound["username"] = item["username"]
            if item.get("password"):
                outbound["password"] = item["password"]
            nodes.append(_record(subscription_id, name, "socks", outbound, json.dumps(item, sort_keys=True)))
        elif proxy_type == "http":
            outbound = {
                "type": "http",
                "server": item["server"],
                "server_port": int(item["port"]),
            }
            if item.get("username"):
                outbound["username"] = item["username"]
            if item.get("password"):
                outbound["password"] = item["password"]
            if _parse_bool(item.get("tls"), False):
                outbound["tls"] = {"enabled": True, "server_name": item.get("servername") or item.get("server")}
            nodes.append(_record(subscription_id, name, "http", outbound, json.dumps(item, sort_keys=True)))
    return nodes


def _try_base64_uri_list(content: str) -> list[str] | None:
    normalized = "".join(content.split())
    try:
        decoded = base64.b64decode(_pad_base64(normalized)).decode("utf-8")
    except Exception:
        return None
    lines = [line.strip() for line in decoded.splitlines() if line.strip()]
    if not lines:
        return None
    if not all("://" in line for line in lines):
        return None
    return lines


def parse_subscription_content(content: str, fmt: str, subscription_id: str | None = None) -> list[NodeRecord]:
    fmt = fmt.lower()
    if fmt == "clash":
        return parse_clash_yaml(content, subscription_id)
    if fmt == "uri":
        return parse_uri_list(content, subscription_id)
    if fmt == "base64":
        decoded = _try_base64_uri_list(content)
        return parse_uri_list("\n".join(decoded or []), subscription_id)

    stripped = content.lstrip()
    if stripped.startswith("proxies:") or "\nproxies:" in content:
        nodes = parse_clash_yaml(content, subscription_id)
        if nodes:
            return nodes

    direct = parse_uri_list(content, subscription_id)
    if direct:
        return direct

    decoded = _try_base64_uri_list(content)
    if decoded:
        return parse_uri_list("\n".join(decoded), subscription_id)

    raise ValueError("unsupported subscription format or no supported nodes found")
