#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iot-stage - 模拟车 MQTT 客户端（华为云 IoTDA 接入版）
=====================================================
与 lab01-tcp-sim（TCP 直连阶段）完全独立，本阶段中小车端角色翻转：
  TCP 阶段：小车端 = TCP 服务端，被动等人来连
  IoT  阶段：小车端 = MQTT 客户端，主动连华为云 IoTDA

链路：
  平台下发命令(FORWARD/BACK/LEFT/RIGHT/STOP) ──推送──> 本脚本（订阅命令主题）
  本脚本每秒更新运动模型 ──属性上报──> 平台（控制台"设备详情-最新上报"可见）

MQTT 鉴权（华为云 IoTDA 一机一密标准算法）：
  clientId  = {device_id}_0_1_{timestamp}
  username  = {device_id}
  password  = hmacsha256(密钥=timestamp, 内容=secret) 的十六进制串
  timestamp = UTC 时间，格式 YYYYMMDDHH（官方规定用 UTC，不是北京时间）

运行：python car_mqtt_client.py
依赖：pip install paho-mqtt
"""
import hashlib
import hmac
import json
import math
import ssl
import time
from datetime import datetime

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------- 配置（本实例专属凭证）
ENDPOINT   = "71b55ab7e9.st1.iotda-device.cn-north-4.myhuaweicloud.com"
PORT       = 8883                      # MQTTS（TLS 加密）
DEVICE_ID  = "6ae440347f2e6c302fa846ee_car_002"
SECRET     = "<设备2密钥，注册时设置或重置后获取>"

# ---------------------------------------------------------------- 运动模型参数（与 lab01 一致）
FIELD_W, FIELD_H = 800, 600
TICK_SEC = 1.0                # 运动模型每秒更新一次（MQTT 阶段不需要 100ms 那么细）
TURN_STEP = 15
DEFAULT_SPEED = 60

# 属性上报节奏（省每日 1 万条消息额度）
REPORT_INTERVAL_MOVING = 5    # 车在动：每 5 秒报一次
REPORT_INTERVAL_IDLE   = 60   # 车停着：每 60 秒报一次（证明还在线）


class Car:
    """小车状态：位置、朝向（0°=朝上，顺时针增）、带符号速度、电量——与 lab01 相同约定"""

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


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- MQTT 鉴权三要素
def mqtt_credentials():
    """华为云 IoTDA 标准鉴权（官方定义）：
    timestamp = UTC 时间，格式 YYYYMMDDHH（注意不是北京时间！）
    password  = hmacsha256(密钥=timestamp, 内容=secret)
    clientId  = 设备ID_0_密码签名类型_时间戳（"1"=HMAC 校验时间戳）
    """
    ts = datetime.utcnow().strftime("%Y%m%d%H")
    digest = hmac.new(ts.encode("utf-8"),
                      SECRET.encode("utf-8"), hashlib.sha256).hexdigest()
    client_id = f"{DEVICE_ID}_0_1_{ts}"
    return client_id, DEVICE_ID, digest


# ---------------------------------------------------------------- 命令解析（与 lab01 的 CMD 语义一致）
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


# ---------------------------------------------------------------- paho 回调
def on_connect(client, userdata, flags, rc, props=None):
    if rc == 0:
        log("[连接] 已连上华为云 IoTDA，设备状态应为『在线』")
        client.subscribe(f"$oc/devices/{DEVICE_ID}/sys/commands/#", qos=1)
        log(f"[订阅] 命令主题 $oc/devices/{DEVICE_ID}/sys/commands/#")
        report_properties(client)      # 上线即报一帧
    else:
        log(f"[连接] 失败 rc={rc}（常见：4=鉴权失败 5=未授权，检查设备ID/密钥/时间戳）")


def on_command(client, userdata, msg):
    """收到平台下发的命令：解析 -> 驱动运动模型 -> 回命令响应"""
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        name = data.get("command_name", "")
        paras = data.get("paras") or {}
        # request_id 在 topic 尾部：.../commands/request_id=xxx
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
    except Exception as e:             # noqa: BLE001 —— 教学脚本，任何解析异常都打出来不崩
        log(f"[异常] 处理命令出错: {e}")


# ---------------------------------------------------------------- 主循环
def main():
    client_id, username, password = mqtt_credentials()
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                         client_id=client_id,
                         clean_session=True)
    client.username_pw_set(username, password)
    client.tls_set(cert_reqs=ssl.CERT_NONE)      # 教学环境跳过平台证书校验
    client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_command

    log(f"[启动] 连接 {ENDPOINT}:{PORT}（MQTTS/TLS）")
    log(f"[鉴权] deviceId={DEVICE_ID}")
    client.connect(ENDPOINT, PORT, keepalive=120)
    client.loop_start()

    last_state = None
    last_report = 0.0
    try:
        while True:
            car.step()
            now = time.time()
            state = car.properties()
            moved = (state != last_state)
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
    if hasattr(__import__("sys").stdout, "reconfigure"):
        __import__("sys").stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
