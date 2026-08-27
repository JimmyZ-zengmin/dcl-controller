#!/usr/bin/env python3
"""
MonitorEngine — 引擎监测组件（IDE 内部组件，非独立进程）

设计原则：
- 是一个普通 Python 类，由 IDE server 拥有生命周期
- 通过线程读取 RTT，数据存内存 + CSV
- 提供 get_latest() / get_history() / get_alerts() 供 IDE 调用
- 通过回调把数据推给 WebSocket 客户端
"""

import os
import json
import time
import platform
import subprocess
import threading
import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "compiler" / "monitor_data"

RTT_CB_ADDR = 0x20008800
RTT_SEARCH_SIZE = 0x1000
PYOCD_TARGET = "stm32h723xx"

JITTER_WARN = 500
JITTER_CRITICAL = 2000
FROZEN_SECONDS = 3.0

ALARM_NAMES = {
    0x01: "JITTER_SPIKE",
    0x02: "PERIOD_HIGH",
    0x03: "ROUTES_CHANGED",
    0x04: "ENGINE_STOPPED",
    0x05: "SAMPLES_FROZEN",
    0x06: "IWDG_RESET",
    0x10: "ADC_DMA_ERROR",
    0x11: "GPIO_DMA_ERROR",
    0x12: "ADC_OVERRUN",
    0x20: "P1_MISSED_SAMPLE",
}

log = logging.getLogger("dcl-monitor")


class MonitorEngine:
    def __init__(self,
                 on_status: Optional[Callable[[dict], None]] = None,
                 on_alert: Optional[Callable[[dict], None]] = None):
        self._on_status = on_status
        self._on_alert = on_alert
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self.latest: dict = {}
        self.history: deque = deque(maxlen=36000)
        self._last_samples = None
        self._last_samples_time = 0.0
        self._startup_count = 0

        self._rtt_proc: Optional[subprocess.Popen] = None
        self._csv_file = None
        self._csv_path: Optional[Path] = None

        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── 生命周期 ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("MonitorEngine started")

    def stop(self):
        self._running = False
        self._kill_rtt()
        if self._thread:
            self._thread.join(timeout=5)
        if self._csv_file:
            try:
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
        log.info("MonitorEngine stopped")

    # ── 查询接口 ──

    def get_latest(self) -> dict:
        return self.latest.copy()

    def get_history(self, seconds: float = 60) -> list:
        cutoff = time.time() - seconds
        return [h for h in self.history if h.get("_ts", 0) >= cutoff]

    def get_alerts(self, limit: int = 50) -> list:
        alert_path = DATA_DIR / "alert.jsonl"
        if not alert_path.exists():
            return []
        lines = alert_path.read_text(encoding="utf-8").strip().split("\n")
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    # ── 内部实现 ──

    def _kill_rtt(self):
        if self._rtt_proc:
            try:
                self._rtt_proc.terminate()
                self._rtt_proc.wait(timeout=2)
            except Exception:
                try:
                    self._rtt_proc.kill()
                except Exception:
                    pass
            self._rtt_proc = None

    def _start_rtt(self) -> bool:
        try:
            self._rtt_proc = subprocess.Popen(
                ["py", "-3", "-m", "pyocd", "rtt", "-t", PYOCD_TARGET,
                 "-a", f"0x{RTT_CB_ADDR:X}", "-s", f"0x{RTT_SEARCH_SIZE:X}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            deadline = time.time() + 5
            while time.time() < deadline:
                line = self._rtt_proc.stdout.readline()
                if line and line.strip().startswith("S="):
                    return True
                if self._rtt_proc.poll() is not None:
                    return False
            return False
        except FileNotFoundError:
            return False

    def _read_line(self) -> Optional[str]:
        if not self._rtt_proc or self._rtt_proc.poll() is not None:
            return None
        try:
            line = self._rtt_proc.stdout.readline().strip()
            return line if line.startswith("S=") else None
        except Exception:
            return None

    def _parse(self, line: str) -> Optional[dict]:
        try:
            result = {}
            for p in line.split():
                if p.startswith("S="):
                    result["samples"] = int(p[2:])
                elif p.startswith("P="):
                    _, rng = p.split("=", 1)
                    if ".." in rng:
                        mn, mx = rng.split("..", 1)
                        result["period_min"] = int(mn)
                        result["period_max"] = int(mx)
                elif p.startswith("R="):
                    result["routes"] = int(p[2:])
                elif p.startswith("E="):
                    result["engine_running"] = int(p[2:])
                elif p.startswith("G="):
                    result["gpio_delta"] = int(p[2:])
                elif p.startswith("A="):
                    result["adc_dma_status"] = int(p[2:])
                elif p.startswith("D="):
                    result["gpio_dma_status"] = int(p[2:])
                elif p.startswith("O="):
                    result["adc_overrun_count"] = int(p[2:])
                elif p.startswith("T="):
                    result["p1_transfer_ok"] = int(p[2:])
                elif p.startswith("M="):
                    result["p1_missed_sample"] = int(p[2:])
            return result if "samples" in result else None
        except (ValueError, IndexError):
            return None

    def _open_csv(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self._csv_path = DATA_DIR / f"{today}.csv"
        need_header = not self._csv_path.exists()
        self._csv_file = open(self._csv_path, "a", encoding="utf-8")
        if need_header:
            self._csv_file.write(
                "timestamp,samples,period_min,period_max,jitter,routes,engine_running,"
                "adc_dma_status,gpio_dma_status,adc_overrun_count\n")
            self._csv_file.flush()

    def _write_csv(self, s: dict):
        pmin, pmax = s.get("period_min", 0), s.get("period_max", 0)
        jitter = pmax - pmin if pmax >= pmin else 0
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        self._csv_file.write(
            f"{ts},{s.get('samples',0)},{pmin},{pmax},{jitter},"
            f"{s.get('routes',0)},{s.get('engine_running',0)},"
            f"{s.get('adc_dma_status',0)},{s.get('gpio_dma_status',0)},{s.get('adc_overrun_count',0)},"
            f"{s.get('p1_transfer_ok',0)},{s.get('p1_missed_sample',0)}\n")
        self._csv_file.flush()

    def _write_alert(self, code: int, detail: str, samples: int):
        name = ALARM_NAMES.get(code, f"UNKNOWN_{code:02X}")
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        entry = json.dumps({"ts": ts, "alarm": name, "samples": samples, "detail": detail},
                          ensure_ascii=False)
        with open(DATA_DIR / "alert.jsonl", "a", encoding="utf-8") as f:
            f.write(entry + "\n")

    def _check(self, s: dict, now: float):
        samples = s.get("samples", 0)
        pmin, pmax = s.get("period_min", 0), s.get("period_max", 0)
        jitter = pmax - pmin if pmax >= pmin else 0
        engine = s.get("engine_running", 1)
        adc_dma = s.get("adc_dma_status", 0)
        gpio_dma = s.get("gpio_dma_status", 0)
        overrun = s.get("adc_overrun_count", 0)

        if self._startup_count < 8:
            return

        if engine == 0:
            self._write_alert(0x04, f"engine_running=0 S={samples}", samples)
            if self._on_alert:
                self._on_alert({"code": "ENGINE_STOPPED", "samples": samples})

        if 0 < jitter < 100000:
            if jitter > JITTER_CRITICAL:
                self._write_alert(0x01, f"P={pmin}..{pmax} ({jitter} cyc critical)", samples)
            elif jitter > JITTER_WARN:
                self._write_alert(0x01, f"P={pmin}..{pmax} ({jitter} cyc warn)", samples)

        # DMA 错误检测 (TEIF/DMEIF/FEIF bits)
        if adc_dma & 0x0E:
            self._write_alert(0x10, f"ADC_DMA_ERROR status=0x{adc_dma:02X}", samples)
            if self._on_alert:
                self._on_alert({"code": "ADC_DMA_ERROR", "status": adc_dma})
        if gpio_dma & 0x0E:
            self._write_alert(0x11, f"GPIO_DMA_ERROR status=0x{gpio_dma:02X}", samples)
            if self._on_alert:
                self._on_alert({"code": "GPIO_DMA_ERROR", "status": gpio_dma})
        if overrun > 0:
            self._write_alert(0x12, f"ADC_OVR count={overrun}", samples)
            if self._on_alert:
                self._on_alert({"code": "ADC_OVERRUN", "count": overrun})

        # P1: 采样完整性 — 检测丢失的采样
        missed = s.get("p1_missed_sample", 0)
        if missed > 0:
            self._write_alert(0x20, f"P1 missed samples={missed}", samples)
            if self._on_alert:
                self._on_alert({"code": "P1_MISSED_SAMPLE", "count": missed})

        if self._last_samples is not None and samples == self._last_samples:
            frozen = now - self._last_samples_time
            if frozen >= FROZEN_SECONDS:
                self._write_alert(0x05, f"S={samples} frozen {frozen:.1f}s", samples)
        else:
            self._last_samples_time = now
        self._last_samples = samples

    def _run(self):
        while self._running:
            if not self._start_rtt():
                time.sleep(10)
                continue

            self._startup_count = 0
            self._open_csv()

            while self._running:
                line = self._read_line()
                if line is None:
                    break

                status = self._parse(line)
                if not status:
                    continue

                self._startup_count += 1
                now = time.time()
                status["_ts"] = now
                self.latest = status
                self.history.append(status)

                self._write_csv(status)
                self._check(status, now)

                if self._on_status:
                    self._on_status(status)

            self._kill_rtt()
            if self._csv_file:
                self._csv_file.close()
                self._csv_file = None
