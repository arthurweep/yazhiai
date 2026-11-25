from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
SAMPLE_RESPONSE = BASE_DIR / "sample" / "response.json"
DB_PATH = Path(os.getenv("PRESSFURNACE_DB", str(DATA_DIR / "pressfurnace_v3_4_4.db")))

# 已冻结的真实设备接口：POST + JSON body
FURNACE_URL = os.getenv(
    "FURNACE_URL",
    "http://10.200.80.32:9001/api/Pressfurnace/realtime",
)
DEVICE_ID = os.getenv("DEVICE_ID", "GZ15011301")
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "3"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "2.5"))

# real: 调真实设备；demo: 使用 sample/response.json，便于离线演示/验收
MODE = os.getenv("PRESSFURNACE_MODE", "real").strip().lower()

# 工艺展示/事件规则。这里不是 PLC 控制定值；后续用真实数据校准时再调整。
DATATEMP_FORMING_WINDOW_C = 5.0
PROCESS_CONTEXT_WINDOW_SECONDS = 600
SUMMARY_INTERVAL_SECONDS = 600
MOLD_NEAR_SETPOINT_TOLERANCE_C = 5.0

# 全局 PV 仿真采用固定的小幅随机游走，不做复杂配置中心。
SIM_FURNACE_PV_BOUND_C = 1.2
SIM_CONVECTION_PV_BOUND = 1.0
SIM_MOLD_PV_BOUND_C = 1.0
SIM_TORQUE_PV_BOUND = 0.25
SIM_VACUUM_PV_BOUND = 1.2
SIM_ANNEAL_PV_BOUND = 0.6
SIM_DATATEMP_PV_BOUND_C = 1.8


# V3.4.2 增量模型默认值：均为软件/开发验证参数，不是PLC控制定值。
# 出炉温度均匀性阈值来自当前项目讨论，用于预警展示；正式量产前由工艺确认。
OUTLET_RANGE_WARN_C = 4.0
OUTLET_SIDE_DELTA_WARN_C = 3.0
ANNEAL_STAGE_DELTA_ATTENTION_C = 5.0

# 停机保温12区向量寻优的分段保护下限。
# 预热/主热段用于理论候选边界；出炉段550℃为当前项目保护假设，必须现场确认后方可用于执行。
ENERGY_HOLD_MIN_PREHEAT_C = 380.0
ENERGY_HOLD_MIN_MAIN_C = 450.0
ENERGY_HOLD_MIN_EXIT_C = 550.0
ENERGY_VECTOR_OFFSET_MIN_C = -40.0
ENERGY_VECTOR_OFFSET_MAX_C = 40.0

# Demo/全局PV仿真：当源JSON温区几乎完全相同时，叠加工艺阶梯PV形态。
# 仅用于测试02/04/诊断链，真实模式不改设备源数据。
SIM_STAGE_PROFILE_C = [560.0, 580.0, 600.0, 620.0, 642.0, 652.0, 662.0, 658.0, 648.0, 630.0, 620.0, 612.0]

APP_TITLE = "汽车玻璃压制成型热能管理与智能监督系统"
APP_VERSION = "3.4.9"
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8800"))
