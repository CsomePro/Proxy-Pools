from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Iterable

from .config import Settings
from .models import AppState, NodeRecord, utc_now

logger = logging.getLogger(__name__)


class SingboxEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None

    def _select_nodes(self, state: AppState) -> list[NodeRecord]:
        enabled = [node for node in state.nodes if node.enabled]
        enabled.sort(
            key=lambda node: (
                node.healthy is False,
                node.last_latency_ms if node.last_latency_ms is not None else 10**9,
                node.name,
            )
        )
        chosen = enabled[: self.settings.pool_size]
        port = self.settings.base_port
        selected: list[NodeRecord] = []
        for node in chosen:
            updated = node.model_copy(deep=True)
            updated.bound_port = port
            updated.bound_tag = f"out-{updated.id[:12]}"
            selected.append(updated)
            port += 1
        return selected

    def render_config(self, nodes: Iterable[NodeRecord]) -> dict:
        inbounds = []
        outbounds = [{"type": "direct", "tag": "direct"}]
        rules = []
        for node in nodes:
            inbound_tag = f"in-{node.id[:12]}"
            outbounds.append({"tag": node.bound_tag, **node.outbound})
            inbounds.append(
                {
                    "type": "socks",
                    "tag": inbound_tag,
                    "listen": self.settings.bind_host,
                    "listen_port": node.bound_port,
                }
            )
            rules.append(
                {
                    "inbound": [inbound_tag],
                    "action": "route",
                    "outbound": node.bound_tag,
                }
            )
        return {
            "log": {"level": self.settings.log_level},
            "inbounds": inbounds,
            "outbounds": outbounds,
            "route": {"final": "direct", "rules": rules},
        }

    def apply(self, state: AppState) -> list[NodeRecord]:
        selected = self._select_nodes(state)
        config = self.render_config(selected)
        self.settings.singbox_config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self.restart()
        return selected

    def restart(self) -> None:
        with self._lock:
            self.stop()
            binary = Path(self.settings.singbox_binary)
            if not binary.exists():
                logger.warning("sing-box binary not found at %s; skipping engine start", binary)
                return
            cmd = [str(binary), "run", "-c", str(self.settings.singbox_config_path)]
            logger.info("starting sing-box with %s", cmd)
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )

    def stop(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None

    def status(self) -> dict:
        with self._lock:
            return {
                "running": bool(self._process and self._process.poll() is None),
                "config_path": str(self.settings.singbox_config_path),
                "binary": self.settings.singbox_binary,
            }
