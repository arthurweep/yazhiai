from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from database import Database, utcnow_iso
from furnace_client import FurnaceClient
from process_engine import build_summary, baseline_payload, datatemp_metrics, diagnose, iso_to_dt, forming_chain_analysis
from settings import (
    DEVICE_ID,
    POLL_INTERVAL_SECONDS,
    PROCESS_CONTEXT_WINDOW_SECONDS,
    SUMMARY_INTERVAL_SECONDS,
)
from simulation import ProcessSimulator
from zone_knowledge import zone_advisories


def _cmp_iso(a: str | None, b: str | None) -> int:
    da, db = iso_to_dt(a), iso_to_dt(b)
    if da and db:
        return (da > db) - (da < db)
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    return (a > b) - (a < b)


def _recipe_change_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "prod_name": (data.get("loading_status") or {}).get("prod_name"),
        "loading_gap_time_sv": (data.get("loading_status") or {}).get("loading_gap_time_sv"),
        "furnace_sv": {},
        "convection_sv": {},
    }
    for z, zd in (data.get("furnace_temp") or {}).items():
        result["furnace_sv"][z] = {}
        for pos, p in (zd or {}).items():
            if isinstance(p, dict):
                result["furnace_sv"][z][pos] = p.get("sv")

    conv = data.get("convection") or {}
    for kind in ("basic_zones", "lcr_zones"):
        for z, zd in (conv.get(kind) or {}).items():
            zmap = result["convection_sv"].setdefault(z, {})
            for group_name, group in (zd or {}).items():
                if not isinstance(group, list):
                    continue
                zmap[group_name] = [
                    {"pos": item.get("pos"), "sv_big": item.get("sv_big"), "sv_small": item.get("sv_small")}
                    for item in group if isinstance(item, dict)
                ]
    return result


def _flatten_numeric(obj: Any, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten_numeric(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            out.update(_flatten_numeric(v, p))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[prefix] = float(obj)
    return out


@dataclass
class RuntimeState:
    latest_source_raw: dict[str, Any] | None = None   # 原始设备/样例 JSON
    latest_raw: dict[str, Any] | None = None          # 当前有效链路 JSON（可能叠加仿真）
    latest_summary: dict[str, Any] | None = None
    latest_simulation_meta: dict[str, Any] = field(default_factory=dict)
    latest_receive_time: str | None = None
    latest_error: str | None = None
    source_ok: bool = False
    consecutive_failures: int = 0
    poll_count: int = 0
    datatemp_status: str = "UNAVAILABLE"
    datatemp_note: str = "尚未获取测温数据"
    last_processed_ir_scan_time_real: str | None = None
    last_processed_ir_scan_time_sim: str | None = None
    previous_recipe: dict[str, Any] | None = None
    last_summary_write: datetime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class FurnaceRuntime:
    def __init__(self, db: Database, client: FurnaceClient):
        self.db = db
        self.client = client
        self.simulator = ProcessSimulator()
        self.state = RuntimeState(
            last_processed_ir_scan_time_real=db.latest_ir_scan_time(DEVICE_ID, simulated=False),
            last_processed_ir_scan_time_sim=db.latest_ir_scan_time(DEVICE_ID, simulated=True),
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="pressfurnace-collector")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            try:
                await self.poll_once()
            except Exception:
                pass
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.2, POLL_INTERVAL_SECONDS - elapsed))

    async def set_simulation(self, enabled: bool) -> dict[str, Any]:
        before = self.simulator.enabled
        status = self.simulator.set_enabled(enabled)
        if before != enabled:
            async with self.state.lock:
                self.state.datatemp_status = "UNAVAILABLE"
                self.state.datatemp_note = "仿真模式切换，等待下一次快照"
                self.state.previous_recipe = None
            self.db.insert_event(
                "SIMULATION_ENABLED" if enabled else "SIMULATION_DISABLED",
                "全局PV仿真已开启" if enabled else "全局PV仿真已关闭，恢复原始JSON",
                severity="WARN" if enabled else "INFO",
                evidence={"simulated": enabled},
            )
            # 不等待下一个3秒周期，立即刷新一次有效数据。
            try:
                await self.poll_once()
            except Exception:
                pass
        return status

    async def poll_once(self) -> None:
        receive_time = utcnow_iso()
        try:
            source_raw = await self.client.fetch()
            effective_raw, sim_meta = self.simulator.apply(source_raw)
            summary = build_summary(effective_raw, receive_time)
            summary["device"]["requested_device_id"] = self.client.device_id
            summary["simulation"] = sim_meta

            async with self.state.lock:
                was_down = not self.state.source_ok and self.state.consecutive_failures > 0
                self.state.latest_source_raw = source_raw
                self.state.latest_raw = effective_raw
                self.state.latest_summary = summary
                self.state.latest_simulation_meta = sim_meta
                self.state.latest_receive_time = receive_time
                self.state.latest_error = None
                self.state.source_ok = True
                self.state.consecutive_failures = 0
                self.state.poll_count += 1

            if was_down:
                self.db.insert_event(
                    "SOURCE_RECOVERED", "设备实时接口已恢复", summary.get("production", {}).get("prod_name"), "INFO"
                )

            await self._handle_device_mismatch(summary)
            await self._handle_datatemp(effective_raw, summary, receive_time, simulated=self.simulator.enabled)
            await self._handle_recipe_events(effective_raw, summary)
            await self._maybe_store_operation_summary(summary, simulated=self.simulator.enabled)
        except Exception as exc:
            async with self.state.lock:
                self.state.latest_error = f"{type(exc).__name__}: {exc}"
                self.state.source_ok = False
                self.state.consecutive_failures += 1
                fail_count = self.state.consecutive_failures
            if fail_count == 1 or fail_count % 10 == 0:
                self.db.insert_event(
                    "SOURCE_ERROR", f"设备实时接口读取失败（连续 {fail_count} 次）", None, "WARN",
                    {"error": str(exc), "url": self.client.url, "requested_device_id": self.client.device_id},
                )
            raise

    async def _handle_device_mismatch(self, summary: dict[str, Any]) -> None:
        actual = summary.get("device", {}).get("device_id")
        requested = self.client.device_id
        if actual and requested and actual != requested:
            prev_events = self.db.events(limit=10)
            if not any(e.get("event_type") == "DEVICE_ID_MISMATCH" and (e.get("evidence") or {}).get("actual") == actual for e in prev_events):
                self.db.insert_event(
                    "DEVICE_ID_MISMATCH",
                    f"请求 deviceId={requested}，但响应 device_info.device_id={actual}",
                    summary.get("production", {}).get("prod_name"),
                    "WARN",
                    {"requested": requested, "actual": actual},
                )

    async def _handle_datatemp(self, raw: dict[str, Any], summary: dict[str, Any], receive_time: str, simulated: bool) -> None:
        ir = raw.get("ir_scan") or {}
        ir_time = ir.get("ir_scan_time")
        points = ir.get("pt_pv")
        if not ir_time or not isinstance(points, list) or len(points) < 6:
            async with self.state.lock:
                self.state.datatemp_status = "UNAVAILABLE"
                self.state.datatemp_note = "DataTemp 字段缺失或六点数据不完整；其它设备采集继续运行"
            return

        latest = (
            self.state.last_processed_ir_scan_time_sim if simulated
            else self.state.last_processed_ir_scan_time_real
        )
        cmp_result = _cmp_iso(ir_time, latest)
        if latest is None or cmp_result > 0:
            metrics = datatemp_metrics(points)
            if not metrics.get("available"):
                async with self.state.lock:
                    self.state.datatemp_status = "UNAVAILABLE"
                    self.state.datatemp_note = "DataTemp 六点数据格式异常"
                return

            ir_dt = iso_to_dt(ir_time)
            since = None
            if ir_dt:
                since = (ir_dt - timedelta(seconds=PROCESS_CONTEXT_WINDOW_SECONDS)).isoformat().replace("+00:00", "Z")
            context_events = self.db.events(limit=20, since_ts=since) if since else []
            prod_name = summary.get("production", {}).get("prod_name")
            row = {
                "device_id": self.client.device_id,
                "ir_scan_time": ir_time,
                "receive_time": receive_time,
                "prod_name": prod_name,
                "is_simulated": simulated,
                **metrics.get("points", {}),
                "mean": metrics.get("mean"), "range": metrics.get("range"), "std": metrics.get("std"),
                "front_avg": metrics.get("front_avg"), "rear_avg": metrics.get("rear_avg"),
                "left_avg": metrics.get("left_avg"), "center_avg": metrics.get("center_avg"), "right_avg": metrics.get("right_avg"),
                "left_right_delta": metrics.get("left_right_delta"),
                "front_rear_delta": metrics.get("front_rear_delta"),
                "center_edge_delta": metrics.get("center_edge_delta"),
                "raw": {"ir_scan_time": ir_time, "pt_pv": points},
                "context": {
                    "window_seconds": PROCESS_CONTEXT_WINDOW_SECONDS,
                    "simulated": simulated,
                    "note": "前置过程事件参考窗口，不代表单片玻璃完整炉内轨迹或因果关系",
                    "event_ids": [e["id"] for e in context_events],
                },
            }
            inserted = self.db.insert_datatemp(row)
            if inserted:
                if simulated:
                    self.state.last_processed_ir_scan_time_sim = ir_time
                else:
                    self.state.last_processed_ir_scan_time_real = ir_time
                async with self.state.lock:
                    self.state.datatemp_status = "OK"
                    self.state.datatemp_note = "检测到新的六点测温事件" + ("（仿真）" if simulated else "")
            return

        if cmp_result == 0:
            last = self.db.latest_datatemp_raw(self.client.device_id, simulated=simulated)
            if last and last.get("ir_scan_time") == ir_time:
                old_points = ((last.get("raw") or {}).get("pt_pv"))
                if old_points is not None and old_points != points:
                    self.db.insert_event(
                        "DATATEMP_INCONSISTENT",
                        "同一 ir_scan_time 下六点温度内容发生变化，按接口一致性异常处理，不创建新测温事件",
                        summary.get("production", {}).get("prod_name"),
                        "WARN",
                        {"ir_scan_time": ir_time, "old": old_points, "new": points, "simulated": simulated},
                    )
                    async with self.state.lock:
                        self.state.datatemp_status = "INCONSISTENT"
                        self.state.datatemp_note = "同一测温时间戳出现不同六点值"
                    return
            async with self.state.lock:
                self.state.datatemp_status = "WAITING"
                self.state.datatemp_note = "设备快照已更新，DataTemp 仍是最近一次测温结果；不重复入库"
            return

        # 仿真与真实时间线已经独立，因此这里只表示当前链路自身的时间回退。
        self.db.insert_event(
            "DATATEMP_TIME_ROLLBACK", "ir_scan_time 小于该链路最近已处理时间，未作为新测温事件",
            summary.get("production", {}).get("prod_name"), "WARN",
            {"incoming": ir_time, "last_processed": latest, "simulated": simulated},
        )
        async with self.state.lock:
            self.state.datatemp_status = "TIME_ROLLBACK"
            self.state.datatemp_note = "DataTemp 时间戳倒退"

    async def _handle_recipe_events(self, raw: dict[str, Any], summary: dict[str, Any]) -> None:
        current = _recipe_change_snapshot(raw)
        previous = self.state.previous_recipe
        self.state.previous_recipe = current
        if previous is None:
            return

        prod = current.get("prod_name")
        if current.get("prod_name") != previous.get("prod_name"):
            self.db.insert_event(
                "PRODUCT_CHANGE",
                f"生产品种从 {previous.get('prod_name')} 切换为 {current.get('prod_name')}",
                prod, "INFO",
                {"before": previous.get("prod_name"), "after": current.get("prod_name")},
            )

        prev_num = _flatten_numeric({k: v for k, v in previous.items() if k != "prod_name"})
        cur_num = _flatten_numeric({k: v for k, v in current.items() if k != "prod_name"})
        changes = []
        for key, after in cur_num.items():
            before = prev_num.get(key)
            if before is None:
                continue
            threshold = 0.5 if "furnace_sv" in key else 0.1
            if abs(after - before) >= threshold:
                changes.append({"key": key, "before": round(before, 3), "after": round(after, 3), "delta": round(after-before, 3)})
        if changes:
            self.db.insert_event(
                "SETPOINT_CHANGE", f"检测到 {len(changes)} 个设定值发生有效变化", prod, "INFO",
                {"changes": changes[:200], "total": len(changes)},
            )

    async def _maybe_store_operation_summary(self, summary: dict[str, Any], simulated: bool) -> None:
        now = datetime.now(timezone.utc)
        if self.state.last_summary_write and (now - self.state.last_summary_write).total_seconds() < SUMMARY_INTERVAL_SECONDS:
            return
        prod = summary.get("production", {}).get("prod_name")
        baseline = self.db.current_baseline(prod)
        compact = {
            "device": summary.get("device"),
            "production": summary.get("production"),
            "furnace": {
                "avg_pv": summary.get("furnace", {}).get("avg_pv"),
                "max_abs_dev": summary.get("furnace", {}).get("max_abs_dev"),
            },
            "datatemp": summary.get("datatemp"),
            "mold_n01_max_abs_sv_pv": summary.get("mold", {}).get("n01", {}).get("max_abs_sv_pv"),
            "mold_n02_max_abs_sv_pv": summary.get("mold", {}).get("n02", {}).get("max_abs_sv_pv"),
            "torque": summary.get("torque"),
            "anneal": summary.get("anneal"),
            "simulation": summary.get("simulation"),
        }
        self.db.insert_summary(utcnow_iso(), prod, baseline.get("id") if baseline else None, compact, simulated=simulated)
        self.state.last_summary_write = now

    async def dashboard(self) -> dict[str, Any]:
        async with self.state.lock:
            summary = self.state.latest_summary
            raw = self.state.latest_raw
            source_ok = self.state.source_ok
            error = self.state.latest_error
            receive_time = self.state.latest_receive_time
            dt_status = self.state.datatemp_status
            dt_note = self.state.datatemp_note
            poll_count = self.state.poll_count
            sim_meta = dict(self.state.latest_simulation_meta or self.simulator.status())

        if summary is None:
            return {
                "ready": False,
                "source": {"ok": source_ok, "error": error, "receive_time": receive_time, "poll_count": poll_count},
                "datatemp_health": {"status": dt_status, "note": dt_note},
                "simulation": sim_meta,
            }
        prod = summary.get("production", {}).get("prod_name")
        baseline = self.db.current_baseline(prod)
        diag = diagnose(summary, baseline)
        zone_ai = zone_advisories(summary, baseline)
        forming_chain = forming_chain_analysis(summary, baseline)
        history = self.db.datatemp_history(limit=30, product=prod, simulated=self.simulator.enabled)
        return {
            "ready": True,
            "source": {"ok": source_ok, "error": error, "receive_time": receive_time, "poll_count": poll_count},
            "datatemp_health": {"status": dt_status, "note": dt_note},
            "summary": summary,
            "diagnosis": diag,
            "zone_advisories": zone_ai,
            "forming_chain": forming_chain,
            "baseline": baseline,
            "datatemp_history": list(reversed(history)),
            "raw_available": raw is not None,
            "simulation": sim_meta,
        }

    async def capture_current_baseline(self, note: str = "") -> dict[str, Any]:
        if self.simulator.enabled:
            raise RuntimeError("全局PV仿真开启时禁止发布运行基线，请关闭仿真并确认真实稳定生产后再发布")
        async with self.state.lock:
            if self.state.latest_summary is None or self.state.latest_raw is None:
                raise RuntimeError("尚无实时数据，无法建立基线")
            summary = self.state.latest_summary
            raw = self.state.latest_raw
        product = summary.get("production", {}).get("prod_name")
        if not product:
            raise RuntimeError("当前 JSON 没有 prod_name，无法建立产品基线")
        payload = baseline_payload(summary, raw)
        baseline = self.db.capture_baseline(product, payload, note)
        self.db.insert_event(
            "BASELINE_RELEASED", f"人工发布运行基线 {product}_V{baseline['version']:02d}", product, "INFO",
            {"baseline_id": baseline["id"], "version": baseline["version"], "note": note},
        )
        return baseline

    async def raw_effective(self) -> dict[str, Any] | None:
        async with self.state.lock:
            return self.state.latest_raw

    async def raw_source(self) -> dict[str, Any] | None:
        async with self.state.lock:
            return self.state.latest_source_raw
