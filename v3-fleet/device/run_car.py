#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3-fleet - 通用模拟车客户端（步骤⑤：管理后台注册 → 开车，配套件）
====================================================================
car_mqtt_client_001.py / _002.py 是 V2 收编定版件（硬编码各自
device_id/密钥），保持原样不动；本脚本是它们的参数化版本——
连接方式、运动模型、上报节奏、命令处理逻辑与 001 完全同款，
仅把身份信息改为命令行参数，供管理后台新注册的设备使用。

用法：
  D:/anaconda3/python.exe run_car.py --node car_003
  D:/anaconda3/python.exe run_car.py --node car_003 --server <设备侧端点>
  D:/anaconda3/python.exe run_car.py --device_id 6ae..._car_003

身份解析规则：
  1. --device_id 直接指定时，密钥仍按 node_id 查（device_id 的
     "_后面"部分），查不到再按完整 device_id 查；
  2. 否则 device_id = {product_id}_{node_id}，product_id 读
     ../server/config.json 的新增字段（与管理后台注册同源，
     云侧/本地规则天然一致）；
  3. 密钥查 ../server/devices_secret.json（管理后台注册时写入）。
     查不到直接报错退出并提示"先在管理后台注册"——云端根本没有
     这台设备，连上去也是鉴权失败 rc=4。

设备侧端点说明：server/config.json 里存的是应用侧专属端点
（iotda-app 域名），设备 MQTT 要连设备侧专属端点（iotda-device
域名，同一实例前缀、端口 8883）。两域名由实例前缀派生：把
iotda-app 替换为 iotda-device 并剥掉 https:// 即可（001/002 硬
编码的正是这个值，规则已对照验证）；--server 可显式覆盖。

MQTT 鉴权（华为云 IoTDA 一机一密标准算法，与 001 同款）：
  clientId  = {device_id}_0_1_{timestamp}
  username  = {device_id}
  password  = hmacsha256(密钥=UTC时间戳, 内容=secret)
  timestamp = UTC 时间，格式 YYYYMMDDHH

依赖：pip install paho-mqtt
"""
import argparse
import hashlib
import hmac
import json
import math
import os
import ssl
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt

SERVER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "server")
CONFIG_PATH = os.path.join(SERVER_DIR, "config.json")
SECRET_PATH = os.path.join(SERVER_DIR, "devices_secret.json")

# ---------------------------------------------------------------- 运动模型参数（与 001 一致）
FIELD_W, FIELD_H = 800, 600
TICK_SEC = 1.0                # 运动模型每秒更新一次
TURN_STEP = 15
DEFAULT_SPEED = 60

# 属性上报节奏（省每日 1 万条消息额度；与 001 同款，勿调快）
REPORT_INTERVAL_MOVING = 5    # 车在动：每 5 秒报一次
REPORT_INTERVAL_IDLE   = 60   # 车停着：每 60 秒报一次（证明还在线）


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 身份解析
def load_identity(args):
    """从命令行 + server 侧配置文件解析出 (device_id, secret, endpoint)。

    放在函数而非模块级：报错信息要带 --node 提示，且便于自测。
    """
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"[启动失败] 找不到 {CONFIG_PATH}（本脚本依赖 server 侧配置）")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    product_id = cfg.get("product_id")
    if not product_id:
        sys.exit("[启动失败] config.json 缺少 product_id 字段（管理后台 v1.2 新增）")

    # 设备侧端点：默认从应用侧端点派生（替换 iotda-app → iotda-device），
    # --server 可覆盖
    endpoint = args.server
    if not endpoint:
        app_ep = cfg.get("endpoint", "")
        if "iotda-app" not in app_ep:
            sys.exit("[启动失败] config.json 的 endpoint 不是 iotda-app 域名，"
                     "无法派生设备侧端点，请用 --server 显式指定")
        endpoint = app_ep.replace("https://", "").replace("iotda-app", "iotda-device")

    node_id = args.node
    if args.device_id:
        device_id = args.device_id
        # node_id 未显式给时从 device_id 拆（密钥表按 node_id 也能查到，
        # 两种键都兼容，见函数头注释）
        if node_id is None and "_" in device_id:
            node_id = device_id.split("_", 1)[1]
    else:
        if node_id is None:
            sys.exit("[用法] 必须给 --node car_003 或 --device_id <完整ID>")
        device_id = f"{product_id}_{node_id}"
    if node_id is None:
        sys.exit("[用法] --device_id 模式下无法推断 node_id，请补 --node")

    if not os.path.exists(SECRET_PATH):
        sys.exit(f"[启动失败] 找不到 {SECRET_PATH}\n"
                 f"设备 {device_id} 尚未注册：请先在管理后台（web/admin.html）"
                 f"注册该设备，拿到密钥后再运行本脚本。")
    with open(SECRET_PATH, encoding="utf-8") as f:
        secrets_map = json.load(f).get("secrets", {})
    # 优先按 node_id 查，再按完整 device_id 查（密钥表两代键型兼容）
    secret = secrets_map.get(node_id) or secrets_map.get(device_id)
    if not secret:
        sys.exit(f"[启动失败] 密钥表里没有 {device_id}（node_id={node_id}）\n"
                 f"请先在管理后台注册该设备（注册成功会自动写入密钥表），再运行本脚本。")
    return device_id, secret, endpoint


# ---------------------------------------------------------------- 运动模型（与 001 同款）
class Car:
    """小车状态：位置、朝向（0°=朝上，顺时针增）、带符号速度、电量"""

    def __init__(self):
        self.x = FIELD_W / 2
        self.y = FIELD_H / 2
        self.heading = 0
        self.speed = 0
        self.battery = 87.0

    def step(self):
        if self.speed != 0:
            rad = math.radians(self.heading)
            self.x += self.speed * 0.02 * math.sin(rad) * 10   # 1s/格，步长放大 10 倍保持速度感
            self.y -= self.speed * 0.02 * math.cos(rad) * 10
            self.x = min(max(self.x, 10), FIELD_W - 10)
            self.y = min(max(self.y, 10), FIELD_H - 10)
            self.battery = max(20.0, self.battery - 0.01)

    def properties(self):
        return {
            "x": round(self.x),
            "y": round(self.y),
            "heading": self.heading % 360,
            "speed": self.speed,
            "battery": round(self.battery, 1),
        }


car = Car()
DEVICE_ID = None   # main() 里按参数赋值（001 是模块级常量，这里是变量）
SECRET = None


# ---------------------------------------------------------------- MQTT 鉴权三要素（与 001 同款）
def mqtt_credentials():
    """华为云 IoTDA 标准鉴权：timestamp 用 UTC（不是北京时间！）
    password = hmacsha256(密钥=timestamp, 内容=secret)"""
    ts = datetime.utcnow().strftime("%Y%m%d%H")
    digest = hmac.new(ts.encode("utf-8"),
                      SECRET.encode("utf-8"), hashlib.sha256).hexdigest()
    client_id = f"{DEVICE_ID}_0_1_{ts}"
    return client_id, DEVICE_ID, digest


# ---------------------------------------------------------------- 命令解析（与 001 同款）
def apply_command(name, paras):
    """平台命令 -> 运动模型。返回给平台的响应负载（None 表示未知命令）"""
    name = (name or "").upper()
    if name in ("FORWARD", "BACK"):
        speed = DEFAULT_SPEED
        if isinstance(paras, dict) and "speed" in paras:
            speed = max(0, min(100, int(paras["speed"])))
        car.speed = speed if name == "FORWARD" else -speed
        return f"{name} 已执行 speed={car.speed}"
    if name == "LEFT":
        car.heading = (car.heading - TURN_STEP) % 360
        return f"LEFT 已执行 heading={car.heading % 360}"
    if name == "RIGHT":
        car.heading = (car.heading + TURN_STEP) % 360
        return f"RIGHT 已执行 heading={car.heading % 360}"
    if name == "STOP":
        car.speed = 0
        return "STOP 已执行"
    return None


def report_properties(client):
    payload = json.dumps({"services": [{"service_id": "car",
                                        "properties": car.properties()}]},
                         ensure_ascii=False)
    topic = f"$oc/devices/{DEVICE_ID}/sys/properties/report"
    client.publish(topic, payload, qos=1)
    log(f"[上报] {car.properties()}")


# ---------------------------------------------------------------- paho 回调（与 001 同款）
def on_connect(client, userdata, flags, rc, props=None):
    if rc == 0:
        log("[连接] 已连上华为云 IoTDA，设备状态应为『在线』")
        client.subscribe(f"$oc/devices/{DEVICE_ID}/sys/commands/#", qos=1)
        log(f"[订阅] 命令主题 $oc/devices/{DEVICE_ID}/sys/commands/#")
        report_properties(client)      # 上线即报一帧
    else:
        log(f"[连接] 失败 rc={rc}（常见：4=鉴权失败 5=未授权/已冻结，"
            f"检查设备ID/密钥/是否在管理后台被冻结）")


def on_command(client, userdata, msg):
    """收到平台下发的命令：解析 -> 驱动运动模型 -> 回命令响应"""
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        name = data.get("command_name", "")
        paras = data.get("paras") or {}
        req_id = ""
        for part in msg.topic.split("/"):
            if part.startswith("request_id="):
                req_id = part[len("request_id="):]
        log(f"[<-云] 命令 {name} 参数 {paras}")

        result = apply_command(name, paras)
        if result is None:
            log(f"[忽略] 平台收到未定义命令 {name}")
            return

        resp_topic = f"$oc/devices/{DEVICE_ID}/sys/commands/response/request_id={req_id}"
        resp = json.dumps({"result_code": 0, "response_name": name},
                          ensure_ascii=False)
        client.publish(resp_topic, resp, qos=1)
        log(f"[->云] 命令响应 {name} 成功（{result}）")
        report_properties(client)      # 执行完立即报一帧，控制台马上看到变化
    except Exception as e:             # noqa: BLE001
        log(f"[异常] 处理命令出错: {e}")


# ---------------------------------------------------------------- 主循环（与 001 同款）
def main():
    parser = argparse.ArgumentParser(description="V3 通用模拟车客户端")
    parser.add_argument("--node", help="节点ID，如 car_003（device_id 自动拼 product_id）")
    parser.add_argument("--device_id", help="完整 device_id（覆盖 --node 拼接）")
    parser.add_argument("--server", help="设备侧 MQTT 端点（默认从 server/config.json 派生）")
    args = parser.parse_args()

    global DEVICE_ID, SECRET
    DEVICE_ID, SECRET, endpoint = load_identity(args)
    PORT = 8883   # MQTTS（TLS 加密）

    client_id, username, password = mqtt_credentials()
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                         client_id=client_id,
                         clean_session=True)
    client.username_pw_set(username, password)
    client.tls_set(cert_reqs=ssl.CERT_NONE)      # 教学环境跳过平台证书校验
    client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_command

    log(f"[启动] 连接 {endpoint}:{PORT}（MQTTS/TLS）")
    log(f"[鉴权] deviceId={DEVICE_ID}")
    client.connect(endpoint, PORT, keepalive=120)
    client.loop_start()

    last_state = None
    last_report = 0.0
    try:
        while True:
            car.step()
            now = time.time()
            state = car.properties()
            interval = REPORT_INTERVAL_MOVING if car.speed != 0 else REPORT_INTERVAL_IDLE
            if now - last_report >= interval:
                report_properties(client)
                last_report = now
            last_state = state
            time.sleep(TICK_SEC)
    except KeyboardInterrupt:
        log("[退出] 手动停止")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
