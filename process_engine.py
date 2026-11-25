from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any

from settings import DATATEMP_FORMING_WINDOW_C, MOLD_NEAR_SETPOINT_TOLERANCE_C

TEMP_POSITIONS = ["top_left", "top_mid", "top_right", "btm_left", "btm_mid", "btm_right"]
TEMP_POSITION_LABELS = {
    "top_left": "上左", "top_mid": "上中", "top_right": "上右",
    "btm_left": "下左", "btm_mid": "下中", "btm_right": "下右",
}


def iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def round_or_none(v: float | None, n: int = 2) -> float | None:
    return round(v, n) if isinstance(v, (int, float)) and math.isfinite(float(v)) else None


def _avg(values: list[float]) -> float | None:
    vals = [float(x) for x in values if isinstance(x, (int, float)) and math.isfinite(float(x))]
    return mean(vals) if vals else None


def datatemp_metrics(pt_pv: list[Any] | None) -> dict[str, Any]:
    """出炉温度六点空间映射（按2026-10-07用户最新确认口径）。

    JSON 数组为 [pt1, pt2, pt3, pt4, pt5, pt6]
    PT1/PT2/PT3 = 前左/前中/前右；PT4/PT5/PT6 = 后左/后中/后右。
    后台仍保留ir_scan/pt_pv原始字段，UI统一称“出炉温度”。
    """
    if not isinstance(pt_pv, list) or len(pt_pv) < 6:
        return {"available": False, "points": {}}

    try:
        p = {i + 1: float(pt_pv[i]) for i in range(6)}
    except Exception:
        return {"available": False, "points": {}}

    vals = [p[i] for i in range(1, 7)]
    front = [p[1], p[2], p[3]]
    rear = [p[4], p[5], p[6]]
    left = [p[1], p[4]]
    center = [p[2], p[5]]
    right = [p[3], p[6]]

    front_avg = mean(front)
    rear_avg = mean(rear)
    left_avg = mean(left)
    center_avg = mean(center)
    right_avg = mean(right)
    edge_avg = mean([left_avg, right_avg])

    return {
        "available": True,
        "points": {
            "p1": p[1], "p2": p[2], "p3": p[3], "p4": p[4], "p5": p[5], "p6": p[6],
        },
        "spatial": {
            "front_left": p[1], "front_center": p[2], "front_right": p[3],
            "rear_left": p[4], "rear_center": p[5], "rear_right": p[6],
        },
        "mean": round(mean(vals), 3),
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
        "range": round(max(vals) - min(vals), 3),
        "std": round(pstdev(vals), 3),
        "front_avg": round(front_avg, 3),
        "rear_avg": round(rear_avg, 3),
        "left_avg": round(left_avg, 3),
        "center_avg": round(center_avg, 3),
        "right_avg": round(right_avg, 3),
        "left_right_delta": round(left_avg - right_avg, 3),
        "front_rear_delta": round(front_avg - rear_avg, 3),
        "center_edge_delta": round(center_avg - edge_avg, 3),
    }


def furnace_summary(furnace_temp: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"zones": {}, "point_count": 0}
    if not isinstance(furnace_temp, dict):
        return out

    all_pv: list[float] = []
    all_sv: list[float] = []
    all_pwr: list[float] = []
    max_abs_dev = -1.0
    max_dev_point = None

    for z in sorted(furnace_temp):
        zd = furnace_temp.get(z) or {}
        points: list[dict[str, Any]] = []
        z_sv: list[float] = []
        z_pv: list[float] = []
        z_pwr: list[float] = []
        for pos in TEMP_POSITIONS:
            d = zd.get(pos) or {}
            sv, pv, pwr = d.get("sv"), d.get("pv"), d.get("pwr")
            if isinstance(sv, (int, float)) and isinstance(pv, (int, float)):
                dev = float(pv) - float(sv)
                if abs(dev) > max_abs_dev:
                    max_abs_dev = abs(dev)
                    max_dev_point = {"zone": z, "position": pos, "delta": round(dev, 3)}
            else:
                dev = None
            point = {
                "key": pos, "label": TEMP_POSITION_LABELS[pos],
                "sv": sv, "pv": pv, "pwr": pwr, "delta": round_or_none(dev, 3),
            }
            points.append(point)
            if isinstance(sv, (int, float)):
                z_sv.append(float(sv)); all_sv.append(float(sv))
            if isinstance(pv, (int, float)):
                z_pv.append(float(pv)); all_pv.append(float(pv))
            if isinstance(pwr, (int, float)):
                z_pwr.append(float(pwr)); all_pwr.append(float(pwr))
        out["zones"][z] = {
            "zone": z.upper(),
            "avg_sv": round_or_none(_avg(z_sv), 2),
            "avg_pv": round_or_none(_avg(z_pv), 2),
            "avg_pwr": round_or_none(_avg(z_pwr), 2),
            "max_abs_dev": round(max([abs(float(p["delta"])) for p in points if p["delta"] is not None] or [0.0]), 3),
            "points": points,
        }
        out["point_count"] += len(points)

    out.update({
        "avg_sv": round_or_none(_avg(all_sv), 2),
        "avg_pv": round_or_none(_avg(all_pv), 2),
        "avg_pwr": round_or_none(_avg(all_pwr), 2),
        "max_abs_dev": round_or_none(max_abs_dev if max_abs_dev >= 0 else None, 3),
        "max_dev_point": max_dev_point,
    })
    return out


def _conv_valves_for_zone(zone_data: dict[str, Any]) -> list[dict[str, Any]]:
    valves: list[dict[str, Any]] = []
    for group_name, group in zone_data.items():
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            valves.append({
                "group": group_name,
                "pos": item.get("pos"),
                "sv_big": item.get("sv_big"),
                "sv_small": item.get("sv_small"),
                "pv": item.get("pv"),
            })
    return valves


def convection_summary(convection: dict[str, Any] | None) -> dict[str, Any]:
    out = {"zones": {}, "physical_valves": 0}
    if not isinstance(convection, dict):
        return out

    for group_key in ("basic_zones", "lcr_zones"):
        group = convection.get(group_key) or {}
        if not isinstance(group, dict):
            continue
        for z, zd in group.items():
            valves = _conv_valves_for_zone(zd or {})
            out["physical_valves"] += len(valves)
            big = [float(x["sv_big"]) for x in valves if isinstance(x.get("sv_big"), (int, float))]
            small = [float(x["sv_small"]) for x in valves if isinstance(x.get("sv_small"), (int, float))]
            pv = [float(x["pv"]) for x in valves if isinstance(x.get("pv"), (int, float))]
            out["zones"][z] = {
                "zone": z.upper(), "type": "Basic" if group_key == "basic_zones" else "LCR",
                "valve_count": len(valves),
                "avg_sv_big": round_or_none(_avg(big), 2),
                "avg_sv_small": round_or_none(_avg(small), 2),
                "avg_pv": round_or_none(_avg(pv), 2),
                "valves": valves,
            }
    return out


def _mold_station_summary(station: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(station, dict):
        return {"available": False}
    points: list[dict[str, Any]] = []
    convex = station.get("convex") or {}
    concave = station.get("concave") or {}
    for key in ["s1", "s2", "s3", "s4", "s5", "s6"]:
        d = convex.get(key) or {}
        points.append({"part": "convex", "key": key, "label": f"凸模 {key.upper()}", **{k: d.get(k) for k in ("sv", "pv", "pwr")}})
    label_map = {"left": "凹模 左", "front": "凹模 前", "right": "凹模 右", "back": "凹模 后"}
    for key in ["left", "front", "right", "back"]:
        d = concave.get(key) or {}
        points.append({"part": "concave", "key": key, "label": label_map[key], **{k: d.get(k) for k in ("sv", "pv", "pwr")}})
    deltas = [abs(float(p["pv"]) - float(p["sv"])) for p in points if isinstance(p.get("sv"), (int, float)) and isinstance(p.get("pv"), (int, float))]
    max_delta = max(deltas) if deltas else None
    return {
        "available": True,
        "points": points,
        "max_abs_sv_pv": round_or_none(max_delta, 2),
        "near_setpoint": bool(max_delta is not None and max_delta <= MOLD_NEAR_SETPOINT_TOLERANCE_C),
        "near_setpoint_tolerance_c": MOLD_NEAR_SETPOINT_TOLERANCE_C,
    }


def mold_summary(mold_preheat: dict[str, Any] | None) -> dict[str, Any]:
    mold_preheat = mold_preheat if isinstance(mold_preheat, dict) else {}
    return {
        "n01": {"role": "当前工作压台", **_mold_station_summary(mold_preheat.get("n01"))},
        "n02": {"role": "备用预热压台", **_mold_station_summary(mold_preheat.get("n02"))},
    }


def torque_summary(torque: dict[str, Any] | None) -> dict[str, Any]:
    torque = torque if isinstance(torque, dict) else {}
    vals = {
        "axis1": torque.get("axis1_pv"),  # 左上
        "axis2": torque.get("axis2_pv"),  # 左下
        "axis3": torque.get("axis3_pv"),  # 右上
        "axis4": torque.get("axis4_pv"),  # 右下
    }
    if not all(isinstance(v, (int, float)) for v in vals.values()):
        return {"available": False, "axes": vals, "unit": None}
    a1, a2, a3, a4 = [float(vals[f"axis{i}"]) for i in range(1, 5)]
    return {
        "available": True,
        "unit": None,
        "axes": {
            "axis1": {"value": a1, "position": "左上"},
            "axis2": {"value": a2, "position": "左下"},
            "axis3": {"value": a3, "position": "右上"},
            "axis4": {"value": a4, "position": "右下"},
        },
        "left_avg": round(mean([a1, a2]), 3),
        "right_avg": round(mean([a3, a4]), 3),
        "upper_avg": round(mean([a1, a3]), 3),
        "lower_avg": round(mean([a2, a4]), 3),
        "left_right_delta": round(mean([a1, a2]) - mean([a3, a4]), 3),
        "upper_lower_delta": round(mean([a1, a3]) - mean([a2, a4]), 3),
        "range": round(max(a1, a2, a3, a4) - min(a1, a2, a3, a4), 3),
    }


def simple_four_values(obj: dict[str, Any] | None, keys: list[str]) -> dict[str, Any]:
    obj = obj if isinstance(obj, dict) else {}
    return {k: obj.get(k) for k in keys}


def anneal_summary(obj: dict[str, Any] | None) -> dict[str, Any]:
    obj = obj if isinstance(obj, dict) else {}
    vals = {
        "stage1_top": (obj.get("stage1_top") or {}).get("pv"),
        "stage1_btm": (obj.get("stage1_btm") or {}).get("pv"),
        "stage2_top": (obj.get("stage2_top") or {}).get("pv"),
        "stage2_btm": (obj.get("stage2_btm") or {}).get("pv"),
    }
    out: dict[str, Any] = {"values": vals, "unit": None}
    if all(isinstance(v, (int, float)) for v in vals.values()):
        out["stage1_top_btm_delta"] = round(float(vals["stage1_top"]) - float(vals["stage1_btm"]), 3)
        out["stage2_top_btm_delta"] = round(float(vals["stage2_top"]) - float(vals["stage2_btm"]), 3)
    return out


def build_summary(data: dict[str, Any], receive_time: str) -> dict[str, Any]:
    device_info = data.get("device_info") or {}
    loading = data.get("loading_status") or {}
    ir = data.get("ir_scan") or {}
    datatemp = datatemp_metrics(ir.get("pt_pv"))
    device_ts = device_info.get("timestamp")
    recv_dt = iso_to_dt(receive_time)
    src_dt = iso_to_dt(device_ts)
    age = (recv_dt - src_dt).total_seconds() if recv_dt and src_dt else None

    return {
        "device": {
            "requested_device_id": None,
            "device_id": device_info.get("device_id"),
            "line_name": device_info.get("line_name"),
            "source_timestamp": device_ts,
            "receive_timestamp": receive_time,
            "age_seconds": round_or_none(age, 1),
        },
        "production": {
            "prod_name": loading.get("prod_name"),
            "loading_gap_time_sv": loading.get("loading_gap_time_sv"),
        },
        "furnace": furnace_summary(data.get("furnace_temp")),
        "convection": convection_summary(data.get("convection")),
        "datatemp": {"ir_scan_time": ir.get("ir_scan_time"), **datatemp},
        "mold": mold_summary(data.get("mold_preheat")),
        "torque": torque_summary(data.get("pressing_torque")),
        "vacuum": simple_four_values(
            data.get("vacuum_pressure"),
            ["vac1_pressure_pv", "vac2_pressure_pv", "vac3_pressure_pv", "vac4_pressure_pv"],
        ),
        "anneal": anneal_summary(data.get("annealing_cooling")),
    }


def baseline_payload(summary: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """人工确认时保存的基线快照。首版保持简单，不自动学习/修改。"""
    return {
        "captured_summary": deepcopy(summary),
        "recipe": {
            "loading_gap_time_sv": (raw.get("loading_status") or {}).get("loading_gap_time_sv"),
            "furnace_temp": deepcopy(raw.get("furnace_temp") or {}),
            "convection": deepcopy(raw.get("convection") or {}),
        },
        "forming_window_half_width_c": DATATEMP_FORMING_WINDOW_C,
    }


def _baseline_summary(baseline: dict[str, Any] | None) -> dict[str, Any] | None:
    if not baseline:
        return None
    payload = baseline.get("payload") or {}
    return payload.get("captured_summary")


def diagnose(summary: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    dt = summary.get("datatemp") or {}
    bsum = _baseline_summary(baseline)
    findings: list[dict[str, Any]] = []
    suggestions: list[str] = []
    forming_state = "UNKNOWN"
    target = None
    window = DATATEMP_FORMING_WINDOW_C

    if bsum and dt.get("available") and (bsum.get("datatemp") or {}).get("available"):
        target = (bsum.get("datatemp") or {}).get("mean")
        current = dt.get("mean")
        if isinstance(target, (int, float)) and isinstance(current, (int, float)):
            delta = float(current) - float(target)
            if delta < -window:
                forming_state = "HARD"
                findings.append({
                    "code": "DATATEMP_BELOW_WINDOW", "severity": "WARN",
                    "text": f"出炉六点均温比已验证基线低 {abs(delta):.1f}℃，处于偏硬侧。",
                    "value": round(delta, 2),
                })
            elif delta > window:
                forming_state = "SOFT"
                findings.append({
                    "code": "DATATEMP_ABOVE_WINDOW", "severity": "WARN",
                    "text": f"出炉六点均温比已验证基线高 {delta:.1f}℃，处于偏软侧。",
                    "value": round(delta, 2),
                })
            else:
                forming_state = "IN_WINDOW"
                findings.append({
                    "code": "DATATEMP_IN_WINDOW", "severity": "INFO",
                    "text": f"出炉六点均温位于当前基线 ±{window:.0f}℃ 成型窗口内。",
                    "value": round(delta, 2),
                })
    elif dt.get("available"):
        forming_state = "NO_BASELINE"
        findings.append({
            "code": "NO_BASELINE", "severity": "INFO",
            "text": "当前有真实 DataTemp，但尚未建立该品种已验证运行基线，暂不做绝对软硬判定。",
        })
    else:
        findings.append({"code": "NO_DATATEMP", "severity": "INFO", "text": "当前没有有效 DataTemp 六点结果。"})

    # 空间特征：只描述事实，不直接做因果归因。
    if dt.get("available"):
        lr = dt.get("left_right_delta")
        fr = dt.get("front_rear_delta")
        ce = dt.get("center_edge_delta")
        findings.append({
            "code": "DATATEMP_SPATIAL", "severity": "INFO",
            "text": f"空间差异：左-右 {lr:+.1f}℃，前-后 {fr:+.1f}℃，中-边 {ce:+.1f}℃。",
            "left_right": lr, "front_rear": fr, "center_edge": ce,
        })

    # 与基线比较炉温/对流：只找最明显的区级偏移。
    temp_diffs: list[tuple[str, float]] = []
    conv_diffs: list[tuple[str, float]] = []
    if bsum:
        cur_z = (summary.get("furnace") or {}).get("zones") or {}
        base_z = (bsum.get("furnace") or {}).get("zones") or {}
        for z, cur in cur_z.items():
            b = base_z.get(z) or {}
            if isinstance(cur.get("avg_pv"), (int, float)) and isinstance(b.get("avg_pv"), (int, float)):
                temp_diffs.append((z, float(cur["avg_pv"]) - float(b["avg_pv"])))
        cur_c = (summary.get("convection") or {}).get("zones") or {}
        base_c = (bsum.get("convection") or {}).get("zones") or {}
        for z, cur in cur_c.items():
            b = base_c.get(z) or {}
            if isinstance(cur.get("avg_pv"), (int, float)) and isinstance(b.get("avg_pv"), (int, float)):
                conv_diffs.append((z, float(cur["avg_pv"]) - float(b["avg_pv"])))

        if temp_diffs:
            z, d = max(temp_diffs, key=lambda x: abs(x[1]))
            findings.append({
                "code": "FURNACE_BASELINE_MAX_DELTA", "severity": "INFO",
                "text": f"炉温区级均值相对基线最大变化位于 {z.upper()}：{d:+.1f}℃。",
                "zone": z, "delta": round(d, 2),
            })
        if conv_diffs:
            z, d = max(conv_diffs, key=lambda x: abs(x[1]))
            findings.append({
                "code": "CONVECTION_BASELINE_MAX_DELTA", "severity": "INFO",
                "text": f"对流实际反馈区级均值相对基线最大变化位于 {z.upper()}：{d:+.1f}。",
                "zone": z, "delta": round(d, 2),
            })

    # 人工确认型增量建议：尽量先恢复基线/检查局部，不做自动写 PLC。
    if forming_state == "HARD":
        top_temp = max(temp_diffs, key=lambda x: abs(x[1])) if temp_diffs else None
        top_conv = max(conv_diffs, key=lambda x: abs(x[1])) if conv_diffs else None
        if top_conv and top_conv[1] < 0 and (not top_temp or abs(top_temp[1]) < 3):
            suggestions.append(
                f"炉温主体未见明显整体下移，优先检查 {top_conv[0].upper()} 对流反馈及对应阀位，再考虑小幅对流补偿；不建议先整体抬升炉温。"
            )
        elif top_temp and top_temp[1] < -3:
            suggestions.append(
                f"先检查并恢复 {top_temp[0].upper()} 附近热场至已验证基线附近，观察后续 DataTemp；避免一次性大幅拉高整炉 SV。"
            )
        else:
            suggestions.append("先确认偏硬是否连续出现；若持续，优先按局部对流/热场证据做小幅调整，并观察后续测温响应。")
        suggestions.append("若热恢复能力不足，可评估小幅增大上片间隙给炉体恢复时间；首期仅给建议，不自动写入 PLC。")
    elif forming_state == "SOFT":
        suggestions.append("玻璃处于偏软侧时，优先检查局部过热、对流过强或节拍变化；避免仅依靠经验继续提高热输入。")
    elif forming_state == "IN_WINDOW":
        suggestions.append("当前出炉热状态位于已验证成型窗口内，建议保持现有基线附近运行并继续观察空间均匀性。")
    elif forming_state == "NO_BASELINE":
        suggestions.append("建议在稳定、质量确认合格的生产时段由工艺员人工发布运行基线，再启用相对偏差诊断。")

    return {
        "forming_state": forming_state,
        "target_mean_c": target,
        "window_half_width_c": window,
        "findings": findings,
        "suggestions": suggestions,
        "control_mode": "SUGGESTION_ONLY",
        "causality_note": "诊断基于时间邻近与相对基线证据，不声称重建单片玻璃完整炉内轨迹，也不把时间相关性直接当作因果关系。",
    }


def _numeric_values(mapping: dict[str, Any]) -> list[float]:
    return [float(v) for v in mapping.values() if isinstance(v, (int, float)) and math.isfinite(float(v))]


def forming_chain_analysis(summary: dict[str, Any], baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    """V3.4.1 成型链联合研判。

    把原先分散展示的出炉温度、N01当前工作压台、压制扭矩、真空和退火上下差
    组织成一条证据链。它只做工艺监督/解释，不声称单次快照已经证明因果关系。
    """
    from settings import OUTLET_RANGE_WARN_C, OUTLET_SIDE_DELTA_WARN_C, ANNEAL_STAGE_DELTA_ATTENTION_C

    dt = summary.get("datatemp") or {}
    mold = (summary.get("mold") or {}).get("n01") or {}
    torque = summary.get("torque") or {}
    vacuum = summary.get("vacuum") or {}
    anneal = summary.get("anneal") or {}

    bsum = _baseline_summary(baseline)
    bdt = (bsum or {}).get("datatemp") or {}

    evidence: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    actions: list[str] = []
    severity_rank = {"INFO": 0, "ATTENTION": 1, "WARN": 2}
    overall = "INFO"

    def add_finding(code: str, severity: str, title: str, text: str, data: dict[str, Any] | None = None) -> None:
        nonlocal overall
        findings.append({"code": code, "severity": severity, "title": title, "text": text, "data": data or {}})
        if severity_rank.get(severity, 0) > severity_rank.get(overall, 0):
            overall = severity

    # 1) 出炉温度：结果反馈与空间均匀性
    outlet_state = "UNAVAILABLE"
    target_mean = bdt.get("mean") if isinstance(bdt.get("mean"), (int, float)) else None
    if dt.get("available"):
        outlet_state = "OBSERVED"
        evidence.append({
            "name": "出炉温度",
            "value": {
                "mean_c": dt.get("mean"), "range_c": dt.get("range"),
                "left_right_c": dt.get("left_right_delta"), "front_rear_c": dt.get("front_rear_delta"),
            },
            "meaning": "玻璃经过12区后、进入压台前的终点板面热状态。",
        })
        r = dt.get("range")
        lr = dt.get("left_right_delta")
        fr = dt.get("front_rear_delta")
        if isinstance(r, (int, float)) and r > OUTLET_RANGE_WARN_C:
            add_finding("OUTLET_NONUNIFORM", "WARN", "出炉板面均匀性超出当前模型警戒", f"六点极差 {r:.1f}℃ > {OUTLET_RANGE_WARN_C:.1f}℃。这说明玻璃进入压台前仍存在明显空间热差，压制负荷和局部贴模行为可能不一致。", {"threshold_c": OUTLET_RANGE_WARN_C})
            actions.append("优先检查出炉六点空间方向与Z05-Z12对应热场/对流分布，不建议只看均温整体拉温。")
        elif isinstance(r, (int, float)):
            add_finding("OUTLET_UNIFORM", "INFO", "出炉板面均匀性处于当前模型参考范围", f"六点极差 {r:.1f}℃，未超过当前软件参考阈值 {OUTLET_RANGE_WARN_C:.1f}℃。仍需结合产品发布基线确认。")
        if isinstance(lr, (int, float)) and abs(lr) > OUTLET_SIDE_DELTA_WARN_C:
            add_finding("OUTLET_LR_BIAS", "ATTENTION", "出炉左右热偏差需要关注", f"左-右均温差 {lr:+.1f}℃，可能把压制负荷推向同一侧；建议与4轴扭矩左右差交叉验证。")
        if isinstance(fr, (int, float)) and abs(fr) > OUTLET_SIDE_DELTA_WARN_C:
            add_finding("OUTLET_FR_BIAS", "ATTENTION", "出炉前后热偏差需要关注", f"前-后均温差 {fr:+.1f}℃，说明板面沿行进方向仍存在热状态差。")
        if target_mean is not None and isinstance(dt.get("mean"), (int, float)):
            delta = float(dt["mean"]) - float(target_mean)
            if delta > DATATEMP_FORMING_WINDOW_C:
                outlet_state = "SOFT_SIDE"
                add_finding("OUTLET_SOFT_SIDE", "WARN", "出炉温度位于已验证基线偏热侧", f"出炉均温较发布基线高 {delta:.1f}℃。若压台热平衡正常，优先考虑前置热输入过量，防止软塌、模布印痕敏感性上升。")
            elif delta < -DATATEMP_FORMING_WINDOW_C:
                outlet_state = "HARD_SIDE"
                add_finding("OUTLET_HARD_SIDE", "WARN", "出炉温度位于已验证基线偏硬侧", f"出炉均温较发布基线低 {abs(delta):.1f}℃。若模具/真空正常，压制抗力、回弹和微裂风险需要重点关注。")
            else:
                outlet_state = "IN_WINDOW"
    else:
        evidence.append({"name": "出炉温度", "value": None, "meaning": "当前没有有效六点测温事件。"})

    # 2) N01 当前工作压台热平衡
    mold_balance: dict[str, Any] = {"available": bool(mold.get("available"))}
    if mold.get("available"):
        pts = mold.get("points") or []
        convex = [float(p["pv"]) for p in pts if p.get("part") == "convex" and isinstance(p.get("pv"), (int, float))]
        concave = [float(p["pv"]) for p in pts if p.get("part") == "concave" and isinstance(p.get("pv"), (int, float))]
        mold_balance.update({
            "convex_mean_c": round(mean(convex), 2) if convex else None,
            "convex_range_c": round(max(convex)-min(convex), 2) if convex else None,
            "lip_mean_c": round(mean(concave), 2) if concave else None,
            "lip_range_c": round(max(concave)-min(concave), 2) if concave else None,
            "convex_lip_delta_c": round(mean(convex)-mean(concave), 2) if convex and concave else None,
            "max_abs_sv_pv_c": mold.get("max_abs_sv_pv"),
        })
        evidence.append({"name": "N01当前工作压台", "value": mold_balance, "meaning": "用于判断压台自身热平衡是否可能参与当前成型异常。"})
        if isinstance(mold.get("max_abs_sv_pv"), (int, float)) and float(mold["max_abs_sv_pv"]) > MOLD_NEAR_SETPOINT_TOLERANCE_C:
            add_finding("MOLD_TRACKING", "ATTENTION", "N01模温跟随存在偏差", f"N01最大|PV-SV|={float(mold['max_abs_sv_pv']):.1f}℃，超过当前软件参考容差 {MOLD_NEAR_SETPOINT_TOLERANCE_C:.1f}℃。此时不应把全部成型偏差都归因到炉温。")

    # 3) 压制扭矩：只做相对/空间不对称，不发明单位
    if torque.get("available"):
        evidence.append({"name": "压制4轴", "value": {"left_right_delta": torque.get("left_right_delta"), "upper_lower_delta": torque.get("upper_lower_delta"), "range": torque.get("range")}, "meaning": "单位/语义未确认，只用于当前相对不对称证据。"})
        lr_t = torque.get("left_right_delta")
        if isinstance(lr_t, (int, float)) and isinstance(dt.get("left_right_delta"), (int, float)):
            # 不硬编码单位阈值，只在出炉左右热偏已经触发时做同向关联提示。
            if abs(float(dt["left_right_delta"])) > OUTLET_SIDE_DELTA_WARN_C and abs(float(lr_t)) > 0:
                add_finding("THERMAL_TORQUE_CORRELATION", "ATTENTION", "出炉热偏与压制左右负荷存在联合证据", f"出炉左-右温差 {float(dt['left_right_delta']):+.1f}℃，同时4轴左右平均差为 {float(lr_t):+.3f}（单位待确认）。两者时间上同时出现时，应优先检查热状态不均与压制受力方向是否一致，而不是单独调压台。")

    # 4) 真空：单位未知，只计算四路离散性作为背景
    vac_vals = _numeric_values(vacuum)
    vacuum_summary = None
    if vac_vals:
        vacuum_summary = {"mean": round(mean(vac_vals), 3), "range": round(max(vac_vals)-min(vac_vals), 3), "unit": None}
        evidence.append({"name": "真空4路", "value": vacuum_summary, "meaning": "单位尚未确认，仅作为压制/吸附背景状态，不做绝对阈值判定。"})

    # 5) 退火上下差：工艺后果是冷却不对称/残余应力风险，但不能直接反推MPa
    a1 = anneal.get("stage1_top_btm_delta")
    a2 = anneal.get("stage2_top_btm_delta")
    anneal_state = "UNAVAILABLE"
    if isinstance(a1, (int, float)) or isinstance(a2, (int, float)):
        anneal_state = "OBSERVED"
        evidence.append({"name": "退火上下温差", "value": {"stage1_top_minus_bottom_c": a1, "stage2_top_minus_bottom_c": a2}, "meaning": "上下冷却不对称的直接过程证据；不能在未标定前换算为真实残余应力MPa。"})
        max_abs = max(abs(float(x)) for x in (a1, a2) if isinstance(x, (int, float)))
        if max_abs > ANNEAL_STAGE_DELTA_ATTENTION_C:
            anneal_state = "ASYMMETRIC"
            add_finding("ANNEAL_ASYMMETRY", "WARN", "退火上下冷却存在明显不对称", f"当前一/二段上下差中最大绝对值为 {max_abs:.1f}℃。持续不对称可能造成厚度方向/上下表面冷却速率差，增加残余应力分布不均风险。")
            actions.append("优先核查退火风场、上下通道与设备状态；不要用提高炉温去补偿退火段冷却不对称。")

    # 6) 联合归因优先级
    causal_summary = "当前证据不足以做单一原因归因，建议按出炉温度→N01热平衡→压制/真空→退火的顺序排查。"
    if outlet_state in {"SOFT_SIDE", "HARD_SIDE"} and mold.get("available") and mold.get("near_setpoint"):
        causal_summary = "出炉热状态已偏离发布基线，而N01当前工作压台仍接近设定值；现阶段证据更支持先检查前置炉温/对流与出炉均匀性，再考虑模具本体。"
    elif outlet_state in {"IN_WINDOW", "OBSERVED"} and mold.get("available") and not mold.get("near_setpoint"):
        causal_summary = "出炉温度未见明显绝对偏离证据，但N01模温跟随存在偏差；应优先检查压台热平衡，避免误把模具问题当作炉温问题。"
    if anneal_state == "ASYMMETRIC":
        causal_summary += " 同时退火上下差较大，成型后质量风险还需单独沿退火冷却链排查。"

    if not actions:
        actions.append("持续观察同类异常是否连续出现，并与发布基线/后续质量结果交叉验证；首期只给建议，不自动写PLC。")

    # 给02页面的“成型链”提供结构化节点，不再只显示一句总结。
    finding_codes = {f.get("code") for f in findings}
    def node_sev(codes: set[str]) -> str:
        matched = [f for f in findings if f.get("code") in codes]
        if any(f.get("severity") == "WARN" for f in matched): return "WARN"
        if any(f.get("severity") == "ATTENTION" for f in matched): return "ATTENTION"
        return "INFO"

    outlet_value = None
    if dt.get("available"):
        outlet_value = f"均温 {float(dt.get('mean')):.1f}℃ · 极差 {float(dt.get('range')):.1f}℃"
    mold_value = None
    if mold_balance.get("available"):
        mold_value = f"凸模均值 {mold_balance.get('convex_mean_c', '--')}℃ · 模唇均值 {mold_balance.get('lip_mean_c', '--')}℃"
    torque_value = None
    if torque.get("available"):
        torque_value = f"左右差 {torque.get('left_right_delta', '--')} · 极差 {torque.get('range', '--')}"
    if vacuum_summary:
        torque_value = (torque_value + " · " if torque_value else "") + f"真空极差 {vacuum_summary.get('range', '--')}"
    anneal_value = None
    if isinstance(a1, (int, float)) or isinstance(a2, (int, float)):
        anneal_value = f"Δ1 {float(a1) if isinstance(a1,(int,float)) else '--'} · Δ2 {float(a2) if isinstance(a2,(int,float)) else '--'}"

    stage_nodes = [
        {"key":"outlet","name":"出炉温度","severity":node_sev({"OUTLET_NONUNIFORM","OUTLET_LR_BIAS","OUTLET_FR_BIAS","OUTLET_SOFT_SIDE","OUTLET_HARD_SIDE"}),"value":outlet_value or "暂无有效六点事件","meaning":"12区后的最终板面热状态"},
        {"key":"mold","name":"N01工作压台","severity":node_sev({"MOLD_TRACKING"}),"value":mold_value or "暂无模温证据","meaning":"判断压台自身热平衡"},
        {"key":"press","name":"压制 / 真空","severity":node_sev({"THERMAL_TORQUE_CORRELATION"}),"value":torque_value or "证据不足","meaning":"验证热状态是否已传递到受力/吸附"},
        {"key":"anneal","name":"退火上下差","severity":node_sev({"ANNEAL_ASYMMETRY"}),"value":anneal_value or "暂无退火差值","meaning":"判断冷却不对称与残余应力风险"},
    ]

    # 不是“AI拍脑袋根因”，而是把现有证据排序给工艺员。
    if outlet_state in {"SOFT_SIDE", "HARD_SIDE"} and mold.get("near_setpoint"):
        priority = ["前置炉温/对流与出炉均匀性", "压制负荷方向", "N01模具", "退火"]
    elif mold.get("available") and not mold.get("near_setpoint"):
        priority = ["N01模具热平衡", "出炉温度", "压制/真空", "退火"]
    elif anneal_state == "ASYMMETRIC":
        priority = ["退火风场/设备状态", "出炉温度", "N01模具", "压制/真空"]
    else:
        priority = ["出炉温度", "N01模具", "压制/真空", "退火"]

    return {
        "severity": overall,
        "outlet_state": outlet_state,
        "anneal_state": anneal_state,
        "evidence": evidence,
        "findings": findings,
        "mold_balance": mold_balance,
        "vacuum_summary": vacuum_summary,
        "causal_summary": causal_summary,
        "actions": actions,
        "stage_nodes": stage_nodes,
        "priority_order": priority,
        "model_limits": [
            "DataTemp/界面统一称‘出炉温度’，后台JSON字段仍保持ir_scan/pt_pv不变。",
            "pressing_torque与真空单位未确认，不做绝对阈值或物理单位推断。",
            "退火上下差用于冷却不对称预警；未建立质量模型前不输出真实残余应力MPa。",
            "联合分析属于过程证据研判，不等同于单片完整炉内轨迹或确定因果关系。",
        ],
    }
