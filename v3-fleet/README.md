# V3 巡检车队平台（v3-fleet）

> 多车车队版。接口协议见 `docs/V3-接口协议设计.md`（**v1.4**，v1.3 基础上新增第 16 节 AI 接口 `/ai/*`），架构交互图见 `docs/V3-终端网络组织架构图.html`，开发顺序见 `docs/V3-开发路线图.md`，**换机部署/课堂演示前检查见 `docs/V3-部署运维手册.md`**。

## 版本策略

| 版本 | 目录 | 内容 | 状态 |
|---|---|---|---|
| V1 | `../lab01-tcp-sim` | TCP 直连 | 定版归档，不动 |
| V2 | `../iot-stage` | IoT 云控单车版 | **定版冻结**，教学版本；V3 只参考其代码，不改其文件 |
| V3 | `.`（本工程 v3-fleet） | 车队版 | 开发中 |

三版本已统一归档于 `smart-patrol-car/` 下，总览见 `../README.md`。

原则：V3 从 V2 复制改造而来，两工程文件互不影响；云侧（IoTDA 物模型 / MQTT 协议 / 应用侧 SDK）零改动，只改业务层。

## 目录结构

```
v3-fleet/
├── docs/     设计文档（接口协议、开发路线图）
├── server/   业务后端（REST :8000 + WS 广播 :8889；fleet_cache 缓存、track_store 轨迹库、admin 管理接口、ai_service AI 服务）
├── device/   设备端模拟器（car_001/002 定版件 + run_car.py 通用模拟器）+ 启动器 + 验收脚本
└── web/      网页端（fleet_view.html 监控/回放 + admin.html 管理后台 + test_console.html 接口测试台含 AI 语音控车）
```

## 设备注册表（server/devices.json）

登记车队全部设备：device_id、名称、渲染色、frozen（步骤⑤起由管理后台动态维护，也支持手工编辑——改完重启后端即可生效）。配套 `server/devices_secret.json` 存设备接入密钥（**敏感文件仅存本机，勿入仓**）。

## 管理后台（步骤⑤）

**访问**：浏览器直接双击打开 `web/admin.html`（零依赖，后端已放行 CORS）。

**登录**：默认账号 `admin / admin123`（存在 `server/config.json` 的 `admin_user/admin_pass`，仅教学环境，改完配置重启后端生效；token 只存后端内存，重启后需重新登录）。

**四视图**：

| 视图 | 内容 |
|---|---|
| 总览 | 在线/离线/冻结统计 + 电量排行 + 最近 7 天消息量（额度监控打底），10 秒自动刷新 |
| 设备管理 | 设备列表、注册新设备、冻结/解冻（带二次确认） |
| 命令历史 | commands.log 结构化展示（时间/设备/命令/参数/结果，最新在前） |
| 实时监控 | 内嵌 fleet_view.html |

**"注册 → 使用 → 冻结"标准闭环操作顺序**：

1. 设备管理页注册新设备（如 node_id=`car_003`）：后端自动完成云侧注册 + devices.json 落盘 + 密钥落盘，页面**只显示一次**密钥，复制保存
2. 启动新设备模拟器（见下方 run_car.py 用法），总览/监控页约 5 秒内出现新车
3. 用 test_console.html 或 APP 对新车下发命令验证
4. 不再需要时在设备管理页点"冻结"：云侧禁连 MQTT + 后端拒绝命令（命令历史页可见拒绝审计记录）；解冻可恢复

## AI 语音控车（步骤⑦，v1.4）

**网页入口**：双击打开 `web/test_console.html`，控制台卡下方的「AI 语音控车」卡：

- **按住说话**：按住录音、松开自动"识别 → 解析 → 执行"，录中按钮变红显示计时；对话区依次显示 [你说] / [AI] 回复 / [执行] 每条动作的 ✓/✗
- **文字指令**：输入框直接打字（回车或点「执行」），跳过 ASR
- **控制目标**：左侧选中了车 → 指令只发给这台车（device_id 优先）；没选车 → 由 AI 听话里的车号派车（"1号车前进" → car_001），两者皆无返回 400 提示
- 话术示例：「1号车前进速度 60」「2号车左转然后停止」「前进」（未提车号时先选车）；前进/后退没说速度默认 50；一句话最多执行 6 个动作

**接口清单**（协议文档第 16 节）：

| 接口 | 说明 |
|---|---|
| `POST /ai/asr` | 语音转文字。body=音频字节（audio/wav 直接用；audio/pcm 按 `?rate=16000&channels=1&bits=16` 补 WAV 头），返回 `{"ok":true,"text":...,"provider":...}` |
| `POST /ai/command` | 自然语言控车。body=`{"text": 必填, "device_id": 可选}`；AI 指令走与手控 /command 相同的下发链路（冻结拦截/审计日志/回执后刷缓存自然生效），白名单校验是最后一道闸 |

**provider 切换**：`server/config.json` 的 `ai.asr_provider` 在 `"local"`（FunASR 本地推理，模型缓存在本机，后端启动时后台预热约 30 秒，断网可用）与 `"qwen"`（云端 asr-flash-message）之间切换，**改后重启后端生效**；LLM 固定走 `ai.llm_*`（DeepSeek）。所有 AI 凭证只存 config.json（本机勿入仓）。依赖：`funasr`、`dashscope`、`requests`（D:/anaconda3 解释器已装齐）。

## 鸿蒙 APP（步骤⑥⑦）

工程 `D:\DevEco\HarmonyOSProjects\CarCtrlFleet`（包名 `edu.myb.carctrlfleet`，自 V2 的 CarCtrlCloud 复制改造，两工程可共存于同一模拟器）：

- **选车控车**：首页点"刷新车辆"→ 车卡片点选绑定（冻结车置灰拒绑）→ 方向盘/速度滑条只作用于绑定的车，2 秒轮询该车状态
- **AI 语音控车**：按住"按住说话"（首次弹麦克风权限）→ 录音直传 `/ai/asr` → `/ai/command` 执行 → 界面显示 AI 回复与逐条执行结果；另有文字指令输入框
- **模拟器联调**：DevEco Studio Run 启动（IDE 自动签名），后端地址填 `http://10.0.2.2:8000`；命令行构建方法与排错见部署运维手册第二节

## 通用模拟器（device/run_car.py）

car_001 / car_002 的模拟器（`car_mqtt_client_001/002.py`）是 V2 收编定版件（硬编码各自身份），保持不动；**新注册设备一律用 run_car.py**（与 001 同款连接/上报/命令逻辑，身份参数化）：

```
cd smart-patrol-car\v3-fleet\device
D:/anaconda3/python.exe run_car.py --node car_003
```

- 设备 ID 自动拼为 `{product_id}_{node_id}`（product_id 读 `server/config.json`）
- 密钥自动读 `server/devices_secret.json`（管理后台注册时写入）；**查不到密钥会报错退出并提示先去管理后台注册**
- `--device_id` 可覆盖完整 ID，`--server` 可覆盖设备侧端点（默认从 config.json 的应用侧端点派生 iotda-device 域名）
- 上报节奏与 001 同款：运动 5 秒 / 空闲 60 秒（勿调快，保护消息额度）

## 端口清单

| 端口 | 协议 | 用途 |
|---|---|---|
| 8000 | HTTP (Flask) | 业务后端 REST 接口（协议第 4/7.2/7.3 节） |
| 8889 | WebSocket (websockets) | 车队状态广播 `ws://<host>:8889/ws`（协议 7.1，步骤④；独立端口原因见 server/ws_server.py 头注释） |

## 步骤① 产物清单（多车模拟底座）

| 文件 | 说明 |
|---|---|
| `server/devices.json` | 双车静态注册表（car_001 / car_002） |
| `device/car_mqtt_client_001.py` | 1 号车模拟器（自 V2 原样收编，未改动） |
| `device/car_mqtt_client_002.py` | 2 号车模拟器（自 V2 原样收编，未改动） |
| `device/start_fleet.py` | 车队启动器：自动扫描 devices.json，拉起全部未冻结的车（001/002 用 V2 收编脚本，新车走 run_car.py），Ctrl+C 同时结束 |
| `device/verify_step1.py` | 云端验收脚本：查双车在线状态 + 影子，输出验收结论 |
| `web/README.md` | 占位说明（网页渲染是步骤④的事） |

## 双车启动方法

```
cd smart-patrol-car\v3-fleet\device
D:/anaconda3/python.exe start_fleet.py
```

- 会分别启动 car_001、car_002 两个子进程，日志前缀区分
- `Ctrl+C` 同时干净停止两台车（各自正常断开 MQTT）
- 上报节奏沿用 V2：运动 5 秒 / 空闲 60 秒（保护每日 1 万条消息额度，勿调快）

## 验收脚本用法

```
cd smart-patrol-car\v3-fleet\device
D:/anaconda3/python.exe verify_step1.py
```

先让双车在线至少 1 分钟（有上报才有影子数据），脚本会对 devices.json 里每台车调用
ShowDevice（在线状态）与 ShowDeviceShadow（最新影子），打印 x/y/heading/speed/battery，
最后汇总"两台设备影子是否各自独立有数据"的验收结论。

应用侧 AK/SK 凭证从 `../../iot-stage/server/config.json` 读取（凭证隔离：只存本机，不入仓、不入文档）。

## ⚠️ 重要警告

**V2 模拟器（iot-stage/server/car_mqtt_client_001.py、_002.py）与 V3 模拟器（本目录同名脚本）不要同时跑！**

华为云 IoTDA 同一 device_id 只允许一个 MQTT 连接，两处同时跑会互相踢下线（表现为反复重连、频繁掉线），影子数据也被污染。启动 start_fleet.py 前务必确认 V2 侧的模拟器已停止。
