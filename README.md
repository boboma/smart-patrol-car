# smart-patrol-car — 鸿蒙巡检车项目归档

> 三个版本的鸿蒙巡检车项目统一归档在此目录下。三个版本相互独立、各自定版，
> 演进关系为 V1（TCP 直连）→ V2（IoT 云控）→ V3（车队平台）。

## 目录结构

| 目录 | 版本 | 内容 | 状态 |
|---|---|---|---|
| `lab01-tcp-sim/` | V1 | TCP 直连教学版：PC 模拟车（TCP 8888 + WS 8889）+ Canvas 俯视图网页，APP 端直连 | **定版归档**，不动 |
| `iot-stage/` | V2 | IoT 云控单车版：华为云 IoTDA + MQTT，Flask 后端（:8000）+ 鸿蒙 APP（CarCtrlCloud） | **定版冻结**，教学版本 |
| `v3-fleet/` | V3 | 车队平台版：多车注册表 + 车队缓存 + 轨迹落库回放 + B/S 管理后台 + AI 语音控车 | **开发中**（主线） |

## 版本演进一句话

- **V1**：APP 与模拟车在同一局域网内 TCP 直连，理解"客户端-服务端"与协议设计
- **V2**：引入华为云 IoTDA，车变 MQTT 设备、命令走云、状态靠设备影子，理解"云-管-端"
- **V3**：单车 → 车队：设备注册表动态管理、多车同屏监控、轨迹落库回放、管理后台、AI 语音控车

## 各版本入口

- **V1**：见 `lab01-tcp-sim/README.md`（server 启动 + web/car_view.html）
- **V2**：见 `iot-stage/README.md`（app.py + car_mqtt_client_001.py）
- **V3**：见 `v3-fleet/README.md`（功能总览）与 `v3-fleet/docs/`（接口协议 v1.4、开发路线图、部署运维手册）

## 凭证管理约定（2026-11-20 统一整理）

原则：**账号/密钥一律不写死在代码里**，集中放各项目本机配置文件；仓库/分享时用脱敏模板。

| 项目 | 凭证文件（真实，仅本机勿入仓） | 脱敏模板（随项目分发） | 内容 |
|---|---|---|---|
| V1 lab01-tcp-sim | 无 | 无 | 纯 TCP/WS 本机直连，零账号零密钥 |
| V2 iot-stage | `iot-stage/server/config.json` | `iot-stage/server/config.example.json` | 华为云 AK/SK、project_id、endpoint、**device_secrets（car_001/002 设备密钥）** |
| V3 v3-fleet | `v3-fleet/server/config.json` + `devices_secret.json` | `v3-fleet/server/config.example.json` | AK/SK、AI（ASR/LLM）key、管理后台账号；设备密钥由管理后台注册时自动写入 devices_secret.json |

- 代码读取路径：V2 车脚本/认证测试读同目录 `config.json` 的 `device_secrets` 段；
  V3 所有车脚本（含 001/002 收编脚本与 run_car.py）统一读 `server/devices_secret.json`（node_id / 完整 device_id 两种键兼容）。
- 拿到新环境：复制 `config.example.json` 为 `config.json` 填入自己的凭证即可，无需改任何代码。

## 通用注意事项

1. **同 device_id 的模拟器全局只能跑一份**——V2 与 V3 的 car_001/car_002 脚本同源同 ID，
   两处同时启动会 MQTT 互踢（反复掉线）。启动前确认另一版本的车没在跑。
2. **凭证只存本机**：`iot-stage/server/config.json` 与 `v3-fleet/server/config.json`
   含华为云 AK/SK、AI API key、管理后台账号，均不入仓不入文档。
3. V3 的验收脚本会读 `../iot-stage/server/config.json`（凭证隔离：AK/SK 只在 V2 存一份），
   因此 **iot-stage 与 v3-fleet 必须保持同级目录**，请勿单独挪动其中一个。
4. 鸿蒙 APP 工程不在本目录，在 `D:\DevEco\HarmonyOSProjects\`：
   `CarCtrlCloud`（V2 用）/ `CarCtrlFleet`（V3 用），详见各自 README。
