from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

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
        self._maintenance_task: asyncio.Task | None = None
        self._healthcheck_task: asyncio.Task | None = None
        self._healthcheck_lock = asyncio.Lock()
        self._healthcheck_running = False
        self._last_healthcheck_started_at: str | None = None
        self._last_healthcheck_finished_at: str | None = None

    def _eligible_nodes(self, state: AppState) -> list[NodeRecord]:
        return [node for node in state.nodes if node.enabled and node.pool_enabled and node.bound_port]

    def _running_nodes(self, state: AppState) -> list[NodeRecord]:
        return [node for node in state.nodes if node.runtime_state == "running" and node.bound_port]

    def _proxy_url(self, node: NodeRecord, host: str | None = None) -> str:
        return f"socks5://{host or settings.proxy_public_host}:{node.bound_port}"

    def _candidate_order(self, state: AppState, nodes: list[NodeRecord], strategy: str) -> list[NodeRecord]:
        if strategy == "round_robin":
            ordered = sorted(nodes, key=lambda item: item.name)
            if not ordered:
                return ordered
            start = state.rotation_index % len(ordered)
            return ordered[start:] + ordered[:start]
        shuffled = list(nodes)
        random.shuffle(shuffled)
        return shuffled

    def _assign_ports(self, reset_runtime: bool = False) -> AppState:
        snapshot = self.store.snapshot()
        assigned = self.engine.ensure_ports(snapshot.nodes)
        assigned_by_id = {node.id: node for node in assigned}

        def mutator(state: AppState) -> None:
            for node in state.nodes:
                assigned_node = assigned_by_id[node.id]
                node.bound_port = assigned_node.bound_port
                node.bound_tag = assigned_node.bound_tag
                if reset_runtime:
                    node.runtime_state = "stopped"
                    node.lease_expires_at = None

        return self.store.update(mutator)

    async def startup(self) -> None:
        self.engine.stop_all()
        self._assign_ports(reset_runtime=True)
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def shutdown(self) -> None:
        if self._maintenance_task:
            self._maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance_task
        self.engine.stop_all()

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                self._expire_old_leases()
                await self._maybe_run_periodic_healthcheck()
            except Exception:
                logger.exception("maintenance loop failed")
            await asyncio.sleep(settings.cleanup_interval_secs)

    async def _maybe_run_periodic_healthcheck(self) -> None:
        finished_at = _iso_to_dt(self._last_healthcheck_finished_at)
        now = datetime.now(timezone.utc)
        if finished_at and (now - finished_at).total_seconds() < settings.healthcheck_interval_secs:
            return
        if self._healthcheck_running:
            return
        self.start_healthcheck()

    def _expire_old_leases(self) -> AppState:
        now = datetime.now(timezone.utc)
        snapshot = self.store.snapshot()
        expired = [
            node for node in snapshot.nodes
            if node.runtime_state == "running"
            and node.lease_expires_at
            and (_iso_to_dt(node.lease_expires_at) or now) <= now
        ]
        for node in expired:
            self.engine.stop_node(node.id)

        def mutator(state: AppState) -> None:
            for node in state.nodes:
                if node.id in {item.id for item in expired}:
                    node.runtime_state = "stopped"
                    node.lease_expires_at = None

        if expired:
            return self.store.update(mutator)
        return snapshot

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
            for node in [n for n in snapshot.nodes if n.subscription_id == refreshed.id and n.runtime_state == "running"]:
                self.engine.stop_node(node.id)

            nodes = await self.fetch_subscription(refreshed)
            previous_by_id = {node.id: node for node in snapshot.nodes if node.subscription_id == refreshed.id}
            merged_nodes: list[NodeRecord] = []
            for node in nodes:
                previous = previous_by_id.get(node.id)
                if previous:
                    node.pool_enabled = previous.pool_enabled
                    node.enabled = previous.enabled
                    node.healthy = previous.healthy
                    node.last_checked_at = previous.last_checked_at
                    node.last_latency_ms = previous.last_latency_ms
                    node.last_error = previous.last_error
                    node.failure_count = previous.failure_count
                    node.selected_at = previous.selected_at
                    node.bound_port = previous.bound_port
                    node.bound_tag = previous.bound_tag
                node.runtime_state = "stopped"
                node.lease_expires_at = None
                merged_nodes.append(node)
            refreshed.last_refreshed_at = utc_now()
            refreshed.last_error = None
            self.store.replace_subscription(refreshed, merged_nodes)
            self._assign_ports(reset_runtime=False)
        except Exception:
            refreshed.last_error = "refresh failed"
            self.store.replace_subscription(refreshed, [n for n in snapshot.nodes if n.subscription_id == refreshed.id])
            raise
        return refreshed

    def delete_subscription(self, subscription_id: str) -> None:
        snapshot = self.store.snapshot()
        for node in [n for n in snapshot.nodes if n.subscription_id == subscription_id]:
            self.engine.stop_node(node.id)
        self.store.delete_subscription(subscription_id)

    def list_subscriptions(self) -> list[SubscriptionRecord]:
        return self.store.snapshot().subscriptions

    def list_nodes(self) -> list[NodeRecord]:
        return sorted(self.store.snapshot().nodes, key=lambda item: (item.name, item.id))

    def update_node(self, node_id: str, *, pool_enabled: bool | None = None, enabled: bool | None = None) -> NodeRecord:
        updated_node: NodeRecord | None = None

        def mutator(state: AppState) -> None:
            nonlocal updated_node
            for node in state.nodes:
                if node.id != node_id:
                    continue
                if pool_enabled is not None:
                    node.pool_enabled = pool_enabled
                if enabled is not None:
                    node.enabled = enabled
                if (pool_enabled is False or enabled is False) and node.runtime_state == "running":
                    node.runtime_state = "stopped"
                    node.lease_expires_at = None
                node.updated_at = utc_now()
                updated_node = node.model_copy(deep=True)
                break
            if updated_node is None:
                raise KeyError(f"node {node_id} not found")

        self.store.update(mutator)
        if updated_node and (pool_enabled is False or enabled is False):
            self.engine.stop_node(node_id)
        assert updated_node is not None
        return updated_node

    def update_nodes(
        self,
        node_ids: list[str],
        *,
        pool_enabled: bool | None = None,
        enabled: bool | None = None,
    ) -> list[NodeRecord]:
        requested_ids = set(node_ids)
        if not requested_ids:
            return []

        updated_nodes: list[NodeRecord] = []
        stopped_ids: list[str] = []

        def mutator(state: AppState) -> None:
            existing_ids = {node.id for node in state.nodes}
            missing_ids = requested_ids - existing_ids
            if missing_ids:
                raise KeyError(f"nodes not found: {', '.join(sorted(missing_ids))}")

            for node in state.nodes:
                if node.id not in requested_ids:
                    continue
                if pool_enabled is not None:
                    node.pool_enabled = pool_enabled
                if enabled is not None:
                    node.enabled = enabled
                if (pool_enabled is False or enabled is False) and node.runtime_state == "running":
                    node.runtime_state = "stopped"
                    node.lease_expires_at = None
                    stopped_ids.append(node.id)
                node.updated_at = utc_now()
                updated_nodes.append(node.model_copy(deep=True))

        self.store.update(mutator)
        for node_id in stopped_ids:
            self.engine.stop_node(node_id)
        return updated_nodes

    def status(self) -> dict:
        state = self.store.snapshot()
        eligible_nodes = self._eligible_nodes(state)
        return {
            "subscriptions": len(state.subscriptions),
            "nodes": len(state.nodes),
            "pool_enabled_nodes": len([n for n in state.nodes if n.pool_enabled and n.enabled]),
            "assigned_nodes": len([n for n in state.nodes if n.bound_port]),
            "running_nodes": len(self._running_nodes(state)),
            "healthy_running_nodes": len([n for n in self._running_nodes(state) if n.healthy is True]),
            "engine": self.engine.status(),
            "strategy": settings.strategy,
            "base_port": settings.base_port,
            "lease_ttl_secs": settings.lease_ttl_secs,
            "healthcheck_running": self._healthcheck_running,
            "last_healthcheck_started_at": self._last_healthcheck_started_at,
            "last_healthcheck_finished_at": self._last_healthcheck_finished_at,
            "healthcheck_total": len(eligible_nodes),
            "healthcheck_waiting": len([n for n in eligible_nodes if n.healthcheck_state == "waiting"]),
            "healthcheck_in_progress": len([n for n in eligible_nodes if n.healthcheck_state == "running"]),
            "healthcheck_passed": len([n for n in eligible_nodes if n.healthcheck_state == "passed"]),
            "healthcheck_failed": len([n for n in eligible_nodes if n.healthcheck_state == "failed"]),
        }

    async def check_node(self, node: NodeRecord) -> tuple[bool, int | None, str | None]:
        if not node.bound_port:
            return False, None, "node has no assigned local port"
        temp_started = False
        try:
            if not self.engine.is_running(node.id):
                self.engine.start_node(node)
                temp_started = True
                await asyncio.sleep(1)
        except Exception as exc:
            return False, None, f"runner start failed: {exc}"

        proxy_url = self._proxy_url(node, host="127.0.0.1")
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
        finally:
            if temp_started:
                self.engine.stop_node(node.id)

    async def check_pool_nodes(self) -> AppState:
        async with self._healthcheck_lock:
            self._healthcheck_running = True
            self._last_healthcheck_started_at = utc_now()
            try:
                snapshot = self.store.snapshot()
                eligible = self._eligible_nodes(snapshot)

                def mark_waiting(state: AppState) -> None:
                    eligible_ids = {node.id for node in eligible}
                    for node in state.nodes:
                        if node.id in eligible_ids:
                            node.healthcheck_state = "waiting"

                self.store.update(mark_waiting)

                results: dict[str, tuple[bool, int | None, str | None]] = {}
                semaphore = asyncio.Semaphore(max(1, settings.healthcheck_concurrency))

                async def worker(node: NodeRecord) -> tuple[str, tuple[bool, int | None, str | None]]:
                    async with semaphore:
                        def mark_running(state: AppState) -> None:
                            for item in state.nodes:
                                if item.id == node.id:
                                    item.healthcheck_state = "running"
                                    break

                        self.store.update(mark_running)
                        return node.id, await self.check_node(node)

                tasks = [asyncio.create_task(worker(node)) for node in eligible]
                for task in asyncio.as_completed(tasks):
                    node_id, result = await task
                    results[node_id] = result

                    def mark_result(state: AppState) -> None:
                        for item in state.nodes:
                            if item.id != node_id:
                                continue
                            ok, latency, error = result
                            item.healthy = ok
                            item.last_checked_at = utc_now()
                            item.last_latency_ms = latency
                            item.last_error = error
                            item.failure_count = 0 if ok else item.failure_count + 1
                            item.healthcheck_state = "passed" if ok else "failed"
                            break

                    self.store.update(mark_result)

                updated = self.store.snapshot()
                self._last_healthcheck_finished_at = utc_now()
                return updated
            finally:
                self._healthcheck_running = False

    def start_healthcheck(self) -> bool:
        if self._healthcheck_running:
            return False
        if self._healthcheck_task and not self._healthcheck_task.done():
            return False
        self._healthcheck_task = asyncio.create_task(self.check_pool_nodes())
        return True

    def _is_health_fresh(self, node: NodeRecord) -> bool:
        checked_at = _iso_to_dt(node.last_checked_at)
        if checked_at is None:
            return False
        age = datetime.now(timezone.utc) - checked_at
        return age.total_seconds() <= settings.healthcheck_interval_secs * 2

    def _set_running_lease(self, node: NodeRecord, strategy: str) -> NodeRecord:
        expiry = datetime.now(timezone.utc) + timedelta(seconds=settings.lease_ttl_secs)
        updated_node: NodeRecord | None = None

        def mutator(state: AppState) -> None:
            nonlocal updated_node
            if strategy == "round_robin":
                state.rotation_index += 1
            for item in state.nodes:
                if item.id != node.id:
                    continue
                item.runtime_state = "running"
                item.selected_at = utc_now()
                item.last_allocated_at = utc_now()
                item.lease_expires_at = expiry.isoformat()
                updated_node = item.model_copy(deep=True)
                break

        self.store.update(mutator)
        assert updated_node is not None
        return updated_node

    async def next_proxy(self, strategy: str | None = None, require_healthy: bool = True) -> ProxyLease:
        strategy = (strategy or settings.strategy).lower()
        state = self.store.snapshot()
        candidates = self._eligible_nodes(state)
        if not candidates:
            raise RuntimeError("no pool-enabled proxy nodes available")

        if require_healthy:
            healthy = [node for node in candidates if node.healthy is True and self._is_health_fresh(node)]
            if not healthy:
                raise RuntimeError("no cached healthy proxy nodes available; run health check first")
            candidates = healthy

        ordered = self._candidate_order(state, candidates, strategy)
        chosen = ordered[0]

        self.engine.start_node(chosen)
        await asyncio.sleep(0.5)
        chosen = self._set_running_lease(chosen, strategy)

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
