# -*- coding: utf-8 -*-
"""
v3-fleet - WS 广播接口自测脚本（步骤④验收辅助，协议 7.1）
============================================================
验证 ws_server 的三件事：
  1. 连上 ws://127.0.0.1:8889/ws 后能持续收到 fleet_state 帧
     （连收 3 帧，帧间隔应为约 1 秒）；
  2. 每帧结构合法：type=fleet_state、ts 可读、cars 是非空数组、
     每台车字段齐全（device_id/name/online/x/y/heading/speed/
     battery/time 必须存在；离线车的 x/y 等值允许为 null）；
  3. 心跳：发 {"type":"ping"} 能收到 {"type":"pong"}。

用法（先启动 app.py，再另开终端）：
  cd v3-fleet/server
  D:/anaconda3/python.exe test_ws.py

退出码：全部通过 0，任一失败 1（可被批处理判断）。
"""
import asyncio
import json
import sys

import websockets

WS_URL = "ws://127.0.0.1:8889/ws"
N_FRAMES = 3
# 协议 7.1 帧内每台车必备字段（color 是超集，见用例 2 的宽松附加检查）
REQUIRED_FIELDS = ["device_id", "name", "online", "x", "y",
                   "heading", "speed", "battery", "time"]

results = []  # [(用例名, 是否通过, 说明)]


def record(name, ok, note=""):
    results.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {note}" if note else ""))


async def main():
    print(f"连接 {WS_URL} …")
    try:
        async with websockets.connect(WS_URL, open_timeout=5) as ws:
            # ---------------------------------------- 用例 1/2：连收 3 帧 fleet_state
            for i in range(N_FRAMES):
                name = f"第 {i + 1} 帧 fleet_state"
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
                    record(name, False, f"收帧失败: {e}")
                    break
                try:
                    frame = json.loads(raw)
                except ValueError as e:
                    record(name, False, f"非合法 JSON: {e}")
                    break

                if frame.get("type") != "fleet_state":
                    record(name, False, f"type={frame.get('type')!r} 不对")
                    break
                cars = frame.get("cars")
                if not isinstance(cars, list) or not cars:
                    record(name, False, f"cars 缺失或为空: {type(cars)}")
                    break
                # 字段"存在性"校验：字段必须在（值可为 null，如离线车 x/y）
                problems = []
                for c in cars:
                    missing = [k for k in REQUIRED_FIELDS if k not in c]
                    if missing:
                        problems.append(f"{c.get('device_id')} 缺 {missing}")
                if problems:
                    record(name, False, "; ".join(problems))
                    break
                ts = frame.get("ts")
                ok_ts = isinstance(ts, str) and len(ts) == len("2026-11-07 20:30:00")
                extra = f"ts={ts}，{len(cars)} 车"
                if not ok_ts:
                    record(name, False, f"ts 异常: {ts!r}")
                    break
                if all("color" in c for c in cars):
                    extra += "，含 color 超集"
                record(name, True, extra)

            # ---------------------------------------- 用例 3：心跳 ping → pong
            await ws.send('{"type":"ping"}')
            # pong 会混在每秒一帧的广播流里，最多读 5 条等它出现
            got_pong = False
            for _ in range(5):
                try:
                    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                except (asyncio.TimeoutError, websockets.ConnectionClosed, ValueError):
                    break
                if reply.get("type") == "pong":
                    got_pong = True
                    break
            record("心跳 ping → pong", got_pong,
                   "" if got_pong else "5 次读取内未见到 pong")
    except (OSError, websockets.InvalidURI, websockets.InvalidHandshake,
            asyncio.TimeoutError) as e:
        record("连接 ws://127.0.0.1:8889/ws", False,
               f"{e}（ws_server 未启动？先跑 app.py）")

    # ------------------------------------------------ 汇总
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("=" * 50)
    print(f"结果: {passed}/{total} 通过")
    if total > 0 and passed == total:
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
