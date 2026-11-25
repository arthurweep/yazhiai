from __future__ import annotations

from copy import deepcopy
from typing import Any

from glass_model import (
    FORMING_WINDOW_HALF_C,
    DEFAULT_FORMING_LOG10_VISCOSITY,
    DEFAULT_VFT_MODEL,
    pair_forming_target,
    radiation_ratio,
    thermal_load_index,
    vft_curve,
)

# 不是单片炉内轨迹权重，而是新品种候选配方预览时的“工艺影响权重”。
# 前段避免粗暴抬温，中后段对成型热状态更敏感。
ZONE_WEIGHTS = {
    "z01": 0.08, "z02": 0.12, "z03": 0.18, "z04": 0.26,
    "z05": 0.42, "z06": 0.58, "z07": 0.76, "z08": 0.88,
    "z09": 0.92, "z10": 0.84, "z11": 0.72, "z12": 0.64,
}


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def target_forming_temp_suggestion(
    *,
    big_thickness_mm: float,
    small_thickness_mm: float,
    big_material: str,
    small_material: str,
    validated_target_c: float | None = None,
) -> dict[str, Any]:
    """统一调用V3.4机理内核，给新品种初调生成可编辑靶心。"""
    return pair_forming_target(
        big_thickness_mm=big_thickness_mm,
        small_thickness_mm=small_thickness_mm,
        big_material=big_material,
        small_material=small_material,
        validated_target_c=validated_target_c,
    )


def _zone_end_mean(furnace_temp: dict[str, Any]) -> float | None:
    vals: list[float] = []
    for z in ("z10", "z11", "z12"):
        for p in (furnace_temp.get(z) or {}).values():
            if isinstance(p, dict) and isinstance(p.get("sv"), (int, float)):
                vals.append(float(p["sv"]))
    return _mean(vals) if vals else None


def _zone_sv_profile(furnace_temp: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for z in sorted(furnace_temp):
        vals = [
            float(p["sv"]) for p in (furnace_temp[z] or {}).values()
            if isinstance(p, dict) and isinstance(p.get("sv"), (int, float))
        ]
        out.append({"zone": z.upper(), "avg_sv": round(_mean(vals), 1) if vals else None})
    return out


def _staged_reference_if_flat(reference: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Keep real stair profiles untouched; give flat test/demo recipes a staged candidate shape.

    This is only for the *candidate recipe* used by the new-product solver. It never
    rewrites live PV/SV data. The shape mirrors the process roles: preheat ramp,
    main-heat peak, then exit-soak taper.
    """
    out = deepcopy(reference)
    prof = _zone_sv_profile(out)
    vals = [float(x["avg_sv"]) for x in prof if isinstance(x.get("avg_sv"), (int, float))]
    if len(vals) != 12:
        return out, "REAL_REFERENCE_SHAPE"
    pre = _mean(vals[0:4]); main = _mean(vals[4:9]); exit_seg = _mean(vals[9:12])
    # A plausible pressing-furnace profile should show a real preheat-to-main rise and
    # a later soak/taper. A flat test profile with one isolated outlier is not treated
    # as a valid staircase merely because its total span is large.
    plausible_stage = (main >= pre + 8.0) and (main >= exit_seg + 3.0)
    if plausible_stage:
        return out, "REAL_REFERENCE_SHAPE"
    base = _mean(vals)
    offsets = [-34, -24, -14, -6, 2, 8, 14, 10, 4, -3, -8, -12]
    avg_off = _mean(offsets)
    targets = {f"z{i:02d}": base + (offsets[i-1] - avg_off) for i in range(1,13)}
    for z, zd in out.items():
        t = targets.get(str(z).lower())
        if t is None:
            continue
        vals_z = [float(p["sv"]) for p in (zd or {}).values() if isinstance(p, dict) and isinstance(p.get("sv"),(int,float))]
        cur_avg = _mean(vals_z) if vals_z else base
        delta = t - cur_avg
        for p in (zd or {}).values():
            if isinstance(p, dict) and isinstance(p.get("sv"),(int,float)):
                p["sv"] = round(float(p["sv"]) + delta, 1)
                p.pop("pv", None); p.pop("pwr", None)
    return out, "STAGED_CANDIDATE_FROM_FLAT_REFERENCE"


def _apply_reference_pane_demand(reference: dict[str, Any], heat_index: float) -> dict[str, Any]:
    """Let thickness change the 12-zone candidate shape instead of only one scalar target."""
    out = deepcopy(reference)
    demand_c = _clip((float(heat_index) - 1.0) * 7.0, -4.0, 9.0)
    demand_weights = {
        "z01":0.10,"z02":0.18,"z03":0.30,"z04":0.45,
        "z05":0.66,"z06":0.82,"z07":1.00,"z08":0.96,"z09":0.84,
        "z10":0.62,"z11":0.46,"z12":0.34,
    }
    for z,zd in out.items():
        d=demand_c*demand_weights.get(str(z).lower(),0.5)
        for p in (zd or {}).values():
            if isinstance(p,dict) and isinstance(p.get("sv"),(int,float)):
                p["sv"] = round(float(p["sv"]) + d,1)
                p.pop("pv",None); p.pop("pwr",None)
    return out


def _convection_zone_profile(convection: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for group_key in ("basic_zones", "lcr_zones"):
        for z, zd in (convection.get(group_key) or {}).items():
            big: list[float] = []
            small: list[float] = []
            for arr in (zd or {}).values():
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, dict):
                            if isinstance(item.get("sv_big"), (int, float)):
                                big.append(float(item["sv_big"]))
                            if isinstance(item.get("sv_small"), (int, float)):
                                small.append(float(item["sv_small"]))
            rows.append({
                "zone": z.upper(),
                "avg_sv_big": round(_mean(big), 1) if big else None,
                "avg_sv_small": round(_mean(small), 1) if small else None,
            })
    return sorted(rows, key=lambda x: x["zone"])


def _clone_adjust_furnace(reference: dict[str, Any], delta: float) -> dict[str, Any]:
    out = deepcopy(reference)
    for z, zd in out.items():
        w = ZONE_WEIGHTS.get(z, 0.5)
        for p in (zd or {}).values():
            if isinstance(p, dict) and isinstance(p.get("sv"), (int, float)):
                p["sv"] = round(float(p["sv"]) + delta * w, 1)
                p.pop("pv", None)
                p.pop("pwr", None)
    return out


def _adjust_convection(reference: dict[str, Any], big_delta: float, small_delta: float) -> dict[str, Any]:
    out = deepcopy(reference)
    for group_key in ("basic_zones", "lcr_zones"):
        for _, zd in (out.get(group_key) or {}).items():
            for _, arr in (zd or {}).items():
                if not isinstance(arr, list):
                    continue
                for item in arr:
                    if isinstance(item, dict):
                        if isinstance(item.get("sv_big"), (int, float)):
                            item["sv_big"] = round(_clip(float(item["sv_big"]) + big_delta, 0, 100), 1)
                        if isinstance(item.get("sv_small"), (int, float)):
                            item["sv_small"] = round(_clip(float(item["sv_small"]) + small_delta, 0, 100), 1)
                        item.pop("pv", None)
    return out


def _enforce_material_convection_policy(convection: dict[str, Any], *, big_is_lowe: bool, small_is_lowe: bool) -> dict[str, Any]:
    """V3.4.4 material policy for candidate recipes.

    Non-coated glass defaults to zero *new-product candidate* convection. Low-E keeps
    its independent channel so the solver can compensate radiation absorption loss.
    This only changes candidate suggestions, never the live equipment JSON.
    """
    out = deepcopy(convection)
    for group_key in ("basic_zones", "lcr_zones"):
        for _, zd in (out.get(group_key) or {}).items():
            for _, arr in (zd or {}).items():
                if not isinstance(arr, list):
                    continue
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    if not big_is_lowe and isinstance(item.get("sv_big"), (int, float)):
                        item["sv_big"] = 0.0
                    if not small_is_lowe and isinstance(item.get("sv_small"), (int, float)):
                        item["sv_small"] = 0.0
    return out


def _candidate_values_for_pane(*, is_lowe: bool, heat_index: float, peer_heat: float, hard_side: bool, soft_side: bool) -> list[float]:
    """Bounded candidate convection deltas for the first-pass search.

    Non-coated panes default to little/no extra convection because plant feedback
    says convection is costly.  Low-E gets a wider search range.  If the current
    result is already soft, negative deltas are allowed first.
    """
    if soft_side:
        return [-8.0, -5.0, -2.0, 0.0]
    if is_lowe:
        return [0.0, 3.0, 6.0, 9.0, 12.0] if hard_side else [0.0, 3.0, 6.0]
    # V3.4.4现场原则：共享炉温以大片/小片中的较厚片为主要热负荷依据；
    # 非镀膜片默认不主动新增对流，Low-E优先用对应独立对流补偿。若后续真实能耗标定证明
    # 少量对流综合更优，再开放非镀膜片这一搜索维度。
    return [0.0]


def _candidate_objective(preview: dict[str, Any], furnace_shift: float, big_delta: float, small_delta: float, gap_delta: float) -> dict[str, float]:
    target = float(preview["target_forming_temp_c"])
    big = float(preview["estimated_big_temp_c"])
    small = float(preview["estimated_small_temp_c"])
    pane_targets = preview.get("pane_targets") or {}
    big_target = float(pane_targets.get("big_c", target))
    small_target = float(pane_targets.get("small_c", target))
    quality = ((big-big_target)/5.0)**2 + ((small-small_target)/5.0)**2 + 0.55*((big-small)/4.0)**2
    rad = float(preview.get("radiation_load_ratio_vs_reference") or 1.0)
    conv = float(preview.get("convection_load_proxy_vs_reference") or 1.0)
    # Quality remains the hard priority.  Among quality-equivalent candidates,
    # convection is deliberately penalized more strongly than a small radiation shift.
    radiation_cost = max(0.0, rad-1.0) * 4.0 + max(0.0, 1.0-rad) * -0.35
    convection_cost = max(0.0, conv-1.0) * 7.0 + max(0.0, 1.0-conv) * -0.60
    adjustment = abs(furnace_shift)*0.035 + (abs(big_delta)+abs(small_delta))*0.025 + max(0.0,gap_delta)*0.10
    total = quality*18.0 + radiation_cost + convection_cost + adjustment
    return {
        "total": total, "quality": quality, "radiation": radiation_cost,
        "convection": convection_cost, "adjustment": adjustment,
    }


def initial_linked_solve(
    *,
    reference_recipe: dict[str, Any],
    current_datatemp_mean: float | None,
    target_forming_temp_c: float,
    big_thickness_mm: float,
    small_thickness_mm: float,
    big_material: str,
    small_material: str,
    current_loading_gap_s: float | None,
) -> dict[str, Any]:
    furnace = deepcopy(reference_recipe.get("furnace_temp") or {})
    convection = deepcopy(reference_recipe.get("convection") or {})
    if not furnace:
        raise ValueError("缺少参考炉温配方，无法执行联动求解")
    if not convection:
        raise ValueError("缺少参考对流配方，无法执行联动求解")

    ref_end = _zone_end_mean(furnace)
    current_result = current_datatemp_mean if isinstance(current_datatemp_mean, (int, float)) else ref_end
    if current_result is None:
        current_result = target_forming_temp_c
    result_gap = float(target_forming_temp_c) - float(current_result)

    big_load = thermal_load_index(big_thickness_mm, big_material)
    small_load = thermal_load_index(small_thickness_mm, small_material)
    big_is_lowe = str(big_material).lower() == "lowe"
    small_is_lowe = str(small_material).lower() == "lowe"
    big_heat = float(big_load["heat_load_index"])
    small_heat = float(small_load["heat_load_index"])
    # Shared furnace heat demand deliberately removes the Low-E radiation penalty:
    # thickness chooses the furnace staircase; coating shortfall is handled by
    # independent convection rather than by pushing the shared furnace hotter.
    big_furnace_heat = float(thermal_load_index(big_thickness_mm, "normal")["heat_load_index"])
    small_furnace_heat = float(thermal_load_index(small_thickness_mm, "normal")["heat_load_index"])
    ref_candidates = [("big", big_is_lowe, float(big_thickness_mm), big_furnace_heat), ("small", small_is_lowe, float(small_thickness_mm), small_furnace_heat)]
    reference_pane = max(ref_candidates, key=lambda x: (x[2], 1 if x[0] == "big" else 0))
    furnace, shape_status = _staged_reference_if_flat(furnace)
    furnace = _apply_reference_pane_demand(furnace, reference_pane[3])
    solver_reference = deepcopy(reference_recipe)
    solver_reference["furnace_temp"] = furnace
    hard_side = result_gap > FORMING_WINDOW_HALF_C
    soft_side = result_gap < -FORMING_WINDOW_HALF_C

    # Search a deliberately small engineering neighbourhood around the reference
    # recipe.  This is not global black-box optimization: the purpose is to rank
    # plausible incremental candidates while preserving the V3.4 safety boundary.
    if soft_side:
        furnace_candidates = list(range(-36, 1, 3))
    elif hard_side:
        furnace_candidates = list(range(0, 49, 2))
    else:
        furnace_candidates = list(range(-14, 17, 2))
    big_candidates = _candidate_values_for_pane(is_lowe=big_is_lowe, heat_index=big_heat, peer_heat=small_heat, hard_side=hard_side, soft_side=soft_side)
    small_candidates = _candidate_values_for_pane(is_lowe=small_is_lowe, heat_index=small_heat, peer_heat=big_heat, hard_side=hard_side, soft_side=soft_side)
    gap_candidates = [0.0]
    if isinstance(current_loading_gap_s, (int,float)):
        if hard_side: gap_candidates = [0.0, 0.5, 1.0]
        elif soft_side: gap_candidates = [-0.3, 0.0]

    ranked: list[dict[str, Any]] = []
    for furnace_shift in furnace_candidates:
        proposed_furnace = _clone_adjust_furnace(furnace, furnace_shift)
        zone_profile = _zone_sv_profile(proposed_furnace)
        for big_delta in big_candidates:
            for small_delta in small_candidates:
                proposed_convection = _adjust_convection(convection, big_delta, small_delta)
                proposed_convection = _enforce_material_convection_policy(
                    proposed_convection, big_is_lowe=big_is_lowe, small_is_lowe=small_is_lowe
                )
                conv_profile = _convection_zone_profile(proposed_convection)
                for gap_delta in gap_candidates:
                    gap = None if current_loading_gap_s is None else max(0.1, round(float(current_loading_gap_s)+gap_delta,1))
                    preview = preview_linked_adjustment(
                        reference_recipe=solver_reference,
                        reference_result_temp_c=float(current_result),
                        target_forming_temp_c=float(target_forming_temp_c),
                        big_thickness_mm=big_thickness_mm,
                        small_thickness_mm=small_thickness_mm,
                        big_material=big_material,
                        small_material=small_material,
                        zone_profile=zone_profile,
                        convection_zone_profile=conv_profile,
                        loading_gap_s=gap,
                        reference_loading_gap_s=current_loading_gap_s,
                    )
                    score = _candidate_objective(preview, furnace_shift, big_delta, small_delta, gap_delta)
                    ranked.append({
                        "furnace_shift": float(furnace_shift), "big_delta": float(big_delta), "small_delta": float(small_delta),
                        "gap_delta": float(gap_delta), "gap": gap, "preview": preview, "score": score,
                        "zone_profile": zone_profile, "convection_zone_profile": conv_profile,
                        "recipe": {"furnace_temp": proposed_furnace, "convection": proposed_convection, "loading_gap_time_sv": gap},
                    })
    feasible = [x for x in ranked if x["preview"].get("pane_states", {}).get("big") == "IN_WINDOW" and x["preview"].get("pane_states", {}).get("small") == "IN_WINDOW"]
    ranked.sort(key=lambda x: x["score"]["total"])
    feasible.sort(key=lambda x: x["score"]["total"])
    # Quality-window feasibility is a constraint, not a cheap score trade-off.
    best = feasible[0] if feasible else ranked[0]
    preview = best["preview"]

    if not feasible:
        largest_miss = min(max(abs(x["preview"]["estimated_big_temp_c"] - x["preview"]["pane_targets"]["big_c"]), abs(x["preview"]["estimated_small_temp_c"] - x["preview"]["pane_targets"]["small_c"])) for x in ranked)
    else:
        largest_miss = 0.0

    material_context = []
    if big_is_lowe: material_context.append("大片Low-E")
    if small_is_lowe: material_context.append("小片Low-E")
    if not material_context: material_context.append("大小片均非镀膜")
    material_text = "、".join(material_context)
    why = []
    if not feasible:
        why.append(f"当前参数/模型边界内没有两片同时进入成型窗口的可行方案；最小最大热偏差约{largest_miss:.1f}℃。请核实实际DataTemp、材料模型及对流实测补偿能力，不能将偏硬方案伪称最优。")
    else:
        why.append("先筛选大小片都命中成型窗口的候选，再以辐射/对流负荷和节拍代价排序，输出窗口内较低成本方案。")
    if shape_status == "STAGED_CANDIDATE_FROM_FLAT_REFERENCE":
        why.append("当前参考SV过于平坦，候选求解仅对建议配方建立‘预热爬升→主热峰值→均温回落’阶梯形态；不会改写实时JSON。")
    if abs(best["furnace_shift"]) <= 1 and (best["big_delta"] > 0 or best["small_delta"] > 0):
        why.append("共享炉温基本保持参考阶梯，优先用必要的独立对流修正片间吸热差。")
    elif best["furnace_shift"] > 1:
        why.append(f"综合比较后允许共享热场小幅抬高 {best['furnace_shift']:.1f}℃（按各区权重分配），因为继续增加对流的成本代理更高。")
    elif best["furnace_shift"] < -1:
        why.append(f"候选工艺位于偏软侧，先把共享热场回退 {abs(best['furnace_shift']):.1f}℃，避免靠过量热输入维持成型。")
    if big_is_lowe and best["big_delta"] > 0: why.append(f"大片Low-E增加对流 {best['big_delta']:.1f} 个开度点，用独立热通道补偿辐射吸热瓶颈。")
    if small_is_lowe and best["small_delta"] > 0: why.append(f"小片Low-E增加对流 {best['small_delta']:.1f} 个开度点，避免为了小片整体抬高共享炉温。")
    if not big_is_lowe and best["big_delta"] == 0: why.append("大片非镀膜，求解器没有主动新增对流。")
    if not small_is_lowe and best["small_delta"] == 0: why.append("小片非镀膜，求解器没有主动新增对流。")
    if best["gap_delta"] > 0: why.append(f"仍偏硬时只把上片间隙增加 {best['gap_delta']:.1f}s，给热场恢复时间；该动作带产能代价，需工艺员确认。")

    top_alternatives = []
    for item in ranked[:5]:
        top_alternatives.append({
            "furnace_shift_c": item["furnace_shift"],
            "convection_delta_big": item["big_delta"],
            "convection_delta_small": item["small_delta"],
            "loading_gap_delta_s": item["gap_delta"],
            "estimated_big_temp_c": item["preview"]["estimated_big_temp_c"],
            "estimated_small_temp_c": item["preview"]["estimated_small_temp_c"],
            "forming_state": item["preview"]["forming_state"],
            "radiation_proxy": item["preview"]["cost_tradeoff"]["radiation_proxy"],
            "convection_proxy": item["preview"]["cost_tradeoff"]["convection_proxy"],
            "objective": round(item["score"]["total"],4),
        })

    result = {
        "status": "DEVELOPMENT_VALIDATION",
        "candidate_feasible": bool(feasible),
        "title": "新品种初调参考｜工艺员确认后验证",
        "target_forming_temp_c": round(float(target_forming_temp_c), 1),
        "target_log10_eta": DEFAULT_FORMING_LOG10_VISCOSITY,
        "reference_result_temp_c": round(float(current_result), 1),
        "estimated_gap_c": round(result_gap, 1),
        "furnace_shift_max_c": round(float(best["furnace_shift"]), 1),
        "convection_delta_big": round(float(best["big_delta"]), 1),
        "convection_delta_small": round(float(best["small_delta"]), 1),
        "thermal_load": {"big": big_load, "small": small_load},
        "strategy": {
            "baseline_principle": f"共享炉温以{'大片' if reference_pane[0]=='big' else '小片'}为基准（厚度优先），并保持Z01-Z04预热爬升、Z05-Z09主热峰值、Z10-Z12均温回落的阶梯结构；镀膜片再用独立对流补偿。",
            "convection_cost_principle": "对流现场能耗高：非镀膜候选默认对流为0，Low-E优先使用对应sv_big/sv_small补偿；后续只有在完成真实能耗标定后，才允许对非镀膜开放‘少量对流替代升温’的优化维度。",
            "objective_principle": "先满足大小片成型热状态与片间一致性，再比较T⁴辐射负荷代理、对流负荷代理和节拍代价。",
            "material_context": material_text,
            "why": why,
        },
        "optimization": {
            "method": "BOUNDED_ENGINEERING_GRID_SEARCH",
            "candidate_count": len(ranked),
            "feasible_count": len(feasible),
            "feasible": bool(feasible),
            "feasibility_note": "两片均在目标窗口内" if feasible else f"未找到可行候选，最小最大偏差 {largest_miss:.1f}℃；需现场验证/扩展已标定工艺边界",
            "best_objective": round(best["score"]["total"],4),
            "components": {k: round(v,4) for k,v in best["score"].items() if k != "total"},
            "top_alternatives": top_alternatives,
            "note": "开发验证约束搜索，不是黑箱自动控制；真实DataTemp与质量验证仍是发布基线的必要条件。",
        },
        "loading_gap_current_s": current_loading_gap_s,
        "loading_gap_suggestion_s": best["gap"],
        "loading_gap_note": "上片间隙只作为热恢复/产能权衡项；首期仅建议，不自动写PLC。",
        "zone_profile": best["zone_profile"],
        "reference_zone_profile": _zone_sv_profile(furnace),
        "reference_shape_status": shape_status,
        "reference_pane": reference_pane[0],
        "convection_zone_profile": best["convection_zone_profile"],
        "reference_convection_zone_profile": _convection_zone_profile(_enforce_material_convection_policy(convection, big_is_lowe=big_is_lowe, small_is_lowe=small_is_lowe)),
        "recipe": best["recipe"],
        "logic": [
            "从当前/已验证参考配方开始，不凭空生成绝对配方。",
            "同一种基片玻璃的内禀VFT目标由粘度决定；厚度与Low-E主要改变热量进入玻璃的难易程度。",
            "大小片共用12区炉温；更厚片决定共享阶梯炉温，Low-E吸热差异再通过独立sv_big/sv_small补偿。",
            "求解器显式惩罚对流负荷，并同时比较共享炉温的T⁴辐射负荷代理；因此不是‘有Low-E就无限开风’。",
            "人工修改靶心、区级炉温、大小片对流或上片间隙后，03独立求解器立即重新预览VFT达成点与风险。",
        ],
        "limitations": [
            "现场玻璃成分的正式VFT A/B/C尚未标定，当前VFT用于参考形态与相对判断。",
            "辐射/对流成本均为相对参考配方的开发验证代理，不等同于电表实测kWh。",
            "候选值只用于开发验证/工艺初调，不写PLC，必须用真实出炉温度与质量结果验证。",
        ],
        "preview": preview,
    }
    return result

def _profile_dict(rows: list[dict[str, Any]], value_key: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows or []:
        z = str(row.get("zone") or "").lower()
        v = row.get(value_key)
        if z and isinstance(v, (int, float)):
            out[z] = float(v)
    return out


def _reference_profiles(reference_recipe: dict[str, Any]) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    z = {
        r["zone"].lower(): float(r["avg_sv"])
        for r in _zone_sv_profile(reference_recipe.get("furnace_temp") or {})
        if isinstance(r.get("avg_sv"), (int, float))
    }
    c: dict[str, dict[str, float]] = {}
    for r in _convection_zone_profile(reference_recipe.get("convection") or {}):
        c[r["zone"].lower()] = {
            "big": float(r["avg_sv_big"]) if isinstance(r.get("avg_sv_big"), (int, float)) else 0.0,
            "small": float(r["avg_sv_small"]) if isinstance(r.get("avg_sv_small"), (int, float)) else 0.0,
        }
    return z, c


def preview_linked_adjustment(
    *,
    reference_recipe: dict[str, Any],
    reference_result_temp_c: float,
    target_forming_temp_c: float,
    big_thickness_mm: float,
    small_thickness_mm: float,
    big_material: str,
    small_material: str,
    zone_profile: list[dict[str, Any]],
    convection_zone_profile: list[dict[str, Any]],
    loading_gap_s: float | None,
    reference_loading_gap_s: float | None,
) -> dict[str, Any]:
    ref_z, ref_c = _reference_profiles(reference_recipe)
    cand_z = _profile_dict(zone_profile, "avg_sv")

    furnace_num = furnace_den = 0.0
    zone_deltas: dict[str, float] = {}
    for z, ref in ref_z.items():
        cur = cand_z.get(z, ref)
        d = cur - ref
        zone_deltas[z] = d
        w = ZONE_WEIGHTS.get(z, 0.5)
        furnace_num += d * w
        furnace_den += w
    weighted_furnace_delta = furnace_num / furnace_den if furnace_den else 0.0
    furnace_effect = weighted_furnace_delta * 0.52

    big_load = thermal_load_index(big_thickness_mm, big_material)
    small_load = thermal_load_index(small_thickness_mm, small_material)
    pair_target = pair_forming_target(big_thickness_mm=big_thickness_mm, small_thickness_mm=small_thickness_mm, big_material=big_material, small_material=small_material, validated_target_c=None)
    target_shift = float(target_forming_temp_c) - float(pair_target["suggested_target_forming_temp_c"])
    big_target_c = float(pair_target.get("big_target_c", pair_target["big"]["recommended_measured_target_c"])) + target_shift
    small_target_c = float(pair_target.get("small_target_c", pair_target["small"]["recommended_measured_target_c"])) + target_shift
    big_factor = max(0.2, float(big_load["heat_load_index"]) * float(big_load["convection_dependency_index"]))
    small_factor = max(0.2, float(small_load["heat_load_index"]) * float(small_load["convection_dependency_index"]))

    big_conv_effects: list[float] = []
    small_conv_effects: list[float] = []
    conv_deltas: dict[str, dict[str, float]] = {}
    for row in convection_zone_profile or []:
        z = str(row.get("zone") or "").lower()
        if z not in ref_c:
            continue
        big = float(row.get("avg_sv_big") if isinstance(row.get("avg_sv_big"), (int, float)) else ref_c[z]["big"])
        small = float(row.get("avg_sv_small") if isinstance(row.get("avg_sv_small"), (int, float)) else ref_c[z]["small"])
        db = big - ref_c[z]["big"]
        ds = small - ref_c[z]["small"]
        conv_deltas[z] = {"big": db, "small": ds}
        zone_weight = 0.75 if z in {"z05", "z06", "z07", "z08"} else 0.45
        big_conv_effects.append(db * zone_weight / big_factor)
        small_conv_effects.append(ds * zone_weight / small_factor)

    # 对流变化对等效出炉温度的影响是开发验证灵敏度，不冒充CFD/真实热传递系数。
    big_conv_effect = _mean(big_conv_effects) * 0.13
    small_conv_effect = _mean(small_conv_effects) * 0.13
    common_conv_effect = (big_conv_effect + small_conv_effect) / 2.0

    gap_effect = 0.0
    if isinstance(loading_gap_s, (int, float)) and isinstance(reference_loading_gap_s, (int, float)):
        gap_effect = (float(loading_gap_s) - float(reference_loading_gap_s)) * 0.55

    common_base = float(reference_result_temp_c) + furnace_effect + gap_effect
    big_estimated = common_base + big_conv_effect
    small_estimated = common_base + small_conv_effect
    estimated = (big_estimated + small_estimated) / 2.0
    big_delta_target = big_estimated - big_target_c
    small_delta_target = small_estimated - small_target_c
    delta_to_target = estimated - float(target_forming_temp_c)
    pane_states = {
        "big": "IN_WINDOW" if abs(big_delta_target) <= FORMING_WINDOW_HALF_C else ("HARD" if big_delta_target < 0 else "SOFT"),
        "small": "IN_WINDOW" if abs(small_delta_target) <= FORMING_WINDOW_HALF_C else ("HARD" if small_delta_target < 0 else "SOFT"),
    }
    if pane_states["big"] == pane_states["small"] == "IN_WINDOW":
        state = "IN_WINDOW"
    elif big_delta_target < -FORMING_WINDOW_HALF_C or small_delta_target < -FORMING_WINDOW_HALF_C:
        state = "HARD"
    else:
        state = "SOFT"

    warnings: list[dict[str, Any]] = []
    suggestions: list[str] = []
    front_deltas = [zone_deltas.get(f"z{i:02d}", 0.0) for i in range(1, 5)]
    if front_deltas and _mean(front_deltas) > 4.0:
        warnings.append({"code": "FRONT_ZONE_OVERHEAT", "level": "WARN", "text": "Z01-Z04相对参考配方整体抬高较多，前段过早软化/辊道波纹风险上升。"})
    max_zone = max(zone_deltas.items(), key=lambda x: abs(x[1])) if zone_deltas else (None, 0.0)
    if max_zone[0] and abs(max_zone[1]) > 8.0:
        warnings.append({"code": "ZONE_LARGE_SHIFT", "level": "WARN", "text": f"{max_zone[0].upper()} 相对参考变化 {max_zone[1]:+.1f}℃，首轮调整幅度偏大。"})
    max_conv = 0.0
    max_conv_zone = None
    for z, d in conv_deltas.items():
        m = max(abs(d["big"]), abs(d["small"]))
        if m > max_conv:
            max_conv, max_conv_zone = m, z
    if max_conv > 12.0:
        warnings.append({"code": "CONVECTION_LARGE_SHIFT", "level": "WARN", "text": f"{str(max_conv_zone).upper()} 大小片对流相对参考变化超过12个开度点，建议分步验证。"})
    if abs(big_estimated - small_estimated) > 4.0:
        warnings.append({"code": "BIG_SMALL_THERMAL_MISMATCH", "level": "WARN", "text": f"大小片预计热状态相差 {abs(big_estimated-small_estimated):.1f}℃，应优先用独立对流缩小差异。"})
    if pane_states["big"] != "IN_WINDOW":
        warnings.append({"code":"BIG_TARGET_MISS","level":"WARN","text":f"大片预计 {big_estimated:.1f}℃，相对自身成型目标 {big_target_c:.1f}℃ 偏差 {big_delta_target:+.1f}℃。"})
    if pane_states["small"] != "IN_WINDOW":
        warnings.append({"code":"SMALL_TARGET_MISS","level":"WARN","text":f"小片预计 {small_estimated:.1f}℃，相对自身成型目标 {small_target_c:.1f}℃ 偏差 {small_delta_target:+.1f}℃。"})

    if state == "HARD":
        suggestions.append("预计达成点仍偏硬：优先检查Z05-Z09局部热场与大小片对流，不建议直接整体拉高12区炉温。")
    elif state == "SOFT":
        suggestions.append("预计达成点进入偏软侧：优先回退过高对流/中后段热输入，防止模布印痕、波纹和塌陷敏感性上升。")
    else:
        suggestions.append("预计达成点进入目标VFT等效成型窗口：保持候选值，转向真实出炉温度空间分布与质量结果验证。")
    if not warnings:
        warnings.append({"code": "NO_MAJOR_MODEL_WARNING", "level": "INFO", "text": "当前候选调整未触发大幅拉温、过量对流或大小片热状态分离警戒。"})

    ref_mean_sv = _mean(list(ref_z.values())) if ref_z else target_forming_temp_c
    cand_mean_sv = _mean([cand_z.get(z, v) for z, v in ref_z.items()]) if ref_z else ref_mean_sv
    radiation_load_ratio = radiation_ratio(cand_mean_sv, ref_mean_sv)
    ref_conv_values = [abs(v[k]) for v in ref_c.values() for k in ("big", "small")]
    cand_conv_values = []
    for z, refv in ref_c.items():
        d = conv_deltas.get(z, {"big": 0.0, "small": 0.0})
        cand_conv_values.extend([max(0.0, refv["big"] + d["big"]), max(0.0, refv["small"] + d["small"])])
    ref_conv_sum = sum(ref_conv_values)
    convection_load_proxy = (sum(cand_conv_values) / ref_conv_sum) if ref_conv_sum > 1e-9 else 1.0

    chart = []
    for row in vft_curve(center_temp_c=float(target_forming_temp_c), span_c=55, step_c=1):
        chart.append({
            "temp_c": row["temp_c"],
            "vft_log10_eta": row["log10_eta"],
            "relative_viscosity_to_target": row["relative_viscosity_to_target"],
            "forming_response_index": row.get("forming_response_index"),
            "radiation_ratio_to_target": round(radiation_ratio(row["temp_c"], float(target_forming_temp_c)), 5),
        })

    # 给UI的“智能解释”直接由当前候选状态生成，不依赖固定页面文案。
    lowe_note = []
    if str(big_material).lower() == "lowe": lowe_note.append("大片Low-E")
    if str(small_material).lower() == "lowe": lowe_note.append("小片Low-E")
    material_note = "、".join(lowe_note) if lowe_note else "大小片均为非镀膜片"
    if state == "HARD":
        ai_summary = f"候选工艺仍偏硬 {abs(delta_to_target):.1f}℃。当前{material_note}；共享炉温贡献约 {furnace_effect:+.1f}℃，对流贡献约 {common_conv_effect:+.1f}℃。优先用最少必要的局部热输入补偿，避免整炉拉温。"
    elif state == "SOFT":
        ai_summary = f"候选工艺偏软 {abs(delta_to_target):.1f}℃。继续整体抬温的收益有限，辐射热负荷与对流负荷都会增加，应优先回退过量局部热输入。"
    else:
        ai_summary = f"候选工艺已进入目标窗口，预计达成 {estimated:.1f}℃。辐射负荷代理 {radiation_load_ratio:.3f}×、对流负荷代理 {convection_load_proxy:.3f}×；下一步重点验证大小片热状态差与真实出炉温度六点均匀性。"

    return {
        "status": "DEVELOPMENT_VALIDATION",
        "estimated_achieved_temp_c": round(estimated, 2),
        "estimated_big_temp_c": round(big_estimated, 2),
        "estimated_small_temp_c": round(small_estimated, 2),
        "target_forming_temp_c": round(float(target_forming_temp_c), 2),
        "pane_targets": {"big_c": round(big_target_c,2), "small_c": round(small_target_c,2), "reference_pane": pair_target.get("reference_pane")},
        "pane_states": pane_states,
        "target_log10_eta": DEFAULT_FORMING_LOG10_VISCOSITY,
        "window_low_c": round(float(target_forming_temp_c) - FORMING_WINDOW_HALF_C, 2),
        "window_high_c": round(float(target_forming_temp_c) + FORMING_WINDOW_HALF_C, 2),
        "delta_to_target_c": round(delta_to_target, 2),
        "forming_state": state,
        "estimated_log10_eta": round(DEFAULT_VFT_MODEL.log10_viscosity(estimated), 4),
        "effects": {
            "furnace_effect_c": round(furnace_effect, 2),
            "big_convection_effect_c": round(big_conv_effect, 2),
            "small_convection_effect_c": round(small_conv_effect, 2),
            "convection_effect_c": round(common_conv_effect, 2),
            "loading_gap_effect_c": round(gap_effect, 2),
            "weighted_furnace_delta_c": round(weighted_furnace_delta, 2),
        },
        "thermal_load": {"big": big_load, "small": small_load},
        "radiation_load_ratio_vs_reference": round(radiation_load_ratio, 4),
        "convection_load_proxy_vs_reference": round(convection_load_proxy, 4),
        "cost_tradeoff": {
            "radiation_proxy": round(radiation_load_ratio, 4),
            "convection_proxy": round(convection_load_proxy, 4),
            "note": "均为相对参考配方的开发验证负荷代理，不等同于电表实测kWh。",
        },
        "warnings": warnings,
        "suggestions": suggestions,
        "ai_summary": ai_summary,
        "chart": chart,
        "model_note": "统一VFT/热响应开发验证模型用于方向性实时反馈；它不替代真实出炉温度，也不自动写PLC。",
    }
