from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from .models import AppState, NodeRecord, SubscriptionRecord, utc_now


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> AppState:
        if not self.path.exists():
            return AppState()
        return AppState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def _save_unlocked(self) -> None:
        self._state.updated_at = utc_now()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(self._state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def snapshot(self) -> AppState:
        with self._lock:
            return AppState.model_validate(self._state.model_dump())

    def update(self, mutator: Callable[[AppState], None]) -> AppState:
        with self._lock:
            mutator(self._state)
            self._save_unlocked()
            return AppState.model_validate(self._state.model_dump())

    def replace_subscription(self, record: SubscriptionRecord, nodes: list[NodeRecord]) -> AppState:
        def mutator(state: AppState) -> None:
            state.subscriptions = [s for s in state.subscriptions if s.id != record.id] + [record]
            state.nodes = [n for n in state.nodes if n.subscription_id != record.id] + nodes

        return self.update(mutator)

    def delete_subscription(self, subscription_id: str) -> AppState:
        def mutator(state: AppState) -> None:
            state.subscriptions = [s for s in state.subscriptions if s.id != subscription_id]
            state.nodes = [n for n in state.nodes if n.subscription_id != subscription_id]

        return self.update(mutator)

