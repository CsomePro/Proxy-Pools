from __future__ import annotations

import json
import ipaddress
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .config import Settings
from .models import NodeRecord

logger = logging.getLogger(__name__)


class SingboxEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self.runtime_dir = self.settings.data_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def _config_path(self, node: NodeRecord) -> Path:
        return self.runtime_dir / f"{node.id}.json"

    def render_config(self, node: NodeRecord) -> dict:
        inbound_tag = f"in-{node.id[:12]}"
        outbound_tag = node.bound_tag or f"out-{node.id[:12]}"
        outbound = {"tag": outbound_tag, **node.outbound}
        server = outbound.get("server")
        if isinstance(server, str):
            try:
                ipaddress.ip_address(server)
            except ValueError:
                outbound.setdefault(
                    "domain_resolver",
                    {
                        "server": "local",
                        "strategy": "prefer_ipv4",
                    },
                )
        return {
            "log": {"level": self.settings.log_level},
            "dns": {
                "servers": [
                    {
                        "type": "local",
                        "tag": "local",
                    }
                ]
            },
            "inbounds": [
                {
                    "type": "socks",
                    "tag": inbound_tag,
                    "listen": self.settings.bind_host,
                    "listen_port": node.bound_port,
                }
            ],
            "outbounds": [
                {"type": "direct", "tag": "direct"},
                outbound,
            ],
            "route": {
                "final": "direct",
                "rules": [
                    {
                        "inbound": [inbound_tag],
                        "action": "route",
                        "outbound": outbound_tag,
                    }
                ],
            },
        }

    def _log_path(self, node_id: str) -> Path:
        return self.runtime_dir / f"{node_id}.log"

    def _tracked_runtime_processes(self) -> list[tuple[int, str]]:
        matches: list[tuple[int, str]] = []
        runtime_prefix = str(self.runtime_dir)
        for proc_dir in Path("/proc").iterdir():
            if not proc_dir.name.isdigit():
                continue
            try:
                cmdline = (proc_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            except Exception:
                continue
            if "sing-box run -c " not in cmdline:
                continue
            if runtime_prefix not in cmdline:
                continue
            matches.append((int(proc_dir.name), cmdline.strip()))
        return matches

    def cleanup_orphans(self) -> None:
        active_pids = {proc.pid for proc in self._processes.values() if proc.poll() is None}
        for pid, cmdline in self._tracked_runtime_processes():
            if pid in active_pids:
                continue
            logger.warning("killing orphan sing-box runner pid=%s cmd=%s", pid, cmdline)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        time.sleep(0.2)
        for pid, cmdline in self._tracked_runtime_processes():
            if pid in active_pids:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue

    def ensure_ports(self, nodes: list[NodeRecord]) -> list[NodeRecord]:
        used_ports = {node.bound_port for node in nodes if node.bound_port}
        next_port = self.settings.base_port
        updated: list[NodeRecord] = []
        for node in sorted(nodes, key=lambda item: item.name):
            item = node.model_copy(deep=True)
            if item.bound_port is None:
                while next_port in used_ports:
                    next_port += 1
                item.bound_port = next_port
                item.bound_tag = f"out-{item.id[:12]}"
                used_ports.add(next_port)
                next_port += 1
            elif not item.bound_tag:
                item.bound_tag = f"out-{item.id[:12]}"
            updated.append(item)
        return updated

    def start_node(self, node: NodeRecord) -> None:
        with self._lock:
            process = self._processes.get(node.id)
            if process and process.poll() is None:
                return
            self.cleanup_orphans()
            binary = Path(self.settings.singbox_binary)
            if not binary.exists():
                raise FileNotFoundError(f"sing-box binary not found at {binary}")
            config_path = self._config_path(node)
            config_path.write_text(json.dumps(self.render_config(node), indent=2), encoding="utf-8")
            cmd = [str(binary), "run", "-c", str(config_path)]
            logger.info("starting node %s on port %s", node.name, node.bound_port)
            log_handle = self._log_path(node.id).open("w", encoding="utf-8")
            process = subprocess.Popen(
                cmd,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._processes[node.id] = process
            time.sleep(0.3)
            if process.poll() is not None:
                log_handle.close()
                message = self._log_path(node.id).read_text(encoding="utf-8", errors="ignore").strip()
                self._processes.pop(node.id, None)
                raise RuntimeError(message or f"sing-box exited immediately for node {node.name}")

    def stop_node(self, node_id: str) -> None:
        with self._lock:
            process = self._processes.pop(node_id, None)
            if not process:
                return
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()

    def is_running(self, node_id: str) -> bool:
        with self._lock:
            process = self._processes.get(node_id)
            return bool(process and process.poll() is None)

    def stop_all(self) -> None:
        with self._lock:
            node_ids = list(self._processes.keys())
        for node_id in node_ids:
            self.stop_node(node_id)
        self.cleanup_orphans()

    def status(self) -> dict:
        with self._lock:
            running = []
            for node_id, process in list(self._processes.items()):
                if process.poll() is None:
                    running.append(node_id)
                else:
                    self._processes.pop(node_id, None)
            return {
                "binary": self.settings.singbox_binary,
                "runtime_dir": str(self.runtime_dir),
                "running_nodes": len(running),
            }
