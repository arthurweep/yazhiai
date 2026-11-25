from __future__ import annotations

from typing import Any


def explain(diagnosis: dict[str, Any]) -> dict[str, Any]:
    """首版不依赖外部大模型，保证厂内断网仍可运行。

    这里将结构化规则事实整理成人可读说明；未来接企业批准的大模型时只替换这一层，
    不改采集、数据库、规则与前端接口。
    """
    findings = diagnosis.get("findings") or []
    suggestions = diagnosis.get("suggestions") or []
    state = diagnosis.get("forming_state")
    state_text = {
        "HARD": "当前出炉热状态偏硬侧",
        "SOFT": "当前出炉热状态偏软侧",
        "IN_WINDOW": "当前出炉热状态位于已验证成型窗口",
        "NO_BASELINE": "当前尚未建立该品种运行基线",
        "UNKNOWN": "当前无法完成软硬状态判断",
    }.get(state, "当前状态待确认")
    return {
        "headline": state_text,
        "evidence_text": "；".join([f.get("text", "") for f in findings[:5] if f.get("text")]),
        "action_text": "；".join(suggestions[:3]),
        "mode": "RULE_PHYSICS_EXPLANATION",
        "note": "V3.4 默认采用规则/物理解释，未配置外部大模型也能完整运行。",
    }
