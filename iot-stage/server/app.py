"""
iot-stage - 业务后端（轻量中转服务）
=====================================
架构角色：APP（控制端）→ 业务后端（本服务）→ 华为云 IoTDA → 小车端

职责（对应"路线B"的三层管控思想）：
  1. 凭证隔离：AK/SK 只存在本机，客户端零持有
  2. 管控点：命令白名单校验 + 审计日志（commands.log）
  3. 协议简化：把平台 API 包成两个极简接口给 APP

HTTP 接口：
  GET  /health                    探活 + 设备在线状态
  GET  /status                    小车最新状态（来自设备影子）
  POST /command                   下发控制命令（同步：等车执行完才返回）
       body: {"cmd": "FORWARD", "speed": 80}
       cmd   ∈ FORWARD / BACK / LEFT / RIGHT / STOP
       speed 仅 FORWARD/BACK 需要，0~100

运行：python app.py           （读取同目录 config.json，默认端口 8000）
依赖：flask、huaweicloudsdkcore、huaweicloudsdkiotda（v5）
"""

import json
import logging
import os
from decimal import Decimal

from flask import Flask, jsonify, request

from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
from huaweicloudsdkcore.exceptions import exceptions as hc_exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkiotda.v5 import IoTDAClient
from huaweicloudsdkiotda.v5.model import (
    CreateCommandRequest,
    DeviceCommandRequest,
    ShowDeviceRequest,
    ShowDeviceShadowRequest,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 配置加载
with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

# ---------------------------------------------------------------- 审计日志
# 管控层可见的证据：谁在何时下发了什么、结果如何。教学点：命令审计日志。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(BASE_DIR, "commands.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger("backend")

# ---------------------------------------------------------------- 云客户端（全局一份）
# 官方指南用法（标准版实例，见 sdkreference-iothub/iot_10_10003.html）：
#   1. BasicCredentials + with_derived_predicate：专属端点自动启用 V11 衍生签名
#   2. 自建 Region(region_id, 应用侧专属端点) + with_region()
#   3. 用实例专属端点时，请求一律不带 instance_id（域名已隐含实例）
credentials = BasicCredentials(CFG["ak"], CFG["sk"], CFG["project_id"])
credentials.with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
iot_client = (
    IoTDAClient.new_builder()
    .with_credentials(credentials)
    .with_region(Region(CFG["region"], CFG["endpoint"]))
    .build()
)

COMMAND_PARAS = {"FORWARD": ("speed",), "BACK": ("speed",), "LEFT": (), "RIGHT": (), "STOP": ()}

app = Flask(__name__)


# ---------------------------------------------------------------- CORS
# 俯视图页面（web/car_cloud_view.html）以 file:// 打开，向本服务发起跨源 fetch，
# 浏览器要求响应带 CORS 头，否则拦截。教学环境放行所有来源即可。
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ---------------------------------------------------------------- 辅助
def build_paras(cmd: str, body: dict):
    """按白名单校验命令并抽出参数。返回 (paras, 错误信息)"""
    if cmd not in COMMAND_PARAS:
        return None, f"未知命令 {cmd}，可选：{list(COMMAND_PARAS)}"
    needed = COMMAND_PARAS[cmd]
    if not needed:
        return {}, None
    speed = body.get("speed")
    if not isinstance(speed, int) or not 0 <= speed <= 100:
        return None, "speed 必须是 0~100 的整数"
    return {"speed": speed}, None


def shadow_to_state(shadow_resp) -> dict:
    """把设备影子响应压平成 APP 好用的状态字典"""
    state = {"x": None, "y": None, "heading": None,
             "speed": None, "battery": None, "time": None}
    try:
        for s in shadow_resp.shadow:
            if s.reported and s.reported.properties:
                for k in state:
                    if k in s.reported.properties:
                        v = s.reported.properties[k]
                        # battery 等数值可能返回 Decimal，jsonify 不能序列化 Decimal
                        state[k] = float(v) if isinstance(v, Decimal) else v
                state["time"] = s.reported.event_time
    except (AttributeError, TypeError):
        pass
    return state


def device_online() -> bool:
    """设备是否在线（设备详情的 status 字段：ONLINE/OFFLINE/INACTIVE）"""
    try:
        resp = iot_client.show_device(
            ShowDeviceRequest(device_id=CFG["device_id"]))
        return resp.status == "ONLINE"
    except hc_exceptions.SdkException as e:
        log.error("查设备状态失败: %s", e)
        return False


# ---------------------------------------------------------------- 接口
@app.get("/health")
def health():
    return jsonify({"ok": True, "device_online": device_online()})


@app.get("/status")
def status():
    """APP 轮询此接口拿小车最新状态（数据源：设备影子）"""
    try:
        resp = iot_client.show_device_shadow(
            ShowDeviceShadowRequest(device_id=CFG["device_id"]))
    except hc_exceptions.SdkException as e:
        return jsonify({"ok": False, "error": f"查影子失败: {e}"}), 502
    return jsonify({"ok": True, "online": device_online(), **shadow_to_state(resp)})


@app.post("/command")
def command():
    """下发命令（同步命令：等小车执行回执后才返回，超时约 20 秒）"""
    body = request.get_json(silent=True) or {}
    cmd = (body.get("cmd") or "").upper()
    paras, err = build_paras(cmd, body)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    req = CreateCommandRequest(
        device_id=CFG["device_id"],
        # 云端校验要求 paras 恒非空：无参命令也传 {}（传 None 报 IOTDA.000006）
        body=DeviceCommandRequest(service_id="car", command_name=cmd, paras=paras or {}),
    )
    log.info("命令下发 -> %s %s", cmd, paras or "")
    try:
        resp = iot_client.create_command(req)
    except hc_exceptions.SdkException as e:
        log.error("命令失败 -> %s %s err=%s", cmd, paras or "", e)
        return jsonify({"ok": False, "error": str(e)}), 502

    # 同步命令：resp.response 有值（response_name=命令名）说明车已执行并回执
    # 注意：SDK 反序列化后 response 可能是 dict 也可能是模型对象，统一压成 dict 处理
    resp_info = getattr(resp, "response", None)
    if isinstance(resp_info, dict):
        info = resp_info
    elif resp_info is not None:
        info = {k: getattr(resp_info, k, None) for k in ("response_name", "result", "paras", "error_code")}
    else:
        info = None
    result = {
        "command_id": getattr(resp, "command_id", None),
        "executed": bool(info and info.get("response_name")),
        "response_name": info.get("response_name") if info else None,
    }
    log.info("命令回执 -> %s %s result=%s", cmd, paras or "", result)
    return jsonify({"ok": True, **result})


if __name__ == "__main__":
    print("=" * 50)
    print(" 业务后端已启动（APP -> 本服务 -> 华为云 IoTDA）")
    print(f"   GET  http://127.0.0.1:{CFG['http_port']}/status")
    print(f"   POST http://127.0.0.1:{CFG['http_port']}/command")
    print(f" 目标设备: {CFG['device_id']}")
    print("=" * 50, flush=True)
    app.run(host="0.0.0.0", port=CFG["http_port"], debug=False)
