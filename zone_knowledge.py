from __future__ import annotations

from statistics import mean, median
from typing import Any

from settings import DATATEMP_FORMING_WINDOW_C, OUTLET_RANGE_WARN_C

# V3.4 原有温区认知全部保留，只在其上增加“证据→机理→风险→建议”的失效机理层。
ZONE_KNOWLEDGE: dict[str, dict[str, Any]] = {
    "z01": {"role": "预热初始段", "stage": "预热爬升", "hardware": "Basic对流", "mission": "建立温升起点并控制热冲击，避免玻璃表层突然升温。", "watch": "关注入口热冲击、上下/左右受热一致性；速度未知时用相邻温区热场梯度代替时间升温率。"},
    "z02": {"role": "预热爬升段", "stage": "预热爬升", "hardware": "Basic对流", "mission": "继续建立玻璃整体热量并保持平缓爬升。", "watch": "关注Z01→Z04温升是否出现突跃，防止表层与板芯热差扩大。"},
    "z03": {"role": "预热过渡段", "stage": "预热爬升", "hardware": "Basic对流", "mission": "从表层升温过渡到厚度方向透热，为主热段建立入口热状态。", "watch": "厚玻/Low-E若在前段热量进入不足，后段可能被迫通过高温或高对流补偿。"},
    "z04": {"role": "预热收尾段", "stage": "预热爬升", "hardware": "Basic对流", "mission": "完成前段预热并为强热段建立稳定入口。", "watch": "避免为了追温把前区整体抬得过高，防止热应力或过早软化后形成辊道波筋。"},
    "z05": {"role": "强热注入入口", "stage": "强热注入", "hardware": "LCR对流", "mission": "进入主要热量注入阶段，利用LCR对流补偿厚度与低辐射膜面的吸热差异。", "watch": "关注是否出现‘共享炉温持续抬高、对流补偿不足’的辐射强攻迹象。"},
    "z06": {"role": "主热蓄能段", "stage": "强热注入", "hardware": "LCR对流", "mission": "持续向玻璃输入热量，使板芯热状态向成型区推进。", "watch": "若出炉偏硬而炉温已高于发布基线，应优先检查热量是否真正进入玻璃及对流有效性。"},
    "z07": {"role": "软化推进核心段", "stage": "强热注入", "hardware": "LCR对流", "mission": "承担主要软化推进，是厚玻/Low-E差异化补热的重要区域。", "watch": "避免为了Low-E吸热瓶颈继续死拉辐射温度，使白玻/下表面提前过软并放大辊道波筋。"},
    "z08": {"role": "软化收敛段", "stage": "强热注入", "hardware": "LCR对流", "mission": "让玻璃热状态向成型窗口收敛，并控制大小片热状态差。", "watch": "过度补热会让出炉端偏软并增加模布印痕、塌陷与PMD敏感性。"},
    "z09": {"role": "强热退出/均温衔接", "stage": "强热注入", "hardware": "Basic对流", "mission": "平滑退出强热注入，避免进入无对流区前热状态突变。", "watch": "作为最后一个有对流的温区，调节应克制，并结合出炉温度验证。"},
    "z10": {"role": "均温成型起始", "stage": "均温成型", "hardware": "无对流", "mission": "停止强制对流后依靠共同热场进行均热与表里热状态收敛。", "watch": "重点看出炉温度是否偏热/偏硬和六点均匀性，不应再用对流补偿。"},
    "z11": {"role": "出炉稳态保持", "stage": "均温成型", "hardware": "无对流", "mission": "维持出炉前稳定热状态，减少前置热输入波动传递到压制工位。", "watch": "出炉偏热需防模布印痕；出炉偏硬需防压制抗力、回弹和微裂纹。"},
    "z12": {"role": "成型准备锚点", "stage": "均温成型", "hardware": "无对流", "mission": "形成进入压台前的最终热边界。", "watch": "以出炉温度六点和N01/压制结果做闭环验证，不把单次结果归因到Z12一个区。"},
}


def _baseline_summary(baseline: dict[str, Any] | None) -> dict[str, Any] | None:
    if not baseline:
        return None
    return ((baseline.get("payload") or {}).get("captured_summary") or None)


def _baseline_zone(baseline: dict[str, Any] | None, z: str) -> dict[str, Any] | None:
    bsum = _baseline_summary(baseline)
    if not bsum:
        return None
    return ((bsum.get("furnace") or {}).get("zones") or {}).get(z)


def _baseline_conv(baseline: dict[str, Any] | None, z: str) -> dict[str, Any] | None:
    bsum = _baseline_summary(baseline)
    if not bsum:
        return None
    return ((bsum.get("convection") or {}).get("zones") or {}).get(z)


def _neighbor_gradient(zones: dict[str, Any], idx: int) -> float | None:
    if idx <= 1:
        return None
    cur = (zones.get(f"z{idx:02d}") or {}).get("avg_pv")
    prev = (zones.get(f"z{idx-1:02d}") or {}).get("avg_pv")
    if isinstance(cur, (int, float)) and isinstance(prev, (int, float)):
        return float(cur) - float(prev)
    return None


def _mean_numeric(values: list[Any]) -> float | None:
    vals = [float(v) for v in values if isinstance(v, (int, float))]
    return mean(vals) if vals else None


def zone_advisories(summary: dict[str, Any], baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    furnace = ((summary.get("furnace") or {}).get("zones") or {})
    convection = ((summary.get("convection") or {}).get("zones") or {})
    outlet = summary.get("datatemp") or {}
    mold = ((summary.get("mold") or {}).get("n01") or {})
    torque = summary.get("torque") or {}
    bsum = _baseline_summary(baseline)
    boutlet = (bsum or {}).get("datatemp") or {}
    out: dict[str, Any] = {}

    # 当前数据合同没有可靠玻璃速度/炉内驻留时间。因此02只把相邻区PV变化称为
    # “空间升温梯度代理”，绝不伪造℃/s；未来接入可靠速度后可升级为时间升温率。
    velocity_available = False

    preheat_grads = [g for i in range(2,5) if isinstance((g := _neighbor_gradient(furnace, i)), (int,float))]
    preheat_grad_center = median([abs(g) for g in preheat_grads]) if preheat_grads else None
    preheat_pwrs = [(furnace.get(f"z{i:02d}") or {}).get("avg_pwr") for i in range(1,5)]
    preheat_pwr_center = _mean_numeric(preheat_pwrs)

    main_svs = [(furnace.get(f"z{i:02d}") or {}).get("avg_sv") for i in range(5,10)]
    main_pwrs = [(furnace.get(f"z{i:02d}") or {}).get("avg_pwr") for i in range(5,10)]
    main_sv_center = _mean_numeric(main_svs)
    main_pwr_center = _mean_numeric(main_pwrs)
    main_conv_levels = []
    for i in range(5,10):
        c=(convection.get(f"z{i:02d}") or {})
        v=_mean_numeric([c.get("avg_sv_big"),c.get("avg_sv_small")])
        if v is not None: main_conv_levels.append(v)
    main_conv_center = _mean_numeric(main_conv_levels)

    outlet_hard = outlet_soft = outlet_nonuniform = False
    base_outlet_mean = boutlet.get("mean") if isinstance(boutlet.get("mean"),(int,float)) else None
    if outlet.get("available"):
        if isinstance(outlet.get("range"),(int,float)) and float(outlet["range"]) > OUTLET_RANGE_WARN_C:
            outlet_nonuniform = True
        if base_outlet_mean is not None and isinstance(outlet.get("mean"),(int,float)):
            d=float(outlet["mean"])-float(base_outlet_mean)
            outlet_hard = d < -DATATEMP_FORMING_WINDOW_C
            outlet_soft = d > DATATEMP_FORMING_WINDOW_C

    for idx in range(1, 13):
        z = f"z{idx:02d}"
        k = ZONE_KNOWLEDGE[z]
        row = furnace.get(z) or {}
        conv = convection.get(z) or {}
        sv, pv, pwr = row.get("avg_sv"), row.get("avg_pv"), row.get("avg_pwr")
        sv_pv_delta = (float(pv) - float(sv)) if isinstance(pv, (int, float)) and isinstance(sv, (int, float)) else None
        b = _baseline_zone(baseline, z)
        bc = _baseline_conv(baseline, z)
        baseline_delta = None
        if b and isinstance(pv, (int, float)) and isinstance(b.get("avg_pv"), (int, float)):
            baseline_delta = float(pv) - float(b["avg_pv"])
        spatial_grad = _neighbor_gradient(furnace, idx)
        base_grad = None
        if bsum and idx > 1:
            bz = ((bsum.get("furnace") or {}).get("zones") or {})
            base_grad = _neighbor_gradient(bz, idx)

        severity = "NORMAL"
        flags: list[str] = []
        if sv_pv_delta is not None:
            if abs(sv_pv_delta) >= 6:
                severity = "WARN"; flags.append(f"PV相对SV偏差 {sv_pv_delta:+.1f}℃")
            elif abs(sv_pv_delta) >= 3:
                severity = "ATTENTION"; flags.append(f"PV相对SV偏差 {sv_pv_delta:+.1f}℃")
        if baseline_delta is not None and abs(baseline_delta) >= 5:
            severity = "WARN"; flags.append(f"相对发布基线 {baseline_delta:+.1f}℃")

        evidence: list[str] = []
        mechanism: list[str] = []
        risks: list[str] = []
        actions: list[str] = []
        trigger = {"active": False, "name": "未触发明确失效机理预警", "score": 0, "basis": []}

        evidence.append(f"区级SV/PV/PWR：{sv if sv is not None else '--'} / {pv if pv is not None else '--'} / {pwr if pwr is not None else '--'}。")
        if spatial_grad is not None:
            evidence.append(f"与前一区PV均值的空间温差为 {spatial_grad:+.1f}℃；速度未知，因此只作为沿程升温梯度代理，不换算为℃/s。")
        if baseline_delta is not None:
            evidence.append(f"本区PV相对该品种发布基线变化 {baseline_delta:+.1f}℃。")
        else:
            evidence.append("当前没有该品种发布基线，绝对好/坏判断降级为机理解释、相邻区形态和后续结果证据。")

        if idx <= 4:
            mechanism.append("预热段的核心是让玻璃表层与板芯逐步建立热量。真正要防的是热冲击和过早进入低粘度区，而不是追求入口段尽快升到高温。")
            shape_trigger = False
            if spatial_grad is not None:
                if base_grad is not None and spatial_grad - base_grad > 8:
                    shape_trigger = True; trigger["basis"].append(f"空间梯度较发布基线增加 {spatial_grad-base_grad:+.1f}℃")
                elif preheat_grad_center is not None and abs(spatial_grad) > max(8.0, preheat_grad_center*1.60):
                    shape_trigger = True; trigger["basis"].append(f"本级梯度 {spatial_grad:+.1f}℃ 显著高于当前预热段典型梯度")
            pwr_trigger = isinstance(pwr,(int,float)) and isinstance(preheat_pwr_center,(int,float)) and float(pwr) > preheat_pwr_center + 12
            if pwr_trigger: trigger["basis"].append("本区PWR明显高于预热段平均负荷")
            if shape_trigger or (pwr_trigger and idx>1 and isinstance(spatial_grad,(int,float)) and spatial_grad>0):
                severity = "WARN"; flags.append("预热爬升过急代理预警")
                trigger.update(active=True,name="预热升温过急 / 热冲击演化",score=2 if shape_trigger and pwr_trigger else 1)
                risks.append("表层升温快于板芯会放大厚度方向热应力，冷端存在炸裂/暗裂风险；若前段过早软化，玻璃在后续陶瓷辊道上的自重周期变形会增加早期波筋敏感性。")
                actions.append("优先压平Z01-Z04的阶梯爬升，减少局部高PWR/高设定突跃；不要用前区猛升温去补偿后段偏硬。")
            else:
                risks.append("当前未触发自适应爬升异常，但仍需持续看预热段空间梯度是否突然变陡，以及后续出炉温度是否出现方向性不均。")
                actions.append("维持平滑爬升；有发布基线时优先比较梯度形态，没有速度数据时不输出虚假的升温率。")

        elif idx <= 9:
            mechanism.append("强热段的目标是把热量真正送进玻璃。Low-E/低辐射膜面会削弱辐射吸收；对流是绕开光学反射的独立通道，但对流本身也有能耗成本。")
            cb, cs, cp = conv.get("avg_sv_big"), conv.get("avg_sv_small"), conv.get("avg_pv")
            conv_avg = _mean_numeric([cb,cs])
            evidence.append(f"对流：sv_big={cb if cb is not None else '--'}，sv_small={cs if cs is not None else '--'}，PV={cp if cp is not None else '--'}；没有激活通道信号时不强行认定PV应跟随哪一路SV。")
            radiation_score = 0
            if baseline_delta is not None and baseline_delta > 5:
                radiation_score += 1; trigger["basis"].append("炉温PV高于发布基线")
            if isinstance(sv,(int,float)) and isinstance(main_sv_center,(int,float)) and float(sv) > main_sv_center + 8:
                radiation_score += 1; trigger["basis"].append("本区SV高于主热段平均水平")
            if isinstance(pwr,(int,float)) and isinstance(main_pwr_center,(int,float)) and float(pwr) > main_pwr_center + 10:
                radiation_score += 1; trigger["basis"].append("PWR高于主热段平均负荷")
            conv_weak = False
            if bc:
                cur_avg_sv = conv_avg
                base_avg_sv = _mean_numeric([bc.get("avg_sv_big"), bc.get("avg_sv_small")])
                if cur_avg_sv is not None and base_avg_sv is not None and cur_avg_sv < base_avg_sv - 5:
                    conv_weak = True; trigger["basis"].append("对流设定低于发布基线")
            elif conv_avg is not None and main_conv_center is not None and conv_avg < main_conv_center*0.72:
                conv_weak = True; trigger["basis"].append("本区对流明显低于主热段典型水平")
            if outlet_hard:
                trigger["basis"].append("后续出炉均温仍位于发布基线偏硬侧")

            if radiation_score >= 2 and (conv_weak or outlet_hard):
                severity = "WARN"; flags.append("疑似辐射强攻 / 膜层吸热瓶颈")
                trigger.update(active=True,name="辐射强攻 + 热量透入不足",score=radiation_score + int(conv_weak) + int(outlet_hard))
                risks.append("如果当前品种含Low-E，现有证据符合‘辐射热被膜层削弱→操作员继续抬共享炉温/PWR→易吸热表面或非镀膜片提前过软’的演化路径，陶瓷辊道周期接触会放大PMD/波筋风险。")
                actions.append("先确认产品膜层属性与对流通道，再评估恢复/增加对应大片或小片对流；除非综合能耗模型证明更优，不建议继续整炉拉温。")
            elif outlet_nonuniform and radiation_score >= 1:
                severity = "ATTENTION" if severity == "NORMAL" else severity; flags.append("强热段输入与出炉不均同时出现")
                trigger.update(active=True,name="强热输入与出炉空间不均联合关注",score=radiation_score+1)
                risks.append("强热段热输入与出炉六点不均同时出现，说明问题可能不是‘平均温度不足’，而是热量分配/对流空间不均。")
                actions.append("优先检查LCR分布、大小片对流和出炉六点方向，不要用整体升温掩盖空间不均。")
            else:
                risks.append("主要双边界：热量透入不足会造成出炉偏硬；共享炉温过高而膜层/对流策略不匹配，则易吸热一侧可能提前软化并形成辊道波筋。")
                actions.append("用出炉温度均温+极差验证热量是否真正进入玻璃；有Low-E时优先比较独立对流与稳定基线。")

        else:
            mechanism.append("Z10-Z12是均温成型段：停止强制对流，让前段已注入的热量继续向板芯均化，并锁定进入N01压台前的最终粘度边界。")
            if outlet.get("available"):
                om = outlet.get("mean"); rg = outlet.get("range")
                evidence.append(f"最近出炉温度均温 {float(om):.1f}℃、极差 {float(rg):.1f}℃；该结果属于12区共同作用，不能归因于单一区。")
                if outlet_soft:
                    severity = "WARN"; flags.append("出炉偏热 / 模痕边界")
                    trigger.update(active=True,name="出炉过热 → 模布印痕/塌陷边界",score=2,basis=trigger["basis"]+["出炉均温高于发布基线窗口"])
                    risks.append("进入压台时表面粘度偏低，对模布纹理、局部接触压力和真空吸附更敏感，模布印痕/局部塌陷风险上升。")
                    actions.append("优先回退Z10-Z12或前置强热段的过量热输入，并确认N01模温是否正常；不要继续增加对流。")
                elif outlet_hard:
                    severity = "WARN"; flags.append("出炉偏硬 / 微裂边界")
                    trigger.update(active=True,name="出炉偏硬 → 压制抗力/微裂边界",score=2,basis=trigger["basis"]+["出炉均温低于发布基线窗口"])
                    risks.append("玻璃贴模能力下降，压制抗力、回弹、边缘/孔区微裂与曲面吻合风险上升。")
                    # 如果N01接近设定，热端原因优先级更高；否则先排除模具。
                    if mold.get("available") and mold.get("near_setpoint"):
                        actions.append("N01当前接近设定，先检查前置热量是否真正进入玻璃，再做局部热输入增量；不要直接把Z10-Z12大幅拉高。")
                    else:
                        actions.append("先同时核对N01热平衡与前置炉温/对流，避免把模具问题误判为炉温不足。")
                elif outlet_nonuniform:
                    severity = "ATTENTION" if severity == "NORMAL" else severity; flags.append("出炉极差偏大")
                    trigger.update(active=True,name="出炉均温可接受但空间软硬不一致",score=1,basis=trigger["basis"]+["六点极差超过当前软件参考阈值"])
                    risks.append("平均温度可能看似正常，但空间热差会造成局部贴模与压制负荷不一致。")
                    actions.append("优先按六点方向追溯Z05-Z12热场/对流分布，并与4轴压制左右/上下差交叉验证。")
                else:
                    risks.append("出炉均温与空间均匀性未触发当前模型警戒，下一步重点转向N01热平衡、4轴压制/真空和退火上下差。")
                    actions.append("保持当前均温段附近运行，持续用出炉温度→N01→压制/真空的证据链验证。")
            else:
                risks.append("当前缺少有效出炉温度事件，不能对过热模痕/偏硬微裂做绝对双向边界判定。")
                actions.append("等待有效出炉温度六点事件，并结合N01与压制结果做联合判断。")

        # 设备跟踪/基线偏差本身已达到关注或预警级时，Trigger与顶部Severity保持一致，
        # 避免界面出现“红色预警但触发器显示未触发”的认知冲突。该类Trigger只说明过程偏差，
        # 不自动等同于某一种质量失效机理。
        if not trigger.get("active") and severity in {"ATTENTION", "WARN"} and flags:
            trigger.update(
                active=True,
                name="温区跟踪/基线偏差需确认",
                score=1 if severity == "ATTENTION" else 2,
                basis=list(flags),
            )
        if not risks:
            risks.append("当前未触发明确失效机理警戒，继续以发布基线与后续结果验证。")
        if not actions:
            actions.append(k["watch"])

        reasoning = [f"{k['role']}的主要任务：{k['mission']}"] + evidence + mechanism + risks
        action = actions[0]
        out[z] = {
            "zone": z.upper(), "role": k["role"], "stage": k["stage"], "hardware": k["hardware"],
            "mission": k["mission"], "watch": k["watch"], "severity": severity, "flags": flags,
            "current": {
                "avg_sv": sv, "avg_pv": pv, "avg_pwr": pwr,
                "sv_pv_delta": round(sv_pv_delta, 2) if sv_pv_delta is not None else None,
                "baseline_pv_delta": round(baseline_delta, 2) if baseline_delta is not None else None,
                "zone_to_zone_delta_c": round(spatial_grad, 2) if spatial_grad is not None else None,
                "baseline_zone_to_zone_delta_c": round(base_grad, 2) if base_grad is not None else None,
            },
            "convection": {"type": conv.get("type"), "avg_sv_big": conv.get("avg_sv_big"), "avg_sv_small": conv.get("avg_sv_small"), "avg_pv": conv.get("avg_pv"), "valve_count": conv.get("valve_count")},
            "gradient_mode": "TIME_RATE" if velocity_available else "ZONE_SPATIAL_PROXY",
            "trigger_assessment": trigger,
            "failure_mechanism": {
                "evidence": evidence, "mechanism": mechanism, "risks": risks, "actions": actions,
                "confidence_note": "基于当前快照、相邻区热场、发布基线与后续出炉结果的过程证据；无可靠速度时不输出℃/s。",
            },
            "reasoning": reasoning, "action": action,
        }
    return out
