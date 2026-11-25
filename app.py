from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ai_service import explain
from database import Database
from energy_model import optimize_energy
from glass_model import mechanism_estimate
from furnace_client import FurnaceClient
from model_engine import initial_linked_solve, preview_linked_adjustment, target_forming_temp_suggestion
from runtime import FurnaceRuntime
from settings import APP_TITLE, APP_VERSION, DEVICE_ID, FURNACE_URL, MODE, STATIC_DIR


db = Database()
client = FurnaceClient()
runtime = FurnaceRuntime(db, client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.start()
    yield
    await runtime.stop()


app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_, exc: RequestValidationError):
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
        parts.append(f"{loc or '请求参数'}：{err.get('msg', '格式不正确')}")
    return JSONResponse(status_code=422, content={"detail": "；".join(parts), "errors": exc.errors()})


class BaselineCaptureRequest(BaseModel):
    note: str = Field(default="", max_length=300)


class MechanismEstimateRequest(BaseModel):
    thickness_mm: float = Field(ge=1.0, le=8.0)
    material: Literal["normal", "lowe"] = "normal"
    target_override_c: float | None = Field(default=None, ge=400, le=750)
    test_temp_c: float | None = Field(default=None, ge=350, le=800)


class PhysicalInput(BaseModel):
    big_thickness_mm: float = Field(ge=1.0, le=8.0)
    small_thickness_mm: float = Field(ge=1.0, le=8.0)
    big_material: Literal["normal", "lowe"] = "normal"
    small_material: Literal["normal", "lowe"] = "normal"


class TargetSuggestionRequest(PhysicalInput):
    pass


class InitialSolveRequest(PhysicalInput):
    target_forming_temp_c: float = Field(ge=400, le=750)


class ZoneProfileRow(BaseModel):
    zone: str
    avg_sv: float = Field(ge=300, le=800)


class ConvectionProfileRow(BaseModel):
    zone: str
    avg_sv_big: float = Field(ge=0, le=100)
    avg_sv_small: float = Field(ge=0, le=100)


class ModelPreviewRequest(PhysicalInput):
    target_forming_temp_c: float = Field(ge=400, le=750)
    reference_result_temp_c: float = Field(ge=300, le=800)
    zone_profile: list[ZoneProfileRow]
    convection_zone_profile: list[ConvectionProfileRow]
    loading_gap_s: float | None = Field(default=None, ge=0.1, le=120)
    reference_loading_gap_s: float | None = Field(default=None, ge=0.1, le=120)


class EnergyConfigRequest(BaseModel):
    # V3.4.2取消“参考保温温度”：停机目标温度由当前12区PV + 停机时间直接求解。
    # 该功率表示当前生产热场下的全温待机参考功率，用于能耗标定。
    hold_reference_power_kw: float = Field(gt=0, le=5000)
    equivalent_heat_capacity_kwh_per_c: float = Field(gt=0, le=1000)
    reheat_available_power_kw: float | None = Field(default=None, gt=0, le=10000)
    heating_efficiency: float = Field(default=1.0, gt=0, le=1.0)
    ambient_temp_c: float = Field(default=25.0, ge=-30, le=80)


class EnergyOptimizeRequest(BaseModel):
    downtime_minutes: float = Field(ge=0, le=1440)
    # 以下三个字段为V3.4 API兼容保留；V3.4.2核心算法按12区分段保护下限做向量寻优。
    min_hold_temp_c: float = Field(default=100, ge=100, le=740)
    max_hold_temp_c: float = Field(default=750, ge=100, le=750)
    step_c: float = Field(default=5, ge=1, le=50)
    selected_hold_temp_c: float | None = Field(default=None, ge=100, le=750)
    global_offset_c: float = Field(default=0.0, ge=-60, le=60)
    zone_offsets_c: dict[str, float] = Field(default_factory=dict)
    recovery_target_profile: dict[str, float] | None = None
    recovery_source: str | None = None


class CandidateRecipeSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)
    product_code: str | None = Field(default=None, max_length=120)
    payload: dict[str, Any]


class SimulationRequest(BaseModel):
    enabled: bool


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    dash = await runtime.dashboard()
    return {
        "app": {"title": APP_TITLE, "version": APP_VERSION, "mode": MODE},
        "device_source": {
            "url": FURNACE_URL,
            "method": "POST",
            "request_body": {"deviceId": DEVICE_ID},
            "ok": dash.get("source", {}).get("ok", False),
            "error": dash.get("source", {}).get("error"),
        },
        "simulation": dash.get("simulation") or runtime.simulator.status(),
        "datatemp": dash.get("datatemp_health"),
        "database": {"ok": True, "path": str(db.path)},
        "control_permission": "READ_AND_SUGGEST_ONLY",
    }


@app.get("/api/v1/dashboard")
async def dashboard() -> dict[str, Any]:
    dash = await runtime.dashboard()
    if dash.get("ready"):
        dash["ai_explanation"] = explain(dash.get("diagnosis") or {})
    dash["app"] = {"version": APP_VERSION, "mode": MODE}
    return dash


@app.get("/api/v1/history/datatemp")
async def datatemp_history(
    limit: int = Query(default=50, ge=1, le=500),
    product: str | None = Query(default=None),
    simulated: bool = Query(default=False),
) -> dict[str, Any]:
    return {"items": db.datatemp_history(limit=limit, product=product, simulated=simulated)}


@app.get("/api/v1/events")
async def events(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return {"items": db.events(limit=limit)}


@app.get("/api/v1/simulation")
async def get_simulation() -> dict[str, Any]:
    return runtime.simulator.status()


@app.post("/api/v1/simulation")
async def set_simulation(req: SimulationRequest) -> dict[str, Any]:
    return await runtime.set_simulation(req.enabled)


@app.get("/api/v1/baseline/current")
async def current_baseline() -> dict[str, Any]:
    dash = await runtime.dashboard()
    product = ((dash.get("summary") or {}).get("production") or {}).get("prod_name")
    return {"product_code": product, "baseline": db.current_baseline(product)}


@app.post("/api/v1/baseline/capture")
async def capture_baseline(req: BaselineCaptureRequest) -> dict[str, Any]:
    try:
        baseline = await runtime.capture_current_baseline(req.note)
        return {"ok": True, "baseline": baseline}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc



async def _get_reference_context() -> tuple[dict[str, Any], str, float | None, float | None, dict[str, Any]]:
    dash = await runtime.dashboard()
    if not dash.get("ready"):
        raise HTTPException(status_code=503, detail="尚无设备实时数据，无法取得参考工艺")
    summary = dash["summary"]
    product = summary.get("production", {}).get("prod_name")
    baseline = db.current_baseline(product)
    if baseline:
        reference = (baseline.get("payload") or {}).get("recipe") or {}
        bsum = (baseline.get("payload") or {}).get("captured_summary") or {}
        reference_result = (bsum.get("datatemp") or {}).get("mean")
        source = f"已验证基线 {product}_V{baseline['version']:02d}"
    else:
        raw = await runtime.raw_effective() or {}
        reference = {
            "furnace_temp": raw.get("furnace_temp") or {},
            "convection": raw.get("convection") or {},
            "loading_gap_time_sv": (raw.get("loading_status") or {}).get("loading_gap_time_sv"),
        }
        reference_result = summary.get("datatemp", {}).get("mean")
        source = "当前实时配方（尚未建立已验证基线）"
    return reference, source, reference_result, summary.get("production", {}).get("loading_gap_time_sv"), dash


@app.post("/api/v1/mechanism/estimate")
async def mechanism_estimate_api(req: MechanismEstimateRequest) -> dict[str, Any]:
    # V3.4.2：01是面向高层/培训的独立机理实验室，不继承在线产品基线，也不把临时选择带入03。
    return mechanism_estimate(
        thickness_mm=req.thickness_mm,
        material=req.material,
        target_override_c=req.target_override_c,
        test_temp_c=req.test_temp_c,
        validated_target_c=None,
    )


@app.post("/api/v1/model/target-suggestion")
async def model_target_suggestion(req: TargetSuggestionRequest) -> dict[str, Any]:
    _, source, reference_result, _, dash = await _get_reference_context()
    # 只有真正“已验证基线”才把 DataTemp 均温作为强靶心；当前实时值不能自动变成质量合格靶心。
    baseline = dash.get("baseline")
    validated = reference_result if baseline else None
    result = target_forming_temp_suggestion(
        big_thickness_mm=req.big_thickness_mm,
        small_thickness_mm=req.small_thickness_mm,
        big_material=req.big_material,
        small_material=req.small_material,
        validated_target_c=validated,
    )
    result["reference_source"] = source
    return result


@app.post("/api/v1/model/initial-solve")
async def initial_solve(req: InitialSolveRequest) -> dict[str, Any]:
    reference, reference_source, reference_result, gap, _ = await _get_reference_context()
    try:
        result = initial_linked_solve(
            reference_recipe=reference,
            current_datatemp_mean=reference_result,
            target_forming_temp_c=req.target_forming_temp_c,
            big_thickness_mm=req.big_thickness_mm,
            small_thickness_mm=req.small_thickness_mm,
            big_material=req.big_material,
            small_material=req.small_material,
            current_loading_gap_s=gap,
        )
        result["reference_source"] = reference_source
        result["write_to_plc"] = False
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/model/preview")
async def model_preview(req: ModelPreviewRequest) -> dict[str, Any]:
    reference, reference_source, _, gap, _ = await _get_reference_context()
    try:
        result = preview_linked_adjustment(
            reference_recipe=reference,
            reference_result_temp_c=req.reference_result_temp_c,
            target_forming_temp_c=req.target_forming_temp_c,
            big_thickness_mm=req.big_thickness_mm,
            small_thickness_mm=req.small_thickness_mm,
            big_material=req.big_material,
            small_material=req.small_material,
            zone_profile=[x.model_dump() for x in req.zone_profile],
            convection_zone_profile=[x.model_dump() for x in req.convection_zone_profile],
            loading_gap_s=req.loading_gap_s,
            reference_loading_gap_s=req.reference_loading_gap_s if req.reference_loading_gap_s is not None else gap,
        )
        result["reference_source"] = reference_source
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/recipes/current")
async def current_recipe() -> dict[str, Any]:
    dash = await runtime.dashboard()
    if not dash.get("ready"):
        raise HTTPException(status_code=503, detail="尚无设备实时数据")
    zones = (dash.get("summary", {}).get("furnace", {}).get("zones") or {})
    zone_sv = {str(z).upper(): row.get("avg_sv") for z, row in sorted(zones.items()) if isinstance(row.get("avg_sv"), (int, float))}
    zone_pv = {str(z).upper(): row.get("avg_pv") for z, row in sorted(zones.items()) if isinstance(row.get("avg_pv"), (int, float))}
    prod = (dash.get("summary", {}).get("production") or {})
    return {
        "product_code": prod.get("prod_name"),
        "loading_gap_s": prod.get("loading_gap_time_sv"),
        "zone_sv": zone_sv,
        "zone_pv": zone_pv,
        "source": "CURRENT_DEVICE_JSON",
    }


@app.get("/api/v1/recipes/candidates")
async def candidate_recipes(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return {"items": db.list_candidate_recipes(limit)}


@app.get("/api/v1/recipes/candidates/{recipe_id}")
async def candidate_recipe(recipe_id: int) -> dict[str, Any]:
    row = db.get_candidate_recipe(recipe_id)
    if not row:
        raise HTTPException(status_code=404, detail="候选工艺不存在")
    return row


@app.post("/api/v1/recipes/candidates")
async def save_candidate_recipe(req: CandidateRecipeSaveRequest) -> dict[str, Any]:
    try:
        row = db.save_candidate_recipe(req.name, req.payload, req.product_code, req.note)
        db.insert_event("CANDIDATE_RECIPE_SAVED", f"保存候选工艺：{req.name}", prod_name=req.product_code, evidence={"recipe_id": row["id"]})
        return {"ok": True, "recipe": row}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/energy/config")
async def get_energy_config() -> dict[str, Any]:
    return db.get_energy_config()


@app.post("/api/v1/energy/config")
async def save_energy_config(req: EnergyConfigRequest) -> dict[str, Any]:
    return db.save_energy_config(req.model_dump())


@app.post("/api/v1/energy/optimize")
async def energy_optimize(req: EnergyOptimizeRequest) -> dict[str, Any]:
    dash = await runtime.dashboard()
    if not dash.get("ready"):
        raise HTTPException(status_code=503, detail="尚无设备实时数据，无法取得当前12温区热场")
    zones = (dash.get("summary", {}).get("furnace", {}).get("zones") or {})
    profile = [
        {"zone": str(z).upper(), "pv_c": row.get("avg_pv"), "sv_c": row.get("avg_sv")}
        for z, row in sorted(zones.items())
        if isinstance(row.get("avg_pv"), (int, float))
    ]
    default_recovery = {r["zone"]: float(r["sv_c"]) for r in profile if isinstance(r.get("sv_c"), (int, float))}
    recovery_target = req.recovery_target_profile or default_recovery
    recovery_source = req.recovery_source or "停机前当前生产配方SV"
    try:
        return optimize_energy(
            downtime_minutes=req.downtime_minutes,
            min_hold_temp_c=req.min_hold_temp_c,
            max_hold_temp_c=req.max_hold_temp_c,
            step_c=req.step_c,
            config=db.get_energy_config(),
            current_zone_profile=profile,
            selected_hold_temp_c=req.selected_hold_temp_c,
            global_offset_c=req.global_offset_c,
            zone_offsets_c=req.zone_offsets_c,
            mold_context=((dash.get("summary") or {}).get("mold") or {}).get("n01"),
            recovery_target_profile=recovery_target,
            recovery_source=recovery_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/raw/current")
async def raw_current() -> dict[str, Any]:
    raw = await runtime.raw_effective()
    if raw is None:
        raise HTTPException(status_code=503, detail="尚无实时数据")
    return raw


@app.get("/api/v1/raw/source")
async def raw_source() -> dict[str, Any]:
    raw = await runtime.raw_source()
    if raw is None:
        raise HTTPException(status_code=503, detail="尚无原始设备数据")
    return raw
