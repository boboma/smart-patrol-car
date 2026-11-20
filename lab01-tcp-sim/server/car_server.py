#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab01 - 模拟车服务端（阶段 b：TCP + WebSocket 双服务）
======================================================
作用：模拟一台智能寻检车，同时服务两类客户端：
  1) 鸿蒙 APP（TCP 客户端，端口 8888）—— 发指令、收状态
  2) 浏览器俯视图页面（WebSocket 客户端，端口 8889）—— 只收状态，实时渲染小车

浏览器不能直连裸 TCP，所以 visualization 走 WebSocket 中转：
  APP --TCP:8888--> car_server --WebSocket:8889(JSON)--> car_view.html

TCP 协议（文本帧，以 \n 结尾，UTF-8 编码）：
  上行 APP -> 车:
    PING                  心跳，回 PONG
    CMD:FORWARD[:speed]   前进，speed 0~100，省略默认 60
    CMD:BACK[:speed]      后退（speed 记为负值）
    CMD:LEFT / CMD:RIGHT  原地转向，每次 ±15°
    CMD:STOP              停止
    CMD:STATUS            立即回一帧状态
  下行 车 -> APP:
    ACK:xxx               指令回执（已收到并执行）
    PONG                  心跳应答
    STATE:x=..,y=..,heading=..,speed=..,battery=..
                          每 100ms 广播一次（x/y 逻辑坐标，heading 0°=朝上）
    ERR:xxx               非法指令/未知指令

WebSocket 协议（JSON 文本，每 100ms 一帧）：
    {"x":..,"y":..,"heading":..,"speed":..,"battery":..}

运行：python car_server.py [TCP端口]    （默认 8888，WebSocket 固定 8889）
依赖：websockets 库（pip install websockets）；TCP 部分仍为纯标准库
"""
import asyncio
import json
import math
import socket
import sys
from datetime import datetime

try:
    import websockets
except ImportError:
    websockets = None      # 阶段 b 缺库时降级为纯 TCP，给出提示

HOST = "0.0.0.0"
DEFAULT_PORT = 8888
WS_PORT = 8889
FIELD_W, FIELD_H = 800, 600   # 俯视场地（逻辑像素），与浏览器页面共用
TICK_MS = 100                 # 运动更新 / STATE 广播周期
TURN_STEP = 15                # 每次转向角度（原地转）
DEFAULT_SPEED = 60

# ---------------------------------------------------------------- 状态


class Car:
    """小车状态：位置、朝向（0°=朝上，顺时针增）、带符号速度、电量"""

    def __init__(self):
        self.x = FIELD_W / 2
        self.y = FIELD_H / 2
        self.heading = 0
        self.speed = 0          # +前进 / -后退
        self.battery = 87.0

    def step(self):
        """每个 tick 推进一次运动模型"""
        if self.speed != 0:
            rad = math.radians(self.heading)
            self.x += self.speed * 0.02 * math.sin(rad)
            self.y -= self.speed * 0.02 * math.cos(rad)
            self.x = min(max(self.x, 10), FIELD_W - 10)   # 撞边界：clamp
            self.y = min(max(self.y, 10), FIELD_H - 10)
            self.battery = max(20.0, self.battery - 0.01)  # 动起来才耗电

    def state_frame(self):
        return (f"STATE:x={self.x:.0f},y={self.y:.0f},"
                f"heading={self.heading % 360},speed={self.speed},"
                f"battery={self.battery:.0f}")


car = Car()
clients = set()       # 所有已连接的 TCP 客户端 writer（APP 端 / 测试客户端）
ws_clients = set()    # 所有已连接的 WebSocket 客户端（浏览器俯视图页面）

# ---------------------------------------------------------------- 工具


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def local_ip():
    """探测本机局域网 IP（不真正发包）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

# ---------------------------------------------------------------- 协议解析


def handle_command(text):
    """解析一行指令，返回应答帧列表"""
    if text == "PING":
        return ["PONG"]
    if not text.startswith("CMD:"):
        return [f"ERR:BAD_FRAME:{text}"]

    parts = text.split(":")
    action = parts[1].strip().upper()

    if action in ("FORWARD", "BACK"):
        speed = DEFAULT_SPEED
        if len(parts) >= 3:
            try:
                speed = max(0, min(100, int(parts[2])))
            except ValueError:
                return [f"ERR:BAD_SPEED:{parts[2]}"]
        car.speed = speed if action == "FORWARD" else -speed
        return [f"ACK:{action}:{abs(car.speed)}"]

    if action == "LEFT":
        car.heading = (car.heading - TURN_STEP) % 360
        return [f"ACK:LEFT:{car.heading}"]
    if action == "RIGHT":
        car.heading = (car.heading + TURN_STEP) % 360
        return [f"ACK:RIGHT:{car.heading}"]
    if action == "STOP":
        car.speed = 0
        return ["ACK:STOP"]
    if action == "STATUS":
        return [car.state_frame()]
    return [f"ERR:UNKNOWN_CMD:{action}"]

# ---------------------------------------------------------------- 连接处理


async def client_handler(reader, writer):
    addr = writer.get_extra_info("peername")
    log(f"[连接] 新客户端进入 {addr[0]}:{addr[1]}")
    clients.add(writer)
    try:
        # 接入即推一帧当前状态
        writer.write((car.state_frame() + "\n").encode("utf-8"))
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                log(f"[断开] 客户端正常退出 {addr[0]}:{addr[1]}")
                break
            text = line.decode("utf-8").strip()
            if not text:
                continue
            log(f"[<-APP] {text}   来自 {addr[0]}:{addr[1]}")
            for reply in handle_command(text):
                log(f"[->APP] {reply}")
                writer.write((reply + "\n").encode("utf-8"))
            await writer.drain()
    except (ConnectionError, UnicodeDecodeError) as e:
        log(f"[断开] 客户端异常断开 {addr[0]}:{addr[1]}: {e}")
    finally:
        clients.discard(writer)
        writer.close()

# ---------------------------------------------------------------- WebSocket（浏览器俯视图）


def state_json():
    """小车状态 -> JSON 字符串（给浏览器页面）"""
    return json.dumps({
        "x": round(car.x, 1),
        "y": round(car.y, 1),
        "heading": car.heading % 360,
        "speed": car.speed,
        "battery": round(car.battery, 1),
    })


async def ws_handler(ws):
    peer = ws.remote_address
    log(f"[连接] 浏览器俯视图接入 ws://{peer[0]}:{peer[1]}")
    ws_clients.add(ws)
    try:
        # 接入即推一帧当前状态，页面立刻有画面
        await ws.send(state_json())
        # 浏览器是"只看不控"的观察端，这里只负责收（忽略其发来的任何消息）
        async for _ in ws:
            pass
    except websockets.ConnectionClosed:
        pass
    finally:
        ws_clients.discard(ws)
        log(f"[断开] 浏览器俯视图离开 {peer[0]}:{peer[1]}")


# ---------------------------------------------------------------- 运动循环


async def tick_loop():
    """每 100ms：更新小车运动模型，STATE 广播给 TCP 客户端、JSON 推给浏览器"""
    while True:
        await asyncio.sleep(TICK_MS / 1000)
        car.step()
        frame = (car.state_frame() + "\n").encode("utf-8")
        dead = []
        for w in list(clients):
            try:
                w.write(frame)
                await w.drain()
            except (ConnectionError, RuntimeError):
                dead.append(w)
        for w in dead:
            clients.discard(w)

        # WebSocket 广播（JSON 帧）
        if ws_clients:
            payload = state_json()
            dead_ws = []
            for ws in list(ws_clients):
                try:
                    await ws.send(payload)
                except websockets.ConnectionClosed:
                    dead_ws.append(ws)
            for ws in dead_ws:
                ws_clients.discard(ws)

# ---------------------------------------------------------------- 启动


async def main():
    port = DEFAULT_PORT
    if len(sys.argv) >= 2 and sys.argv[1].isdigit():
        port = int(sys.argv[1])

    ip = local_ip()
    print("=" * 46)
    print(" Lab01 模拟车服务端（阶段 b：TCP + WebSocket）")
    print(f" 本机局域网 IP : {ip}")
    print(f" TCP 监听      : {HOST}:{port}   <- 鸿蒙 APP（控制端）")
    if websockets is not None:
        print(f" WebSocket 监听: {HOST}:{WS_PORT}   <- 浏览器俯视图")
        print(f" 俯视图页面    : 打开 web/car_view.html 即可看到小车")
    else:
        print(" WebSocket     : 未安装 websockets 库，俯视图不可用")
        print("               : pip install websockets 后重启本脚本")
    print(f" 自测命令      : python test_client.py {ip} --auto")
    print(" 防火墙提示    : 首次运行请允许 Python 访问网络")
    print("=" * 46, flush=True)

    server = await asyncio.start_server(client_handler, HOST, port)
    if websockets is not None:
        ws_server = await websockets.serve(ws_handler, HOST, WS_PORT)
    else:
        ws_server = None
    asyncio.create_task(tick_loop())
    async with server:
        if ws_server is not None:
            await ws_server.serve_forever()
        await server.serve_forever()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
