from __future__ import annotations

import random
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from settings import (
    SIM_ANNEAL_PV_BOUND,
    SIM_CONVECTION_PV_BOUND,
    SIM_DATATEMP_PV_BOUND_C,
    SIM_FURNACE_PV_BOUND_C,
    SIM_MOLD_PV_BOUND_C,
    SIM_TORQUE_PV_BOUND,
    SIM_VACUUM_PV_BOUND,
    SIM_STAGE_PROFILE_C, MODE,
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ProcessSimulator:
    """在原始 JSON 之上叠加轻微、连续的 PV 波动。

    设计目标：测试整个 V3.4 前后端、事件、趋势、诊断和预警链，而不是只让页面数字抖动。
    - SV / 配方不改。
    - PWR 不改。
    - 只改变 PV 类反馈。
    - DataTemp 在仿真开启时按 loading_gap_time_sv 产生新的测温事件。
    - 关闭后立即恢复原始 JSON，不保留任何仿真偏移到真实链路。
    """

    def __init__(self) -> None:
        self.enabled = False
        self._rng = random.Random()
        self._offsets: dict[str, float] = {}
        self._last_ir_monotonic: float | None = None
        self._sim_ir_time: str | None = None
        self._sim_ir_points: list[float] | None = None
        self._sequence = 0
        self._stage_profile_overlay = False

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": "GLOBAL_PV_OVERLAY" if self.enabled else "OFF",
            "sequence": self._sequence,
            "stage_profile_overlay": self._stage_profile_overlay,
            "note": (
                "原始设备 JSON 作为基准，后台对 PV 叠加轻微随机游走；前端、诊断、DataTemp事件与历史均使用仿真副本。"
                if self.enabled else
                "仿真关闭：后台与前端直接使用设备/样例 JSON 原始值。"
            ),
        }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        enabled = bool(enabled)
        if enabled != self.enabled:
            self.enabled = enabled
            self.reset()
        return self.status()

    def reset(self) -> None:
        self._offsets.clear()
        self._last_ir_monotonic = None
        self._sim_ir_time = None
        self._sim_ir_points = None
        self._sequence = 0
        self._stage_profile_overlay = False

    def _walk(self, key: str, bound: float, step: float | None = None) -> float:
        step = step if step is not None else bound * 0.18
        prev = self._offsets.get(key, 0.0)
        # 平滑随机游走：大部分时候小幅变化，偶尔向零回归，避免长期贴着边界。
        delta = self._rng.gauss(0.0, max(1e-6, step)) - prev * 0.06
        cur = max(-bound, min(bound, prev + delta))
        self._offsets[key] = cur
        return cur

    @staticmethod
    def _add(base: Any, offset: float, digits: int = 2) -> Any:
        if isinstance(base, (int, float)) and not isinstance(base, bool):
            return round(float(base) + offset, digits)
        return base

    def _apply_furnace(self, data: dict[str, Any]) -> None:
        furnace = data.get("furnace_temp") or {}
        # Demo模式的样例JSON多区温度几乎一致，不利于验证分区诊断/停机向量寻优。
        # 仅在全局PV仿真开启且PRESSFURNACE_MODE=demo时，给PV叠加一个明确标识的工艺阶梯形态；
        # 真实模式绝不做这一层形态修正，SV/PWR始终保持源JSON。
        self._stage_profile_overlay = bool(MODE == "demo" and len(SIM_STAGE_PROFILE_C) >= 12)
        for zi in range(1, 13):
            z = f"z{zi:02d}"
            zd = furnace.get(z) or {}
            raw_vals = [float(p.get("pv")) for p in zd.values() if isinstance(p, dict) and isinstance(p.get("pv"), (int, float))]
            raw_mean = sum(raw_vals) / len(raw_vals) if raw_vals else None
            target_zone = float(SIM_STAGE_PROFILE_C[zi-1]) if self._stage_profile_overlay else None
            for pos, p in zd.items():
                if isinstance(p, dict) and isinstance(p.get("pv"), (int, float)):
                    key = f"furnace.{z}.{pos}.pv"
                    base = float(p["pv"])
                    if target_zone is not None and raw_mean is not None:
                        # 保留同一区六点原有相对空间差，再叠加小幅随机游走。
                        base = target_zone + (base - raw_mean)
                    p["pv"] = self._add(base, self._walk(key, SIM_FURNACE_PV_BOUND_C), 2)

    def _apply_convection(self, data: dict[str, Any]) -> None:
        conv = data.get("convection") or {}
        for kind in ("basic_zones", "lcr_zones"):
            for z, zd in (conv.get(kind) or {}).items():
                for group_name, arr in (zd or {}).items():
                    if not isinstance(arr, list):
                        continue
                    for idx, item in enumerate(arr):
                        if isinstance(item, dict) and isinstance(item.get("pv"), (int, float)):
                            key = f"conv.{kind}.{z}.{group_name}.{idx}.pv"
                            item["pv"] = self._add(item["pv"], self._walk(key, SIM_CONVECTION_PV_BOUND), 2)

    def _apply_mold(self, data: dict[str, Any]) -> None:
        mold = data.get("mold_preheat") or {}
        for station, sd in mold.items():
            if not isinstance(sd, dict):
                continue
            for family, fd in sd.items():
                if not isinstance(fd, dict):
                    continue
                for pos, p in fd.items():
                    if isinstance(p, dict) and isinstance(p.get("pv"), (int, float)):
                        key = f"mold.{station}.{family}.{pos}.pv"
                        p["pv"] = self._add(p["pv"], self._walk(key, SIM_MOLD_PV_BOUND_C), 2)

    def _apply_simple_pv(self, data: dict[str, Any]) -> None:
        torque = data.get("pressing_torque") or {}
        for key, value in list(torque.items()):
            if key.endswith("_pv") and isinstance(value, (int, float)):
                torque[key] = self._add(value, self._walk(f"torque.{key}", SIM_TORQUE_PV_BOUND), 3)

        vacuum = data.get("vacuum_pressure") or {}
        for key, value in list(vacuum.items()):
            if key.endswith("_pv") and isinstance(value, (int, float)):
                vacuum[key] = self._add(value, self._walk(f"vacuum.{key}", SIM_VACUUM_PV_BOUND), 2)

        anneal = data.get("annealing_cooling") or {}
        for key, p in anneal.items():
            if isinstance(p, dict) and isinstance(p.get("pv"), (int, float)):
                p["pv"] = self._add(p["pv"], self._walk(f"anneal.{key}.pv", SIM_ANNEAL_PV_BOUND), 2)

    def _apply_datatemp(self, data: dict[str, Any]) -> None:
        ir = data.setdefault("ir_scan", {})
        base_points = ir.get("pt_pv")
        if not isinstance(base_points, list) or len(base_points) < 6:
            return

        gap = (data.get("loading_status") or {}).get("loading_gap_time_sv")
        interval = float(gap) if isinstance(gap, (int, float)) and float(gap) > 0 else 12.5
        now_mono = time.monotonic()
        new_event = (
            self._last_ir_monotonic is None
            or now_mono - self._last_ir_monotonic >= max(1.0, interval)
        )

        if new_event:
            self._last_ir_monotonic = now_mono
            self._sim_ir_time = utcnow_iso()
            sim_points: list[float] = []
            for idx, value in enumerate(base_points[:6]):
                if isinstance(value, (int, float)):
                    off = self._walk(f"datatemp.p{idx+1}", SIM_DATATEMP_PV_BOUND_C, step=0.28)
                    sim_points.append(round(float(value) + off, 2))
                else:
                    sim_points.append(value)
            self._sim_ir_points = sim_points

        if self._sim_ir_time and self._sim_ir_points:
            ir["ir_scan_time"] = self._sim_ir_time
            ir["pt_pv"] = list(self._sim_ir_points)

    def apply(self, source_data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.enabled:
            return deepcopy(source_data), self.status()

        data = deepcopy(source_data)
        self._sequence += 1
        original_ts = (data.get("device_info") or {}).get("timestamp")
        # 仿真链代表“当前测试工况”，有效快照时间使用当前时间；原时间保留在 simulation_meta 中。
        data.setdefault("device_info", {})["timestamp"] = utcnow_iso()
        self._apply_furnace(data)
        self._apply_convection(data)
        self._apply_mold(data)
        self._apply_simple_pv(data)
        self._apply_datatemp(data)
        meta = self.status()
        meta.update({
            "original_source_timestamp": original_ts,
            "effective_source_timestamp": data.get("device_info", {}).get("timestamp"),
        })
        return data, meta
