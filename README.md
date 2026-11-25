# 压制动态热能与成型管理 V3.4.9｜参考源码像素级融合版

> **代码基线：V3.4.7完整系统。** 本版没有以V3.4.8为基础继续改界面。03、04、05改为直接使用用户认可的参考HTML源码作为前端母版；真实数据、已确认工艺规则和FastAPI后台继续沿用V3系统。

## 1. V3.4.9 本轮变化

- **03 VFT联动求解**：直接嵌入认可的03参考HTML源码，不再重新设计布局。后台继续按“厚度优先共享阶梯炉温、非镀膜默认不新增对流、Low-E独立补偿、双片窗口可行性优先”求解。
- **03候选工艺**：仅新增用户明确要求的 `保存候选 / 调出候选 / 执行JSON预览`。候选工艺保存到SQLite，不等于已验证发布基线。
- **04停机保温/复线**：直接嵌入认可的04参考HTML源码；12区当前PV直接读取当前设备JSON；保温SV由停机时长与后台模型求解；复线目标可选当前生产配方或03已保存候选工艺。
- **04单区弹窗**：沿用参考HTML原有“降温→保温→升温恢复”的动态热梯度弹窗，只替换数据和计算结果。
- **05退火风机**：使用认可的 `yazhi.html` 原05页面作为显示母版，不再在V3.4.7旧05布局上重新设计。
- **PLC边界**：03/04可生成后续反写使用的执行JSON预览，但当前统一 `PREVIEW_ONLY`、`write_to_plc=false`，没有实际PLC写入。
- **参考原稿留档**：`docs/reference_sources/` 保存03、04、yazhi原始HTML，便于后续核对。

详细边界见：`docs/19_V3.4.9参考源码像素级融合与执行JSON边界.md`。

## 2. 保持冻结的系统边界

- 设备接口仍为 `POST http://10.200.80.32:9001/api/Pressfurnace/realtime`；
- 请求体仍为 `{"deviceId":"GZ15011301"}`；
- 72个炉温物理点、121个对流物理阀位；
- N01当前工作压台 / N02备用预热压台语义不变；
- 出炉温度事件仍由 `ir_scan_time` 判定，无 `glass_id`；
- SQLite事件化存储；
- 全局PV仿真不修改源JSON/SV；
- 第一阶段无PLC写权限。

## 3. Windows首次部署

建议解压到新目录，例如：

```text
D:\AI_Project\pressfurnace_ai_v3_4_9
```

VSCode打开该目录后，在PowerShell执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_selftest.py
```

正常应看到：

```text
Ran 43 tests
OK
```

Demo启动：

```powershell
.\start_demo.bat
```

本机访问：

```text
http://127.0.0.1:8800/
```

局域网访问：

```text
http://服务器IPv4地址:8800/
```

如局域网无法访问，以管理员身份运行：

```text
open_firewall_8800_admin.bat
```

## 4. 真实设备运行

先测试设备接口：

```powershell
.\.venv\Scripts\python.exe check_device.py
```

确认成功后：

```powershell
.\start.bat
```

FastAPI接口文档：

```text
http://127.0.0.1:8800/docs
```

## 5. V3.4.9新增/相关API

- `GET /api/v1/recipes/current`：当前产品、上片间隙、12区SV/PV；
- `GET /api/v1/recipes/candidates`：候选工艺列表；
- `GET /api/v1/recipes/candidates/{id}`：读取候选；
- `POST /api/v1/recipes/candidates`：保存候选；
- `POST /api/v1/model/target-suggestion`；
- `POST /api/v1/model/initial-solve`；
- `POST /api/v1/model/preview`；
- `GET/POST /api/v1/energy/config`；
- `POST /api/v1/energy/optimize`：支持 `recovery_target_profile` 和 `recovery_source`。

## 6. 参考HTML外部资源说明

用户认可的03/04/yazhi原始HTML使用 Tailwind CDN、ECharts CDN 和 Google Fonts。本版为了不破坏已认可的视觉，保留原始引用，并额外提供本地 `static/vendor/tailwind_fallback.css` 作为Tailwind样式后备。

**如果部署电脑完全无法访问外网，ECharts CDN不可用时，03/04/05中的图表可能无法加载；后台、真实JSON、候选保存和能耗接口不受影响。** 若需要厂内完全离线且仍保持参考页图形视觉，需要后续把参考页同版本ECharts文件本地化后冻结验收。

## 7. 历史文档优先级

1. `docs/19_V3.4.9参考源码像素级融合与执行JSON边界.md`
2. `docs/18_V3.4.7布局专项修复与验收.md`（未被19修改的V3.4.7规则继续有效）
3. `docs/00_V3.4项目总冻结说明.md`
4. 其它V3.4.x历史增量文档
