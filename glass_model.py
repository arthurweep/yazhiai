from __future__ import annotations

"""统一玻璃成型机理内核（V3.4）。

设计目标：
1. 把“材料内禀VFT”与“厚度/Low-E造成的吸热过程差异”分开。
2. 不把厚度或Low-E简单等同为“软化点大幅升高”。
3. 在缺少厂内玻璃成分与正式VFT系数时，提供可追溯、可替换的参考VFT形态模型；
   一旦获得材料证书/实验拟合参数，只替换参数，不改页面与API。
4. 所有在线/新品种页面共享同一套计算，避免各页面各自写公式。
"""

import math
from dataclasses import dataclass
from typing import Any

KELVIN_OFFSET = 273.15
REFERENCE_THICKNESS_MM = 2.1
DEFAULT_FORMING_LOG10_VISCOSITY = 7.0
DEFAULT_REFERENCE_FORMING_TEMP_C = 640.0
DEFAULT_VFT_C_K = 450.0
DEFAULT_LOCAL_SLOPE_DECADE_PER_C = -0.040
FORMING_WINDOW_HALF_C = 5.0


@dataclass(frozen=True)
class VFTReferenceModel:
    """参考VFT形态。

    由于当前资料未给出广州现场所用玻璃成分对应的A/B/C正式系数，V3.4不伪造“材料证书级”参数。
    这里用一个可解释的局部参考模型：指定成型参考点、局部斜率和C，再反算A/B，使曲线满足
    log10(eta)=A+B/(T[K]-C)。

    因此该模型能正确表达VFT的非线性/陡峭性，但绝对数值仍需现场材料/实验标定。
    """

    reference_temp_c: float = DEFAULT_REFERENCE_FORMING_TEMP_C
    reference_log10_eta: float = DEFAULT_FORMING_LOG10_VISCOSITY
    c_k: float = DEFAULT_VFT_C_K
    local_slope_decade_per_c: float = DEFAULT_LOCAL_SLOPE_DECADE_PER_C

    @property
    def coefficients(self) -> tuple[float, float, float]:
        t0 = self.reference_temp_c + KELVIN_OFFSET
        distance = t0 - self.c_k
        if distance <= 0:
            raise ValueError("VFT参考参数无效：T-C必须大于0")
        # d(logη)/dT = -B/(T-C)^2
        b = -self.local_slope_decade_per_c * distance * distance
        a = self.reference_log10_eta - b / distance
        return a, b, self.c_k

    def log10_viscosity(self, temp_c: float) -> float:
        a, b, c = self.coefficients
        t = float(temp_c) + KELVIN_OFFSET
        if t <= c + 1e-6:
            return 20.0
        return a + b / (t - c)

    def temperature_for_log10_viscosity(self, log10_eta: float) -> float:
        a, b, c = self.coefficients
        den = float(log10_eta) - a
        if abs(den) < 1e-9:
            raise ValueError("VFT反解失败：目标粘度与模型渐近值冲突")
        return b / den + c - KELVIN_OFFSET


DEFAULT_VFT_MODEL = VFTReferenceModel()


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def thermal_load_index(thickness_mm: float, material: str) -> dict[str, Any]:
    """给出相对热负荷/对流依赖指数，不把它伪装成材料常数。

    厚度项来自单位面积热容量近似与厚度成正比；Low-E项只作为开发验证阶段的“辐射受限权重”，
    其具体值后续必须由膜系/表面方向/实测热响应标定。
    """
    thickness = _clip(thickness_mm, 1.0, 8.0)
    thickness_ratio = thickness / REFERENCE_THICKNESS_MM
    is_lowe = str(material).lower() == "lowe"
    # 不是“85%反射率”等虚构现场材料参数，只是求解器中可替换的相对权重。
    radiation_response_factor = 0.78 if is_lowe else 1.0
    radiation_limited_load = thickness_ratio / radiation_response_factor
    convection_dependency = 1.30 if is_lowe else 1.0
    cooling_sensitivity = REFERENCE_THICKNESS_MM / thickness
    return {
        "thickness_ratio": round(thickness_ratio, 4),
        "radiation_response_factor": round(radiation_response_factor, 4),
        "heat_load_index": round(radiation_limited_load, 4),
        "convection_dependency_index": round(convection_dependency, 4),
        "cooling_sensitivity_index": round(cooling_sensitivity, 4),
        "material": "lowe" if is_lowe else "normal",
        "model_status": "DEVELOPMENT_VALIDATION",
    }


def single_glass_target(
    *,
    thickness_mm: float,
    material: str,
    target_log10_eta: float = DEFAULT_FORMING_LOG10_VISCOSITY,
    validated_surface_target_c: float | None = None,
    vft: VFTReferenceModel = DEFAULT_VFT_MODEL,
) -> dict[str, Any]:
    """计算单片玻璃的成型目标。

    intrinsic_vft_target_c：由VFT和目标粘度反解，是材料内禀流变目标。
    recommended_measured_target_c：给现场DataTemp/表面测温使用的建议靶心。

    厚度不会改变同一基片玻璃的VFT本体；当没有已验证基线时，仅允许很小的“表面-芯部热滞后代理”修正。
    Low-E不直接改VFT目标，而是提高热负荷与对流依赖。
    """
    thickness = _clip(thickness_mm, 1.0, 8.0)
    intrinsic = vft.temperature_for_log10_viscosity(target_log10_eta)
    load = thermal_load_index(thickness, material)
    # 厚度只用于“表面测温靶心”轻量修正，不改同一基片玻璃的内禀VFT。
    # 即使存在已验证基线，也把基线作为参考厚度锚点，而不是让新品种输入完全失去联动。
    thickness_correction = _clip((thickness - REFERENCE_THICKNESS_MM) * 1.2, -1.5, 2.5)
    if isinstance(validated_surface_target_c, (int, float)):
        recommended = float(validated_surface_target_c) + thickness_correction
        target_source = "已验证产品DataTemp/工艺基线锚点 + 厚度热滞后轻量修正"
        measurement_correction = recommended - intrinsic
        status = "BASELINE_ANCHORED"
    else:
        recommended = intrinsic + thickness_correction
        measurement_correction = thickness_correction
        target_source = "参考VFT成型粘度 + 表面/芯部热滞后轻量修正"
        status = "REFERENCE_MODEL_UNCALIBRATED"

    return {
        "material": load["material"],
        "thickness_mm": round(thickness, 3),
        "target_log10_eta": round(float(target_log10_eta), 3),
        "intrinsic_vft_target_c": round(intrinsic, 2),
        "recommended_measured_target_c": round(recommended, 2),
        "measurement_correction_c": round(measurement_correction, 2),
        "target_source": target_source,
        "status": status,
        "thermal": load,
        "explanation": [
            "VFT描述玻璃粘度随绝对温度的非线性变化；目标成型粘度反解得到材料内禀温度。",
            "厚度主要改变单位面积热容量与热响应时间；Low-E主要削弱辐射吸收并提高强制对流依赖。",
            "因此厚度/Low-E首先改变‘怎样达到成型热状态’，而不是把同一种玻璃的内禀VFT软化温度简单抬高。",
        ],
    }


def pair_forming_target(
    *,
    big_thickness_mm: float,
    small_thickness_mm: float,
    big_material: str,
    small_material: str,
    validated_target_c: float | None = None,
    target_log10_eta: float = DEFAULT_FORMING_LOG10_VISCOSITY,
) -> dict[str, Any]:
    big = single_glass_target(
        thickness_mm=big_thickness_mm,
        material=big_material,
        target_log10_eta=target_log10_eta,
        validated_surface_target_c=validated_target_c,
    )
    small = single_glass_target(
        thickness_mm=small_thickness_mm,
        material=small_material,
        target_log10_eta=target_log10_eta,
        validated_surface_target_c=validated_target_c,
    )
    # V3.4.4：共享炉温首先服从厚度热负荷。
    # 谁更厚，谁就是共享12区阶梯热场的主要求解依据；镀膜属性不改变基准片选择，
    # Low-E 的辐射吸收不足优先由该片独立 sv_big / sv_small 对流补偿。
    candidates = [
        ("big", str(big_material).lower(), float(big_thickness_mm), big),
        ("small", str(small_material).lower(), float(small_thickness_mm), small),
    ]
    reference = max(candidates, key=lambda x: (x[2], 1 if x[0] == "big" else 0))
    reference_pane, _, _, reference_model = reference
    shared = float(reference_model["recommended_measured_target_c"])
    pane_cn = "大片" if reference_pane == "big" else "小片"
    if isinstance(validated_target_c, (int, float)):
        source = f"已验证运行基线锚点 + {pane_cn}厚片热负荷修正"
    else:
        source = f"共享炉温求解基准：{pane_cn}（厚度优先；Low-E由独立对流补偿）"

    return {
        "suggested_target_forming_temp_c": round(shared, 1),
        "big_target_c": round(float(big["recommended_measured_target_c"]), 1),
        "small_target_c": round(float(small["recommended_measured_target_c"]), 1),
        "reference_pane": reference_pane,
        "reference_basis": source,
        "window_half_width_c": FORMING_WINDOW_HALF_C,
        "target_log10_eta": target_log10_eta,
        "source": source,
        "editable": True,
        "status": "BASELINE_ANCHORED" if validated_target_c is not None else "REFERENCE_MODEL_UNCALIBRATED",
        "big": big,
        "small": small,
        "pair_heat_load_gap": round(big["thermal"]["heat_load_index"] - small["thermal"]["heat_load_index"], 4),
        "notes": [
            "共享12区炉温首先以更厚片建立阶梯热场基线；镀膜属性不改变基准片选择。",
            "大片/小片各自保留独立成型目标；Low-E优先由对应sv_big/sv_small对流补偿，非镀膜默认不开新增对流。",
            "工艺员可以修改共享求解基准；修改后候选阶梯炉温、对流、VFT图和风险判断立即重算。",
        ],
    }


def vft_curve(
    *,
    center_temp_c: float,
    span_c: float = 45.0,
    step_c: float = 1.0,
    target_log10_eta: float = DEFAULT_FORMING_LOG10_VISCOSITY,
    vft: VFTReferenceModel = DEFAULT_VFT_MODEL,
) -> list[dict[str, float]]:
    start = float(center_temp_c) - float(span_c)
    end = float(center_temp_c) + float(span_c)
    n = int(round((end - start) / step_c))
    target_eta = 10 ** target_log10_eta
    out = []
    for i in range(n + 1):
        t = start + i * step_c
        log_eta = vft.log10_viscosity(t)
        # 相对目标粘度倍率用于UI解释；截断防止图表数值爆炸。
        ratio = _clip((10 ** log_eta) / target_eta, 0.001, 1000.0)
        # 成型响应指数只用于把VFT跨数量级的粘度变化直观化：logη高于目标时接近0，
        # 低于目标时快速趋近100。它不是新的材料常数。
        flow_response = 100.0 / (1.0 + 10.0 ** (log_eta - target_log10_eta))
        out.append({
            "temp_c": round(t, 2),
            "log10_eta": round(log_eta, 5),
            "relative_viscosity_to_target": round(ratio, 5),
            "forming_response_index": round(flow_response, 4),
        })
    return out


def radiation_ratio(temp_c: float, reference_temp_c: float, ambient_c: float = 25.0) -> float:
    t = float(temp_c) + KELVIN_OFFSET
    tr = float(reference_temp_c) + KELVIN_OFFSET
    ta = float(ambient_c) + KELVIN_OFFSET
    den = tr**4 - ta**4
    if den <= 0:
        return 1.0
    return max(0.0, (t**4 - ta**4) / den)


def radiation_curve(reference_temp_c: float, span_high_c: float = 90.0, step_c: float = 2.0) -> list[dict[str, float]]:
    start = float(reference_temp_c)
    end = start + float(span_high_c)
    n = int(round((end - start) / step_c))
    return [
        {
            "temp_c": round(start + i * step_c, 2),
            "radiation_ratio": round(radiation_ratio(start + i * step_c, reference_temp_c), 6),
            "absolute_temperature_ratio": round(((start + i * step_c + KELVIN_OFFSET) / (reference_temp_c + KELVIN_OFFSET)), 6),
        }
        for i in range(n + 1)
    ]


def mechanism_estimate(
    *,
    thickness_mm: float,
    material: str,
    target_override_c: float | None = None,
    test_temp_c: float | None = None,
    validated_target_c: float | None = None,
) -> dict[str, Any]:
    single = single_glass_target(
        thickness_mm=thickness_mm,
        material=material,
        validated_surface_target_c=validated_target_c,
    )
    suggested = float(single["recommended_measured_target_c"])
    target = float(target_override_c) if isinstance(target_override_c, (int, float)) else suggested
    test_temp = float(test_temp_c) if isinstance(test_temp_c, (int, float)) else target
    log_eta = DEFAULT_VFT_MODEL.log10_viscosity(test_temp)
    delta = test_temp - target
    state = "IN_WINDOW" if abs(delta) <= FORMING_WINDOW_HALF_C else ("HARD" if delta < 0 else "SOFT")
    rad_ratio = radiation_ratio(test_temp, target)
    a, b, c = DEFAULT_VFT_MODEL.coefficients

    if state == "HARD":
        ai = "当前测试点位于成型窗口偏硬侧。优先判断热量是否真正进入玻璃，并结合厚度/Low-E选择局部对流补偿，不建议仅凭偏硬就整体拉高炉温。"
    elif state == "SOFT":
        ai = "当前测试点位于偏软侧。继续增加热输入会降低粘度并增加波纹、模布印痕与塌陷敏感性，应优先回退过量热输入。"
    else:
        ai = "当前测试点位于VFT等效成型窗口。此时应减少大幅调参，转向验证DataTemp空间均匀性、模具状态与实际质量结果。"

    thermal = single["thermal"]
    is_lowe = str(material).lower() == "lowe"
    thickness = float(thickness_mm)
    feature_explanations = {
        "target": (
            f"参考VFT把目标粘度 logη={DEFAULT_FORMING_LOG10_VISCOSITY:.1f} 反解为材料内禀成型温度；"
            f"当前厚度 {thickness:.1f}mm 只通过表面/板芯热滞后做轻量修正，不把厚度误当成材料VFT变化。"
        ),
        "intrinsic_vft": "VFT描述玻璃粘度随绝对温度的非线性变化。越接近成型区，少量温升就会跨越更大的粘度数量级，因此压制窗口比普通温控窗口更敏感。",
        "heat_load": f"当前相对热负荷指数 {thermal['heat_load_index']:.2f}×。厚度增加意味着单位面积玻璃质量/热容量增加，板芯追随表面的时间更长。",
        "convection": (
            f"当前对流补偿依赖 {thermal['convection_dependency_index']:.2f}×。" +
            ("Low-E膜面削弱辐射吸热，因此强制对流是绕开光学反射的重要独立热通道；但对流本身也耗能，生产求解不能无限加大。" if is_lowe else "普通白玻辐射热响应相对直接，正常情况下不应为了追求速度无条件增加对流。")
        ),
        "cooling": f"出炉冷却敏感性指数 {thermal['cooling_sensitivity_index']:.2f}×。薄片热惯性小，离炉到合模之间更容易快速变硬；厚片则更需要关注板芯透热。",
        "viscosity": f"目标粘度为 logη {DEFAULT_FORMING_LOG10_VISCOSITY:.1f}。这里是开发验证成型参考，最终应由产品曲率、压制时间与质量结果标定。",
    }

    material_story = (
        f"Low-E镀膜并不会把同一基片玻璃的内禀VFT软化温度直接抬高。它主要削弱辐射热进入玻璃的效率，使加热机制更依赖强制对流。当前厚度 {thickness:.1f}mm，讲解模型建议表面测温靶心为 {target:.1f}℃；该页只用于机理讲解，不带入03新品种求解。"
        if is_lowe else
        f"普通白玻辐射热响应相对直接。当前厚度 {thickness:.1f}mm，厚度差异主要体现在单位面积热容量、板芯透热时间和出炉冷却速度；讲解模型建议表面测温靶心为 {target:.1f}℃。该页状态不带入03新品种求解。"
    )

    return {
        "status": single["status"],
        "single_glass": single,
        "suggested_target_c": round(suggested, 2),
        "active_target_c": round(target, 2),
        "test_temp_c": round(test_temp, 2),
        "forming_state": state,
        "window_low_c": round(target - FORMING_WINDOW_HALF_C, 2),
        "window_high_c": round(target + FORMING_WINDOW_HALF_C, 2),
        "test_log10_eta": round(log_eta, 4),
        "radiation_ratio_vs_target": round(rad_ratio, 5),
        "vft_curve": vft_curve(center_temp_c=target),
        "radiation_curve": radiation_curve(target),
        "vft_model": {
            "equation": "log10(eta)=A+B/(T[K]-C)",
            "A": round(a, 6), "B": round(b, 6), "C_K": round(c, 6),
            "target_log10_eta": DEFAULT_FORMING_LOG10_VISCOSITY,
            "calibration": "REFERENCE_SHAPE_PENDING_MATERIAL_CALIBRATION",
        },
        "ai_explanation": ai,
        "material_story": material_story,
        "feature_explanations": feature_explanations,
        "page_scope": "01_MECHANISM_EDUCATION_ONLY_NOT_SHARED_WITH_03",
        "boundaries": [
            "VFT曲线采用参考形态，待玻璃成分/实验粘度数据标定后替换A/B/C。",
            "T⁴曲线表示辐射换热项相对变化，不等于整炉电表能耗同比例变化。",
        ],
    }
