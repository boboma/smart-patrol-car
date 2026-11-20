#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3-fleet - 验收/演示驾驶脚本（让双车持续跑动，喂轨迹数据）
==========================================================
场景：
  1. 步骤③ 验收：双车跑动 10 分钟后，用 /track 回查轨迹点序列
  2. 步骤④ 网页演示：让车动起来才有画面
  3. 期末演示前的"热车"

策略（为什么不是一直 FORWARD）：
  车直行会撞到场地边界被钳住，位置不再变化，"变化才记"的落库规则
  就不会再产生新点。所以每 30 秒给每台车打一次方向（1 号车 RIGHT、
  2 号车 LEFT，路线自然分叉），并确认 FORWARD 速度没丢。

运行（前后置由调用者负责）：
  前置：start_fleet.py + app.py 都已启动
  D:/anaconda3/python.exe drive_cars.py --minutes 10
  结束时自动给双车发 STOP。
依赖：requests
"""
import argparse
import sys
import time
from datetime import datetime

import requests

BASE = "http://127.0.0.1:8000"
CARS = [
    ("6ae440347f2e6c302fa846ee_car_001", "RIGHT"),   # 1 号车顺时针
    ("6ae440347f2e6c302fa846ee_car_002", "LEFT"),    # 2 号车逆时针，路线分叉
]
TURN_EVERY = 30      # 每 30 秒打一次方向
SPEED = 50


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def send(device_id, cmd, speed=None):
    body = {"device_id": device_id, "cmd": cmd}
    if speed is not None:
        body["speed"] = speed
    try:
        r = requests.post(f"{BASE}/command", json=body, timeout=30)
        data = r.json()
        return r.status_code, data.get("executed")
    except Exception as e:  # noqa: BLE001 —— 驾驶脚本对单次失败容错
        log(f"  [异常] {device_id} {cmd}: {e}")
        return 0, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=10, help="驾驶时长（分钟）")
    ap.add_argument("--speed", type=int, default=SPEED)
    args = ap.parse_args()

    deadline = time.time() + args.minutes * 60
    log(f"开始驾驶 {args.minutes} 分钟（speed={args.speed}，每 {TURN_EVERY}s 打一次方向）")
    tick = 0
    while time.time() < deadline:
        tick += 1
        remain = int(deadline - time.time())
        for device_id, turn_dir in CARS:
            node = device_id.split("_", 1)[1]
            code, ok = send(device_id, "FORWARD", args.speed)
            if not ok:
                log(f"  [WARN] {node} FORWARD 未回执 (HTTP {code})，继续")
            send(device_id, turn_dir)
        log(f"第 {tick} 轮：双车已续速+转向，剩余 {remain // 60}分{remain % 60}秒")
        time.sleep(min(TURN_EVERY, max(1, deadline - time.time())))

    for device_id, _ in CARS:
        send(device_id, "STOP")
    log("驾驶结束，双车已停车")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
