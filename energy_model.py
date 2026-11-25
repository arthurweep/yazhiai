from __future__ import annotations

from statistics import mean
from typing import Any

from settings import (
    ENERGY_HOLD_MIN_PREHEAT_C,
    ENERGY_HOLD_MIN_MAIN_C,
    ENERGY_HOLD_MIN_EXIT_C,
    ENERGY_VECTOR_OFFSET_MIN_C,
    ENERGY_VECTOR_OFFSET_MAX_C,
)


def _radiative_potential(temp_c: float, ambient_c: float) -> float:
    """Relative radiative heat-loss potential: T^4 - Tamb^4.

    It is only the radiative component used by the theoretical shutdown model,
    not a claim that whole-furnace meter power follows T^4 exactly.
    """
    t = float(temp_c) + 273.15
    ta = float(ambient_c) + 273.15
    return max(0.0, t**4 - ta**4)


def _radiative_ratio_to_current(temp_c: float, current_c: float, ambient_c: float) -> float:
    den = _radiative_potential(current_c, ambient_c)
    if den <= 1e-12:
        return 1.0
    return max(0.0, _radiative_potential(temp_c, ambient_c) / den)


def _valid_zone_profile(zone_profile: list[dict[str, Any]] | None) -> list[dict[str, float]]:
    out = []
    for row in zone_profile or []:
        z = str(row.get("zone") or "").upper()
        v = row.get("pv_c")
        if z and isinstance(v, (int, float)):
            out.append({"zone": z, "pv_c": float(v)})
    return sorted(out, key=lambda x: int(x["zone"].replace("Z", "")))


def _zone_index(zone: str) -> int:
    try:
        return int(str(zone).upper().replace("Z", ""))
    except Exception:
        return 0


def _segment(zone: str) -> str:
    i = _zone_index(zone)
    if 1 <= i <= 4:
        return "PREHEAT"
    if 5 <= i <= 9:
        return "MAIN_HEAT"
    return "EXIT_SOAK"


def _floor(zone: str) -> float:
    seg = _segment(zone)
    if seg == "PREHEAT":
        return ENERGY_HOLD_MIN_PREHEAT_C
    if seg == "MAIN_HEAT":
        return ENERGY_HOLD_MIN_MAIN_C
    return ENERGY_HOLD_MIN_EXIT_C


def _shape_retention_floor_map(zones: list[dict[str, float]]) -> dict[str, float]:
    """Keep segment shape above the software protection floors.

    The optimizer starts from the actual current PV vector.  It never replaces a
    real production heat field with a fabricated staircase.  Demo staircase data
    belongs to the simulation layer only.
    """
    out: dict[str, float] = {}
    factors = {"PREHEAT": 0.50, "MAIN_HEAT": 0.45, "EXIT_SOAK": 1.00}
    for seg in ("PREHEAT", "MAIN_HEAT", "EXIT_SOAK"):
        rows = [r for r in zones if _segment(r["zone"]) == seg]
        if not rows:
            continue
        seg_min = min(float(r["pv_c"]) for r in rows)
        base = _floor(rows[0]["zone"])
        k = factors[seg]
        for r in rows:
            cur = float(r["pv_c"])
            idx = _zone_index(r["zone"])
            # Stage-specific shape survives an initially flat heat field.
            lift = ([0, 7, 14, 21][idx-1] if seg == "PREHEAT" else
                    [0, 8, 16, 24, 8][idx-5] if seg == "MAIN_HEAT" else
                    [12, 6, 0][idx-10])
            shaped = base + lift + max(0.0, cur - seg_min) * k
            out[r["zone"]] = round(min(cur, shaped), 1)
    return out


def _zone_capacity_weights(zones: list[dict[str, float]]) -> dict[str, float]:
    """Engineering weights for reheat cost, normalized so their sum is 12.

    Main-heat zones represent the hotter refractory inventory and receive a
    larger share.  This is intentionally a replaceable engineering prior until
    zone-by-zone heat capacity is calibrated from real reheating data.
    """
    pv = [float(r["pv_c"]) for r in zones]
    lo, hi = min(pv), max(pv)
    span = max(1.0, hi - lo)
    stage = {"PREHEAT": 0.78, "MAIN_HEAT": 1.22, "EXIT_SOAK": 1.02}
    raw: dict[str, float] = {}
    for r in zones:
        temp_factor = 0.88 + 0.24 * ((float(r["pv_c"]) - lo) / span)
        raw[r["zone"]] = stage[_segment(r["zone"])] * temp_factor
    scale = 12.0 / max(1e-9, sum(raw.values()))
    return {z: w * scale for z, w in raw.items()}


def _current_power_allocation(zones: list[dict[str, float]], total_power_kw: float, ambient_c: float) -> dict[str, float]:
    """Allocate the configured current full-temperature standby power to zones.

    No reference hold temperature is needed.  The configured power is treated as
    the whole-furnace standby power at the *current* 12-zone heat field.  Zone
    shares are distributed by their current T^4 radiative potential.
    """
    pots = {r["zone"]: _radiative_potential(r["pv_c"], ambient_c) for r in zones}
    den = sum(pots.values())
    if den <= 1e-12:
        return {r["zone"]: float(total_power_kw) / len(zones) for r in zones}
    return {z: float(total_power_kw) * p / den for z, p in pots.items()}


def _context(*, config: dict[str, Any], current_zone_profile: list[dict[str, Any]], downtime_minutes: float, mold_context: dict[str, Any] | None = None, recovery_target_profile: dict[str, float] | None = None, recovery_source: str | None = None) -> dict[str, Any]:
    if not config or not int(config.get("configured") or 0):
        raise ValueError("04能耗模型尚未配置：请先填写当前全温待机参考功率和炉体等效热容量")
    zones = _valid_zone_profile(current_zone_profile)
    if len(zones) != 12:
        raise ValueError(f"当前需要12个温区PV热场，实际取得 {len(zones)} 个")
    ref_power = float(config["hold_reference_power_kw"])
    heat_capacity = float(config["equivalent_heat_capacity_kwh_per_c"])
    efficiency = max(0.01, min(1.0, float(config.get("heating_efficiency") or 1.0)))
    ambient = float(config.get("ambient_temp_c") or 25.0)
    hours = max(0.0, float(downtime_minutes)) / 60.0
    current_avg = mean([z["pv_c"] for z in zones])
    # Current full-temperature standby is the calibration anchor; no reference temperature parameter.
    full_hold_power_kw = ref_power
    full_hold_energy_kwh = full_hold_power_kw * hours
    zone_power_ref = _current_power_allocation(zones, ref_power, ambient)
    capacity_weights = _zone_capacity_weights(zones)
    floor_map = _shape_retention_floor_map(zones)
    recovery_map: dict[str, float] = {}
    supplied = {str(k).upper(): float(v) for k, v in (recovery_target_profile or {}).items() if isinstance(v, (int, float))}
    for row in zones:
        z = row["zone"]
        recovery_map[z] = supplied.get(z, float(row["pv_c"]))

    mold_context = mold_context or {}
    mold_points = mold_context.get("points") or []
    convex = [float(p["pv"]) for p in mold_points if p.get("part") == "convex" and isinstance(p.get("pv"), (int, float))]
    lips = [float(p["pv"]) for p in mold_points if p.get("part") == "concave" and isinstance(p.get("pv"), (int, float))]
    mold_protection_context = {
        "available": bool(mold_context.get("available")),
        "convex_mean_c": round(mean(convex), 1) if convex else None,
        "lip_mean_c": round(mean(lips), 1) if lips else None,
        "note": "N01模温作为Z10-Z12保护背景；当前尚无已验证炉口-模具热耦合系数，因此不从模温直接反算保温值。",
    }
    return {
        "zones": zones,
        "floor_map": floor_map,
        "mold_protection_context": mold_protection_context,
        "ref_power": ref_power,
        "heat_capacity": heat_capacity,
        "efficiency": efficiency,
        "ambient": ambient,
        "hours": hours,
        "current_avg": current_avg,
        "full_hold_power_kw": full_hold_power_kw,
        "full_hold_energy_kwh": full_hold_energy_kwh,
        "zone_power_reference_kw": zone_power_ref,
        "zone_capacity_weights": capacity_weights,
        "reheat_available_power_kw": float(config["reheat_available_power_kw"]) if isinstance(config.get("reheat_available_power_kw"), (int, float)) and config["reheat_available_power_kw"] > 0 else None,
        "recovery_target_map": recovery_map,
        "recovery_source": recovery_source or "停机前当前生产配方SV",
    }


def _profile_map(rows: list[dict[str, Any]], key: str = "hold_temp_c") -> dict[str, float]:
    return {str(r["zone"]).upper(): float(r[key]) for r in rows if isinstance(r.get(key), (int, float))}


def evaluate_hold_profile(*, hold_profile: list[dict[str, Any]], ctx: dict[str, Any]) -> dict[str, Any]:
    selected = _profile_map(hold_profile)
    rows: list[dict[str, Any]] = []
    violations: list[str] = []
    hold_power_kw = 0.0
    reheat_energy = 0.0
    for zrow in ctx["zones"]:
        zone = zrow["zone"]
        cur = float(zrow["pv_c"])
        floor = float((ctx.get("floor_map") or {}).get(zone, _floor(zone)))
        effective_floor = min(cur, floor)
        target = float(selected.get(zone, cur))
        target = max(effective_floor, min(cur, target))
        if cur < floor:
            violations.append(f"{zone}当前PV {cur:.1f}℃ 已低于软件保护下限 {floor:.1f}℃，模型不为保温目的反向抬温")
        ratio = _radiative_ratio_to_current(target, cur, ctx["ambient"])
        zone_ref_power = float(ctx["zone_power_reference_kw"].get(zone, ctx["ref_power"] / 12.0))
        zone_hold_power = zone_ref_power * ratio
        cap_weight = float(ctx["zone_capacity_weights"].get(zone, 1.0))
        zone_capacity = ctx["heat_capacity"] * cap_weight / 12.0
        drop = max(0.0, cur - target)
        recovery_target = float((ctx.get("recovery_target_map") or {}).get(zone, cur))
        recovery_rise = max(0.0, recovery_target - target)
        zone_reheat = zone_capacity * recovery_rise / ctx["efficiency"]
        hold_power_kw += zone_hold_power
        reheat_energy += zone_reheat
        rows.append({
            "zone": zone,
            "segment": _segment(zone),
            "current_temp_c": round(cur, 1),
            "hold_temp_c": round(target, 1),
            "drop_c": round(drop, 1),
            "recovery_target_c": round(recovery_target, 1),
            "recovery_rise_c": round(recovery_rise, 1),
            "floor_c": round(floor, 1),
            "current_power_share_kw": round(zone_ref_power, 3),
            "hold_power_kw": round(zone_hold_power, 3),
            "capacity_weight": round(cap_weight, 4),
            "reheat_energy_kwh": round(zone_reheat, 3),
        })

    hold_energy = hold_power_kw * ctx["hours"]
    standby_saving = ctx["full_hold_energy_kwh"] - hold_energy
    net_benefit = standby_saving - reheat_energy
    return {
        "hold_temp_c": round(mean([r["hold_temp_c"] for r in rows]), 1),  # compatibility display only
        "hold_power_kw": round(hold_power_kw, 2),
        "hold_energy_kwh": round(hold_energy, 2),
        "standby_saving_kwh": round(standby_saving, 2),
        "reheat_energy_kwh": round(reheat_energy, 2),
        "total_energy_kwh": round(hold_energy + reheat_energy, 2),
        "net_benefit_kwh": round(net_benefit, 2),
        "hold_profile": rows,
        "violations": violations,
    }


def _hold_stage_template(downtime_minutes: float) -> dict[str, float]:
    """Canonical shutdown hold staircase for V3.4.4.

    The output shape is driven by downtime and zone role, not copied from the
    production PV shape. Current PV is still used as the energy/reheat starting
    state and as an upper cap (shutdown never heats a zone merely to meet template).
    Values are engineering-development anchors and remain bounded by protection
    floors; they are not PLC setpoints until site validation.
    """
    short = [520, 535, 550, 565, 595, 605, 615, 610, 595, 575, 565, 555]
    medium = [455, 470, 485, 500, 525, 540, 552, 548, 532, 565, 558, 552]
    long = [390, 400, 410, 420, 462, 478, 492, 488, 472, 555, 552, 550]
    m = max(15.0, min(480.0, float(downtime_minutes)))
    if m <= 120.0:
        t = (m - 15.0) / 105.0
        a, b = short, medium
    else:
        t = (m - 120.0) / 360.0
        a, b = medium, long
    vals = [a[i] + (b[i]-a[i])*t for i in range(12)]
    return {f"Z{i+1:02d}": round(vals[i], 1) for i in range(12)}


def _blend_energy_optimum_with_stage(raw_profile: list[dict[str, Any]], downtime_minutes: float) -> list[dict[str, Any]]:
    template = _hold_stage_template(downtime_minutes)
    # Short stops retain more influence from energy optimum/current thermal state;
    # long stops converge toward the canonical staged hold vector.
    m = max(15.0, min(480.0, float(downtime_minutes)))
    stage_weight = 0.30 + 0.55 * ((m - 15.0) / 465.0)
    out = []
    for row in raw_profile:
        r = dict(row)
        cur = float(r["current_temp_c"])
        floor = min(cur, float(r["floor_c"]))
        raw = float(r["hold_temp_c"])
        staged = min(cur, float(template.get(r["zone"], raw)))
        target = raw*(1-stage_weight) + staged*stage_weight
        # keep within physical/protection bounds; never heat during shutdown.
        target = max(floor, min(cur, target))
        r["hold_temp_c"] = round(target, 1)
        r["drop_c"] = round(max(0.0, cur-target), 1)
        out.append(r)
    return out


def _staged_operational_cap(zone: str, current_c: float, downtime_minutes: float, floor: float) -> float:
    """Explicit maximum staged hold allowed by process plan (engineering prior).

    Manual +offsets above this envelope are clamped, rather than incorrectly
    reporting a forbidden candidate as better than the constrained optimum.
    """
    staged = _hold_stage_template(downtime_minutes).get(zone, float(current_c))
    factor = 0.07 + 0.85 * min(1., max(0., (float(downtime_minutes)-15)/465))
    return max(floor, min(float(current_c), float(current_c)-factor*max(0.,float(current_c)-staged)))


def _best_zone_target(zone: str, current_c: float, ctx: dict[str, Any], step_c: float) -> float:
    """Constrained one-zone search using the same hold/reheat objective as final reporting.

    V3.4.8 supports a recovery target that may differ from the pre-shutdown PV
    (for example a saved 03 candidate recipe after a changeover).  A small
    deterministic grid avoids the old derivative assumption that recovery
    always returns to the current PV.
    """
    floor = min(float(current_c), float((ctx.get("floor_map") or {}).get(zone, _floor(zone))))
    upper = _staged_operational_cap(zone, current_c, ctx["hours"]*60, floor)
    recovery = float((ctx.get("recovery_target_map") or {}).get(zone, current_c))
    ref_kw = float(ctx["zone_power_reference_kw"].get(zone, ctx["ref_power"] / 12))
    cap = float(ctx["heat_capacity"]) * float(ctx["zone_capacity_weights"].get(zone, 1)) / 12
    ambient = float(ctx["ambient"])
    step = max(1.0, min(10.0, float(step_c)))
    candidates = []
    t = floor
    while t <= upper + 1e-9:
        ratio = _radiative_ratio_to_current(t, current_c, ambient)
        hold_e = ref_kw * ratio * ctx["hours"]
        reheat_e = cap * max(0.0, recovery - t) / ctx["efficiency"]
        candidates.append((hold_e + reheat_e, t))
        t += step
    candidates.append((
        ref_kw * _radiative_ratio_to_current(upper, current_c, ambient) * ctx["hours"] + cap * max(0.0, recovery-upper) / ctx["efficiency"],
        upper
    ))
    return round(min(candidates, key=lambda x: x[0])[1], 2)

def _restore_segment_shape(profile: list[dict[str, Any]], downtime_minutes: float) -> list[dict[str, Any]]:
    rows = [dict(x) for x in profile]
    retention = max(0.45, min(0.95, 0.95 - max(0.0, float(downtime_minutes)-30.0) / 600.0))
    for seg in ("PREHEAT", "MAIN_HEAT", "EXIT_SOAK"):
        idxs = [i for i,r in enumerate(rows) if r["segment"] == seg]
        if not idxs:
            continue
        cur_avg = mean([float(rows[i]["current_temp_c"]) for i in idxs])
        target_avg = mean([float(rows[i]["hold_temp_c"]) for i in idxs])
        for i in idxs:
            cur = float(rows[i]["current_temp_c"])
            floor = min(cur, float(rows[i]["floor_c"]))
            shaped = target_avg + retention * (cur - cur_avg)
            rows[i]["hold_temp_c"] = round(max(floor, min(cur, shaped)), 1)
            rows[i]["drop_c"] = round(max(0.0, cur-rows[i]["hold_temp_c"]), 1)
    return rows


def _smooth_within_segments(profile: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(x) for x in profile]
    for seg in ("PREHEAT", "MAIN_HEAT", "EXIT_SOAK"):
        idxs = [i for i, r in enumerate(rows) if r["segment"] == seg]
        if len(idxs) < 3:
            continue
        original = [rows[i]["hold_temp_c"] for i in idxs]
        smoothed = original[:]
        for j in range(1, len(original)-1):
            smoothed[j] = round((original[j-1] + 2*original[j] + original[j+1]) / 4.0, 1)
        for j, i in enumerate(idxs):
            cur = rows[i]["current_temp_c"]
            floor = min(cur, rows[i]["floor_c"])
            rows[i]["hold_temp_c"] = round(max(floor, min(cur, smoothed[j])), 1)
            rows[i]["drop_c"] = round(max(0.0, cur-rows[i]["hold_temp_c"]), 1)
    return rows


def _apply_offsets(best_profile: list[dict[str, Any]], global_offset_c: float, zone_offsets_c: dict[str, float] | None) -> list[dict[str, Any]]:
    zone_offsets_c = {str(k).upper(): float(v) for k, v in (zone_offsets_c or {}).items()}
    out = []
    for row in best_profile:
        cur = float(row["current_temp_c"])
        floor = min(cur, float(row["floor_c"]))
        t = float(row["hold_temp_c"]) + float(global_offset_c) + zone_offsets_c.get(row["zone"], 0.0)
        upper = float(row.get("max_operational_temp_c", cur))
        t = max(floor, min(cur, upper, t))
        out.append({"zone": row["zone"], "hold_temp_c": round(t, 1)})
    return out


def _reheat_schedule(ctx: dict[str, Any], selected: dict[str, Any], downtime_minutes: float) -> dict[str, Any]:
    """Optional theoretical timing, not a guaranteed warm-up clock.

    Net heater power during recovery is different from idle holding power;
    never silently treat them as identical.
    """
    kw = ctx.get("reheat_available_power_kw")
    if kw is None or kw <= 0:
        return {"calibrated": False, "note": "需标定复线净可用升温功率，才能倒推何时开始升温"}
    # conservative estimate from temperature recovery energy; no guaranteed readiness without ramp logs
    minutes = max(0., float(selected["reheat_energy_kwh"]) / kw * 60.)
    start = max(0., float(downtime_minutes) - minutes)
    return {
        "calibrated": True, "reheat_minutes": round(minutes, 1),
        "start_reheat_at_minute": round(start, 1),
        "feasible_within_downtime": minutes <= float(downtime_minutes),
        "net_heating_power_kw": kw,
        "note": "能量守恒近似估算，未包括设备升温梯度限速、均热稳定和换型时间；正式运行前需用真实升温曲线和安全裕量校准。",
    }


def optimize_energy(
    *,
    downtime_minutes: float,
    min_hold_temp_c: float = 100.0,  # API compatibility only
    max_hold_temp_c: float = 750.0,
    step_c: float = 5.0,
    config: dict[str, Any],
    current_zone_profile: list[dict[str, Any]],
    selected_hold_temp_c: float | None = None,  # V3.4 compatibility only
    global_offset_c: float = 0.0,
    zone_offsets_c: dict[str, float] | None = None,
    mold_context: dict[str, Any] | None = None,
    recovery_target_profile: dict[str, float] | None = None,
    recovery_source: str | None = None,
) -> dict[str, Any]:
    """V3.4.2 staged 12-zone shutdown hold-vector optimization.

    There is deliberately no 'reference hold temperature'.  The model starts
    from the current 12-zone PV vector and the requested downtime.  The only
    energy calibration inputs are current full-temperature standby power,
    equivalent furnace heat capacity, heating efficiency and ambient temperature.
    """
    ctx = _context(config=config, current_zone_profile=current_zone_profile, downtime_minutes=downtime_minutes, mold_context=mold_context, recovery_target_profile=recovery_target_profile, recovery_source=recovery_source)
    step = max(1.0, float(step_c))

    raw_best = []
    for row in ctx["zones"]:
        zone = row["zone"]
        cur = float(row["pv_c"])
        target = _best_zone_target(zone, cur, ctx, step)
        floor_for_zone = min(cur, float((ctx.get("floor_map") or {}).get(zone, _floor(zone))))
        raw_best.append({
            "zone": zone, "segment": _segment(zone), "current_temp_c": round(cur,1),
            "hold_temp_c": target,
            "floor_c": round(float((ctx.get("floor_map") or {}).get(zone, _floor(zone))),1),
            "max_operational_temp_c": round(_staged_operational_cap(zone, cur, downtime_minutes, floor_for_zone), 2),
            "drop_c": round(max(0.0,cur-target),1),
        })
    # Do not blend a mathematical energy optimum with a visual staircase template:
    # that made manually lowering temperatures appear *better* than "optimal".
    # Zone-specific stage protection floors are already part of _best_zone_target.
    best = evaluate_hold_profile(hold_profile=raw_best, ctx=ctx)

    # V3.4.4 deliberately keeps a staged hold shape even when current PV happens
    # to be flat. Current PV affects energy and reheat cost, not the required
    # shutdown spatial pattern. Therefore a flat no-change vector is not allowed
    # to replace the staged optimum merely because this simplified energy proxy
    # gives it a marginal numerical advantage.
    best_meta = {r["zone"]: r for r in raw_best}
    for r in best["hold_profile"]:
        meta = best_meta[r["zone"]]
        r["segment"] = meta["segment"]
        r["floor_c"] = meta["floor_c"]
        r["max_operational_temp_c"] = meta["max_operational_temp_c"]

    if isinstance(selected_hold_temp_c, (int, float)) and abs(float(global_offset_c)) < 1e-9 and not zone_offsets_c:
        global_offset_c = float(selected_hold_temp_c) - float(best["hold_temp_c"])
    global_offset_c = max(ENERGY_VECTOR_OFFSET_MIN_C, min(ENERGY_VECTOR_OFFSET_MAX_C, float(global_offset_c)))

    selected_profile = _apply_offsets(best["hold_profile"], global_offset_c, zone_offsets_c)
    selected = evaluate_hold_profile(hold_profile=selected_profile, ctx=ctx)
    selected["global_offset_c"] = round(global_offset_c, 1)
    selected["zone_offsets_c"] = {str(k).upper(): round(float(v),1) for k,v in (zone_offsets_c or {}).items()}
    selected["loss_vs_best_kwh"] = round(max(0.0, best["net_benefit_kwh"] - selected["net_benefit_kwh"]), 2)

    points: list[dict[str, Any]] = []
    off = ENERGY_VECTOR_OFFSET_MIN_C
    while off <= ENERGY_VECTOR_OFFSET_MAX_C + 1e-9:
        prof = _apply_offsets(best["hold_profile"], off, {})
        ev = evaluate_hold_profile(hold_profile=prof, ctx=ctx)
        points.append({
            "offset_c": round(off, 1),
            "hold_temp_c": ev["hold_temp_c"],
            "hold_energy_kwh": ev["hold_energy_kwh"],
            "standby_saving_kwh": ev["standby_saving_kwh"],
            "reheat_energy_kwh": ev["reheat_energy_kwh"],
            "net_benefit_kwh": ev["net_benefit_kwh"],
        })
        off += 5.0

    if selected["loss_vs_best_kwh"] <= 1.0:
        ai = "当前方案与算法最优12区保温向量基本一致。算法直接从当前12区PV与停机时间求解，不需要也不使用‘参考保温温度’。"
    elif global_offset_c > 0:
        ai = f"当前方案相对最优向量整体抬高约 {global_offset_c:.1f}℃，复线恢复更轻，但待机热损失增加；理论净收益少约 {selected['loss_vs_best_kwh']:.1f} kWh。"
    elif global_offset_c < 0:
        ai = f"当前方案相对最优向量整体降低约 {abs(global_offset_c):.1f}℃，待机更省，但复线恢复能量增加；理论净收益少约 {selected['loss_vs_best_kwh']:.1f} kWh。"
    else:
        ai = f"当前存在单区人工修正，理论净收益相对算法最优少约 {selected['loss_vs_best_kwh']:.1f} kWh。请结合设备保护/换型实际约束决定是否保留。"

    execution_preview = {
        "status": "PREVIEW_ONLY",
        "write_to_plc": False,
        "command": "HOLD_TEMPERATURE_VECTOR",
        "downtime_minutes": round(float(downtime_minutes), 1),
        "zones": {r["zone"].lower(): r["hold_temp_c"] for r in selected["hold_profile"]},
        "recovery_target_zones": {z.lower(): round(v,1) for z, v in (ctx.get("recovery_target_map") or {}).items()},
        "recovery_source": ctx.get("recovery_source"),
        "note": "为未来执行JSON预留；当前没有PLC写接口，必须经过设备联锁/权限/审批后才能启用。",
    }

    return {
        "model": "当前12区PV + 分段保温允许温度上限/下限 + T⁴待机热损失代理 + 复线升温能量，在工艺约束内求能耗最小",
        "data_status": "MODEL",
        "downtime_minutes": float(downtime_minutes),
        "current_average_temp_c": round(ctx["current_avg"], 2),
        "current_zone_profile": ctx["zones"],
        "recovery_target_profile": [{"zone": z, "target_c": round(v,1)} for z,v in (ctx.get("recovery_target_map") or {}).items()],
        "recovery_source": ctx.get("recovery_source"),
        "full_temp_hold_power_kw": round(ctx["full_hold_power_kw"], 2),
        "reference_full_temp_energy_kwh": round(ctx["full_hold_energy_kwh"], 2),
        "best": best,
        "reheat_schedule": _reheat_schedule(ctx, selected, downtime_minutes),
        "selected": selected,
        "points": points,
        "chart_axis": {"x": "相对算法最优12区保温向量整体偏移 ΔT", "x_unit": "℃", "y": "理论能耗/收益", "y_unit": "kWh"},
        "hold_stage_template": _hold_stage_template(downtime_minutes),
        "calculation_logic": {
            "inputs": ["当前12区PV", "预计停机/换型时长", "复线目标12区SV（当前配方或03已保存候选）", "全温待机参考功率", "炉体等效热容量", "加热效率", "分段保护约束"],
            "steps": [
                "读取当前12区PV作为各区热储量与复线成本起点",
                "按停机时长生成预热段/主热段/出炉段的阶梯保温目标模板",
                "用T⁴相对热损失估计待机节省，用等效热容量估计从保温SV恢复到所选复线目标SV的升温代价",
                "在保护下限内合并能耗最优与阶梯模板，形成12区算法最优向量",
                "比较人工整体偏移/单区修正后的综合净收益损失"
            ],
            "equation": "综合净收益 = 全温待机基准 - 保温维持耗电 - 复线升温代价",
            "boundary": "T⁴仅为辐射热损失理论分量；保护下限/热容量权重/阶梯模板需现场标定。"
        },
        "segment_bounds": {
            "Z01-Z04": {"role": "预热段深降 + 保留段内阶梯", "base_floor_c": ENERGY_HOLD_MIN_PREHEAT_C},
            "Z05-Z09": {"role": "主热段寻优 + 高蓄热复线成本权重", "base_floor_c": ENERGY_HOLD_MIN_MAIN_C},
            "Z10-Z12": {"role": "出炉段保护 + 保留出口热场形态", "base_floor_c": ENERGY_HOLD_MIN_EXIT_C, "status": "CURRENT_PROJECT_ASSUMPTION_PENDING_SITE_CONFIRMATION"},
        },
        "mold_protection_context": ctx.get("mold_protection_context"),
        "ai_explanation": ai,
        "execution_preview": execution_preview,
        "oee_impact": {"quantified": False, "note": "缺少复线时间/产量/停机机会成本模型，当前不伪造OEE损失数值。"},
        "assumptions": [
            "停机前12区热场直接使用当前有效JSON的PV作为热储量/复线成本起点；保温目标形态由停机时间和三区段角色决定，即使初始PV平坦也会形成阶梯保温向量。",
            "能耗模型不再使用参考保温温度。配置的功率被解释为‘当前12区生产热场下的全温待机参考功率’，随后按各区当前T⁴热损失势分配。",
            "Demo全局PV仿真可叠加工艺阶梯热场，仅用于验证分区算法，不进入真实源JSON。",
            "Z01-Z04、Z05-Z09、Z10-Z12采用不同保护下限与停机阶梯目标；下限和阶段模板均为开发验证参数，正式执行前必须现场确认。",
            "复线升温成本采用分段+当前温度修正的热容量权重；获得真实分区升温曲线后可替换权重，不改变API/UI。",
            "T⁴仅表示理论辐射热损失分量，不等同于整炉电表实测。",
        ],
    }
