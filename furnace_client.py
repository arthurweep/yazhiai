from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from settings import DEVICE_ID, FURNACE_URL, MODE, REQUEST_TIMEOUT_SECONDS, SAMPLE_RESPONSE


class FurnaceClient:
    def __init__(
        self,
        url: str = FURNACE_URL,
        device_id: str = DEVICE_ID,
        mode: str = MODE,
        sample_path: Path = SAMPLE_RESPONSE,
    ) -> None:
        self.url = url
        self.device_id = device_id
        self.mode = mode
        self.sample_path = sample_path
        self._demo_data: dict[str, Any] | None = None

    async def fetch(self) -> dict[str, Any]:
        if self.mode == "demo":
            return await self._fetch_demo()
        return await self._fetch_real()

    async def _fetch_real(self) -> dict[str, Any]:
        # 已冻结：POST + JSON body {"deviceId": "GZ15011301"}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False) as client:
            resp = await client.post(self.url, json={"deviceId": self.device_id})
            resp.raise_for_status()
            payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("设备接口返回内容不是 JSON 对象")
        if payload.get("code") not in (200, "200", None):
            raise RuntimeError(f"设备接口业务返回异常: code={payload.get('code')} message={payload.get('message')}")
        data = payload.get("data") if "data" in payload else payload
        if not isinstance(data, dict):
            raise ValueError("设备接口 JSON 中 data 不是对象")
        if not isinstance(data.get("device_info"), dict):
            raise ValueError("设备接口缺少 device_info")
        return data

    async def _fetch_demo(self) -> dict[str, Any]:
        """离线模式直接复用 sample/response.json。

        V3.4 不再在 FurnaceClient 里偷偷修改 PV。所有波动统一交给全局 PV 仿真层，
        从而保证：仿真 OFF = 原 JSON；仿真 ON = 前后台共同使用同一份仿真副本。
        """
        if self._demo_data is None:
            raw = json.loads(self.sample_path.read_text(encoding="utf-8"))
            self._demo_data = raw.get("data", raw)
        await asyncio.sleep(0)
        return deepcopy(self._demo_data)
