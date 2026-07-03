"""System metrics collection via psutil."""

from __future__ import annotations

import logging
import time
from typing import Any

import psutil

logger = logging.getLogger(__name__)

_prev_net_time: float = 0.0
_prev_net_sent: int = 0
_prev_net_recv: int = 0


def collect_metrics(sample_interval: float = 1.0) -> dict[str, Any]:
    """Collect CPU, memory, disk, and network metrics."""
    global _prev_net_time, _prev_net_sent, _prev_net_recv

    cpu_percent = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    now = time.time()

    if _prev_net_time > 0:
        elapsed = now - _prev_net_time
        rx_mbps = ((net.bytes_recv - _prev_net_recv) / elapsed) / (1024 * 1024) * 8
        tx_mbps = ((net.bytes_sent - _prev_net_sent) / elapsed) / (1024 * 1024) * 8
    else:
        rx_mbps = 0.0
        tx_mbps = 0.0

    _prev_net_time = now
    _prev_net_sent = net.bytes_sent
    _prev_net_recv = net.bytes_recv

    return {
        "cpu_usage": round(cpu_percent, 1),
        "memory_usage": round(mem.percent, 1),
        "disk_usage": round(disk.percent, 1),
        "running_tasks": 0,  # set externally
        "rx_mbps": round(rx_mbps, 2),
        "tx_mbps": round(tx_mbps, 2),
        "status_json": {},
    }
