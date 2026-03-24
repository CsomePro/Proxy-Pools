from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubscriptionCreate(BaseModel):
    name: str
    url: str
    format: Literal["auto", "base64", "uri", "clash"] = "auto"


class SubscriptionRecord(BaseModel):
    id: str
    name: str
    url: str
    format: Literal["auto", "base64", "uri", "clash"] = "auto"
    enabled: bool = True
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    last_refreshed_at: Optional[str] = None
    last_error: Optional[str] = None


class NodeRecord(BaseModel):
    id: str
    subscription_id: Optional[str] = None
    name: str
    protocol: str
    enabled: bool = True
    outbound: dict[str, Any]
    source: str
    hash: str
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    healthy: Optional[bool] = None
    last_checked_at: Optional[str] = None
    last_latency_ms: Optional[int] = None
    last_error: Optional[str] = None
    failure_count: int = 0
    bound_port: Optional[int] = None
    bound_tag: Optional[str] = None
    selected_at: Optional[str] = None


class AppState(BaseModel):
    subscriptions: list[SubscriptionRecord] = Field(default_factory=list)
    nodes: list[NodeRecord] = Field(default_factory=list)
    rotation_index: int = 0
    updated_at: str = Field(default_factory=utc_now)


class ProxyLease(BaseModel):
    node_id: str
    name: str
    protocol: str
    proxy_url: str
    bound_port: int
    healthy: Optional[bool]
    last_latency_ms: Optional[int]
    last_checked_at: Optional[str]

