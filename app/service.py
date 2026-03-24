from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from curl_cffi import requests as cffi_requests

from .config import settings
from .engine import SingboxEngine
from .models import AppState, NodeRecord, ProxyLease, SubscriptionCreate, SubscriptionRecord, utc_now
from .parsers import parse_subscription_content
from .store import StateStore

logger = logging.getLogger(__name__)


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ProxyPoolService:
    def __init__(self) -> None:
        self.store = StateStore(settings.state_path)
        self.engine = SingboxEngine(settings)
        self._health_task: asyncio.Task | None = None

    def _bound_nodes(self, state: AppState) -> list[NodeRecord]:
        return [node for node in state.nodes if node.bound_port]

    async def startup(self) -> None:
        self.rebuild_pool()
        self._health_task = asyncio.create_task(self._periodic_healthcheck())

    async def shutdown(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_task
        self.engine.stop()

    async def _periodic_healthcheck(self) -> None:
        while True:
            try:
                await self.check_bound_nodes()
            except Exception:
                logger.exception("periodic healthcheck failed")
            await asyncio.sleep(settings.healthcheck_interval_secs)

    async def fetch_subscription(self, subscription: SubscriptionRecord) -> list[NodeRecord]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(subscription.url)
            response.raise_for_status()
        nodes = parse_subscription_content(response.text, subscription.format, subscription.id)
        deduped: dict[str, NodeRecord] = {}
        for node in nodes:
            deduped[node.hash] = node
        return list(deduped.values())

    async def add_subscription(self, payload: SubscriptionCreate) -> SubscriptionRecord:
        record = SubscriptionRecord(
            id=uuid.uuid4().hex,
            name=payload.name,
            url=payload.url,
            format=payload.format,
        )
        return await self.refresh_subscription(record.id, record)

    async def refresh_subscription(self, subscription_id: str, record: SubscriptionRecord | None = None) -> SubscriptionRecord:
        snapshot = self.store.snapshot()
        current = record or next((item for item in snapshot.subscriptions if item.id == subscription_id), None)
        if not current:
            raise KeyError(f"subscription {subscription_id} not found")

        refreshed = current.model_copy(deep=True)
        refreshed.updated_at = utc_now()
        try:
            nodes = await self.fetch_subscription(refreshed)
            refreshed.last_refreshed_at = utc_now()
            refreshed.last_error = None
            self.store.replace_subscription(refreshed, nodes)
            self.rebuild_pool()
        except Exception as exc:
            refreshed.last_error = str(exc)
            self.store.replace_subscription(refreshed, [n for n in snapshot.nodes if n.subscription_id == refreshed.id])
            raise
        return refreshed

    def delete_subscription(self, subscription_id: str) -> None:
        self.store.delete_subscription(subscription_id)
        self.rebuild_pool()

    def rebuild_pool(self) -> AppState:
        snapshot = self.store.snapshot()
        selected = self.engine.apply(snapshot)
        selected_by_id = {node.id: node for node in selected}

        def mutator(state: AppState) -> None:
            for node in state.nodes:
                bound = selected_by_id.get(node.id)
                if bound:
                    node.bound_port = bound.bound_port
                    node.bound_tag = bound.bound_tag
                else:
                    node.bound_port = None
                    node.bound_tag = None

        return self.store.update(mutator)

    def _proxy_url(self, node: NodeRecord) -> str:
        return f"socks5://{settings.proxy_public_host}:{node.bound_port}"

    def list_subscriptions(self) -> list[SubscriptionRecord]:
        return self.store.snapshot().subscriptions

    def list_nodes(self) -> list[NodeRecord]:
        return self.store.snapshot().nodes

    def status(self) -> dict:
        state = self.store.snapshot()
        return {
            "subscriptions": len(state.subscriptions),
            "nodes": len(state.nodes),
            "bound_nodes": len(self._bound_nodes(state)),
            "healthy_bound_nodes": len([n for n in self._bound_nodes(state) if n.healthy is True]),
            "engine": self.engine.status(),
            "strategy": settings.strategy,
            "base_port": settings.base_port,
            "pool_size": settings.pool_size,
        }

    async def check_node(self, node: NodeRecord) -> tuple[bool, int | None, str | None]:
        if not node.bound_port:
            return False, None, "node is not bound to a local socks5 port"
        proxy_url = self._proxy_url(node)
        proxies = {"http": proxy_url, "https": proxy_url}
        try:
            response = await asyncio.to_thread(
                cffi_requests.get,
                settings.healthcheck_url,
                proxies=proxies,
                timeout=settings.healthcheck_timeout,
                impersonate="chrome110",
            )
            latency = response.elapsed.total_seconds() * 1000 if response.elapsed else None
            if response.status_code in {200, 204}:
                return True, int(latency) if latency is not None else None, None
            return False, int(latency) if latency is not None else None, f"http {response.status_code}"
        except Exception as exc:
            return False, None, str(exc)

    async def check_bound_nodes(self) -> AppState:
        snapshot = self.store.snapshot()
        results: dict[str, tuple[bool, int | None, str | None]] = {}
        for node in self._bound_nodes(snapshot):
            results[node.id] = await self.check_node(node)

        def mutator(state: AppState) -> None:
            for node in state.nodes:
                if node.id not in results:
                    continue
                ok, latency, error = results[node.id]
                node.healthy = ok
                node.last_checked_at = utc_now()
                node.last_latency_ms = latency
                node.last_error = error
                node.failure_count = 0 if ok else node.failure_count + 1

        return self.store.update(mutator)

    async def next_proxy(self, strategy: str | None = None, require_healthy: bool = True) -> ProxyLease:
        strategy = (strategy or settings.strategy).lower()
        state = self.store.snapshot()
        nodes = self._bound_nodes(state)
        if require_healthy:
            healthy = [node for node in nodes if node.healthy is True]
            if healthy:
                nodes = healthy

        if not nodes:
            raise RuntimeError("no bound proxy nodes available")

        stale_limit = datetime.now(timezone.utc).timestamp() - settings.allocation_stale_after_secs
        for node in nodes:
            checked_at = _iso_to_dt(node.last_checked_at)
            if checked_at is None or checked_at.timestamp() < stale_limit:
                ok, latency, error = await self.check_node(node)
                def mutator(state: AppState) -> None:
                    for candidate in state.nodes:
                        if candidate.id == node.id:
                            candidate.healthy = ok
                            candidate.last_checked_at = utc_now()
                            candidate.last_latency_ms = latency
                            candidate.last_error = error
                            candidate.failure_count = 0 if ok else candidate.failure_count + 1
                self.store.update(mutator)
                if ok:
                    node.healthy = True
                    node.last_latency_ms = latency
                    node.last_checked_at = utc_now()

        state = self.store.snapshot()
        nodes = self._bound_nodes(state)
        if require_healthy:
            healthy = [node for node in nodes if node.healthy is True]
            if healthy:
                nodes = healthy

        if not nodes:
            raise RuntimeError("no healthy proxy nodes available")

        if strategy == "round_robin":
            index = state.rotation_index % len(nodes)
            chosen = nodes[index]

            def mutator(state: AppState) -> None:
                state.rotation_index += 1
                for node in state.nodes:
                    if node.id == chosen.id:
                        node.selected_at = utc_now()

            self.store.update(mutator)
        else:
            chosen = random.choice(nodes)

            def mutator(state: AppState) -> None:
                for node in state.nodes:
                    if node.id == chosen.id:
                        node.selected_at = utc_now()

            self.store.update(mutator)

        return ProxyLease(
            node_id=chosen.id,
            name=chosen.name,
            protocol=chosen.protocol,
            proxy_url=self._proxy_url(chosen),
            bound_port=chosen.bound_port or 0,
            healthy=chosen.healthy,
            last_latency_ms=chosen.last_latency_ms,
            last_checked_at=chosen.last_checked_at,
        )
service = ProxyPoolService()
