from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from settings import DB_PATH


class ClosingConnection(sqlite3.Connection):
    """Windows 友好的 SQLite 连接：退出 with 时真正关闭文件句柄。"""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Database:
    """V3.4 轻量 SQLite。

    长期只保存高价值 DataTemp 事件、过程事件、摘要、人工基线和少量模型配置。
    仿真 DataTemp 与真实 DataTemp 使用 is_simulated 隔离，避免测试数据污染真实趋势。
    """

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS datatemp_sample (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    ir_scan_time TEXT NOT NULL,
                    receive_time TEXT NOT NULL,
                    prod_name TEXT,
                    is_simulated INTEGER NOT NULL DEFAULT 0,
                    p1 REAL, p2 REAL, p3 REAL, p4 REAL, p5 REAL, p6 REAL,
                    mean REAL, range_value REAL, std REAL,
                    front_avg REAL, rear_avg REAL,
                    left_avg REAL, center_avg REAL, right_avg REAL,
                    left_right_delta REAL, front_rear_delta REAL, center_edge_delta REAL,
                    raw_json TEXT NOT NULL,
                    context_json TEXT,
                    UNIQUE(device_id, ir_scan_time, is_simulated)
                );

                CREATE INDEX IF NOT EXISTS idx_datatemp_time
                    ON datatemp_sample(is_simulated, ir_scan_time DESC);
                CREATE INDEX IF NOT EXISTS idx_datatemp_prod
                    ON datatemp_sample(prod_name, is_simulated, ir_scan_time DESC);

                CREATE TABLE IF NOT EXISTS process_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    prod_name TEXT,
                    severity TEXT NOT NULL DEFAULT 'INFO',
                    summary TEXT NOT NULL,
                    evidence_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_event_time
                    ON process_event(event_ts DESC);

                CREATE TABLE IF NOT EXISTS operation_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    prod_name TEXT,
                    baseline_id INTEGER,
                    is_simulated INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_summary_time
                    ON operation_summary(ts DESC);

                CREATE TABLE IF NOT EXISTS baseline_profile (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_code TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    note TEXT,
                    status TEXT NOT NULL DEFAULT 'RELEASED',
                    payload_json TEXT NOT NULL,
                    UNIQUE(product_code, version)
                );
                CREATE INDEX IF NOT EXISTS idx_baseline_product
                    ON baseline_profile(product_code, version DESC);

                CREATE TABLE IF NOT EXISTS candidate_recipe (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    product_code TEXT,
                    created_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'CANDIDATE',
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidate_recipe_time
                    ON candidate_recipe(created_at DESC);

                CREATE TABLE IF NOT EXISTS energy_config (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    updated_at TEXT NOT NULL,
                    hold_reference_temp_c REAL,
                    hold_reference_power_kw REAL,
                    equivalent_heat_capacity_kwh_per_c REAL,
                    heating_efficiency REAL NOT NULL DEFAULT 1.0,
                    ambient_temp_c REAL NOT NULL DEFAULT 25.0,
                    configured INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            # Forward-only additive migration for existing V3.4.x SQLite databases.
            existing = {r[1] for r in conn.execute("PRAGMA table_info(energy_config)").fetchall()}
            if "reheat_available_power_kw" not in existing:
                conn.execute("ALTER TABLE energy_config ADD COLUMN reheat_available_power_kw REAL")
            conn.execute(
                """
                INSERT OR IGNORE INTO energy_config(
                    id, updated_at, heating_efficiency, ambient_temp_c, configured
                ) VALUES(1, ?, 1.0, 25.0, 0)
                """,
                (utcnow_iso(),),
            )

    # ---------------- DataTemp ----------------
    def latest_ir_scan_time(self, device_id: str, simulated: bool = False) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT ir_scan_time FROM datatemp_sample
                   WHERE device_id=? AND is_simulated=?
                   ORDER BY ir_scan_time DESC LIMIT 1""",
                (device_id, int(simulated)),
            ).fetchone()
            return row["ir_scan_time"] if row else None

    def latest_datatemp_raw(self, device_id: str, simulated: bool = False) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT ir_scan_time, raw_json FROM datatemp_sample
                   WHERE device_id=? AND is_simulated=?
                   ORDER BY ir_scan_time DESC LIMIT 1""",
                (device_id, int(simulated)),
            ).fetchone()
            if not row:
                return None
            return {"ir_scan_time": row["ir_scan_time"], "raw": json.loads(row["raw_json"])}

    def insert_datatemp(self, row: dict[str, Any]) -> bool:
        try:
            with self._lock, self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO datatemp_sample(
                        device_id, ir_scan_time, receive_time, prod_name, is_simulated,
                        p1,p2,p3,p4,p5,p6, mean, range_value, std,
                        front_avg,rear_avg,left_avg,center_avg,right_avg,
                        left_right_delta,front_rear_delta,center_edge_delta,
                        raw_json,context_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["device_id"], row["ir_scan_time"], row["receive_time"], row.get("prod_name"), int(bool(row.get("is_simulated"))),
                        row.get("p1"), row.get("p2"), row.get("p3"), row.get("p4"), row.get("p5"), row.get("p6"),
                        row.get("mean"), row.get("range"), row.get("std"),
                        row.get("front_avg"), row.get("rear_avg"), row.get("left_avg"), row.get("center_avg"), row.get("right_avg"),
                        row.get("left_right_delta"), row.get("front_rear_delta"), row.get("center_edge_delta"),
                        json.dumps(row.get("raw", {}), ensure_ascii=False),
                        json.dumps(row.get("context", {}), ensure_ascii=False),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def datatemp_history(self, limit: int = 50, product: str | None = None, simulated: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            if product:
                rows = conn.execute(
                    """SELECT * FROM datatemp_sample
                       WHERE prod_name=? AND is_simulated=?
                       ORDER BY ir_scan_time DESC LIMIT ?""",
                    (product, int(simulated), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM datatemp_sample
                       WHERE is_simulated=? ORDER BY ir_scan_time DESC LIMIT ?""",
                    (int(simulated), limit),
                ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- Events ----------------
    def insert_event(
        self,
        event_type: str,
        summary: str,
        prod_name: str | None = None,
        severity: str = "INFO",
        evidence: dict[str, Any] | list[Any] | None = None,
        event_ts: str | None = None,
    ) -> int:
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO process_event(event_ts,event_type,prod_name,severity,summary,evidence_json)
                   VALUES(?,?,?,?,?,?)""",
                (
                    event_ts or utcnow_iso(), event_type, prod_name, severity, summary,
                    json.dumps(evidence or {}, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def events(self, limit: int = 100, since_ts: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            if since_ts:
                rows = conn.execute(
                    "SELECT * FROM process_event WHERE event_ts>=? ORDER BY event_ts DESC LIMIT ?",
                    (since_ts, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM process_event ORDER BY event_ts DESC LIMIT ?", (limit,)
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
            except Exception:
                d["evidence"] = {}
            out.append(d)
        return out

    # ---------------- Summary ----------------
    def insert_summary(self, ts: str, prod_name: str | None, baseline_id: int | None, summary: dict[str, Any], simulated: bool = False) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO operation_summary(ts,prod_name,baseline_id,is_simulated,summary_json) VALUES(?,?,?,?,?)",
                (ts, prod_name, baseline_id, int(simulated), json.dumps(summary, ensure_ascii=False)),
            )

    # ---------------- Baseline ----------------
    def current_baseline(self, product_code: str | None) -> dict[str, Any] | None:
        if not product_code:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM baseline_profile
                   WHERE product_code=? AND status='RELEASED'
                   ORDER BY version DESC LIMIT 1""",
                (product_code,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def capture_baseline(self, product_code: str, payload: dict[str, Any], note: str = "") -> dict[str, Any]:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM baseline_profile WHERE product_code=?",
                (product_code,),
            ).fetchone()
            version = int(row["v"]) + 1
            created_at = utcnow_iso()
            cur = conn.execute(
                """INSERT INTO baseline_profile(product_code,version,created_at,note,status,payload_json)
                   VALUES(?,?,?,?, 'RELEASED', ?)""",
                (product_code, version, created_at, note, json.dumps(payload, ensure_ascii=False)),
            )
            return {
                "id": int(cur.lastrowid), "product_code": product_code, "version": version,
                "created_at": created_at, "note": note, "status": "RELEASED", "payload": payload,
            }

    # ---------------- Candidate recipe ----------------
    def save_candidate_recipe(self, name: str, payload: dict[str, Any], product_code: str | None = None, note: str = "") -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("候选工艺名称不能为空")
        created_at = utcnow_iso()
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO candidate_recipe(name,product_code,created_at,note,status,payload_json)
                   VALUES(?,?,?,?, 'CANDIDATE', ?)""",
                (name, product_code, created_at, note or "", json.dumps(payload, ensure_ascii=False)),
            )
            rid = int(cur.lastrowid)
        return {"id": rid, "name": name, "product_code": product_code, "created_at": created_at, "note": note or "", "status": "CANDIDATE", "payload": payload}

    def list_candidate_recipes(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM candidate_recipe WHERE status='CANDIDATE' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    def get_candidate_recipe(self, recipe_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM candidate_recipe WHERE id=?", (int(recipe_id),)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
        return d

    # ---------------- Energy config ----------------
    def get_energy_config(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM energy_config WHERE id=1").fetchone()
        if not row:
            return {}
        d = dict(row)
        # V3.4.2接口不再暴露“参考保温温度”。数据库旧列保留只为无损兼容已有DB。
        d.pop("hold_reference_temp_c", None)
        d["power_reference_meaning"] = "当前12区生产热场下的全温待机参考功率"
        d["config_contract"] = "ENERGY_CONFIG_V3_4_2_NO_REFERENCE_TEMPERATURE"
        return d

    def save_energy_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self.connect() as conn:
            conn.execute(
                """UPDATE energy_config SET
                       updated_at=?, hold_reference_power_kw=?,
                       equivalent_heat_capacity_kwh_per_c=?, heating_efficiency=?,
                       ambient_temp_c=?, reheat_available_power_kw=?, configured=1
                   WHERE id=1""",
                (
                    utcnow_iso(), float(cfg["hold_reference_power_kw"]),
                    float(cfg["equivalent_heat_capacity_kwh_per_c"]), float(cfg.get("heating_efficiency", 1.0)),
                    float(cfg.get("ambient_temp_c", 25.0)),
                    float(cfg["reheat_available_power_kw"]) if isinstance(cfg.get("reheat_available_power_kw"), (float,int)) else None,
                ),
            )
        return self.get_energy_config()
