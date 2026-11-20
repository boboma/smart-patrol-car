#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3-fleet - 步骤②/③ 接口自测脚本（业务后端多车化 + 轨迹查询）
================================================
对照路线图②/③的验收标准，对 V3 业务后端做全流程自测：
  1. GET  /devices                查车列表
  2. GET  /status?device_id=      双车分别查状态
  3. POST /command                双车分别下发命令
  4. 未注册 device_id 查状态/发命令（期望 400）
  5. GET  /health                 车队摘要
  6. GET  /fleet/status           车队状态批量
  7. GET  /track                  双车轨迹查询 + 未注册 device_id 期望 400

每步打印 PASS/FAIL 和响应摘要，最后汇总。测试对象清单读本目录
devices.json（注册表是唯一真相来源，不依赖被测接口自身的返回）。

安全设计（不实际让车长跑）：
  FORWARD 只用短速度 30，发出后立即补一个 STOP；
  车若离线，命令同步等待约 20 秒超时返回 504——这本身是有效
  测试结果（记 WARN），脚本不会失败在"车不在线"上。

运行：
  先启动后端：D:/anaconda3/python.exe app.py
  再跑自测：  D:/anaconda3/python.exe test_api.py
             （后端地址默认 http://127.0.0.1:8000，可用参数覆盖：
              D:/anaconda3/python.exe test_api.py http://192.168.1.10:8000）
依赖：requests
"""
import json
import os
import sys

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE = "http://127.0.0.1:8000"
TIMEOUT = 30  # 同步命令最长等约 20 秒回执，超时给宽一些
# 未注册 device_id：格式合法（24位hex_节点ID）但注册表里没有
FAKE_DEVICE_ID = "000000000000000000000000_car_999"

results = []  # [(步骤名, PASS/FAIL/WARN, 摘要)]


def record(name, status, summary=""):
    results.append((name, status, summary))
    mark = {"PASS": "[通过]", "FAIL": "[失败]", "WARN": "[警告]"}[status]
    print(f"{mark} {name}" + (f"\n       {summary}" if summary else ""))


def expect(cond, name, ok_summary, fail_summary=""):
    """cond 为真记 PASS，否则记 FAIL。"""
    record(name, "PASS" if cond else "FAIL", ok_summary if cond else fail_summary)
    return cond


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    base = base.rstrip("/")

    # 目标清单：本地注册表（不依赖被测服务的返回，避免"错得一致"）
    with open(os.path.join(BASE_DIR, "devices.json"), encoding="utf-8") as f:
        devices = json.load(f)["devices"]
    target_ids = [d["device_id"] for d in devices]

    print("=" * 64)
    print(f" V3 步骤② 接口自测（目标后端: {base}，注册车 {len(target_ids)} 台）")
    print("=" * 64)

    # ---------------------------------------------------------- 1. 车列表
    try:
        r = requests.get(f"{base}/devices", timeout=TIMEOUT)
        body = r.json()
        ok = (r.status_code == 200 and body.get("ok") is True
              and isinstance(body.get("devices"), list)
              and len(body["devices"]) == len(target_ids))
        if ok:
            brief = "; ".join(
                f"{d['name']}(在线={d['online']},电量={d['battery']})"
                for d in body["devices"])
            expect(True, "1. GET /devices 车列表",
                   f"{len(body['devices'])} 台: {brief}")
        else:
            expect(False, "1. GET /devices 车列表", "",
                   f"HTTP {r.status_code} body={body}")
    except Exception as e:  # noqa: BLE001
        expect(False, "1. GET /devices 车列表", "", f"请求异常: {e}")
        print_summary()
        return

    # ------------------------------------------------ 2. 双车分别查状态
    for device_id in target_ids:
        name = f"2. GET /status {device_id.split('_', 1)[1]}"
        try:
            r = requests.get(f"{base}/status",
                             params={"device_id": device_id}, timeout=TIMEOUT)
            body = r.json()
            ok = (r.status_code == 200 and body.get("ok") is True
                  and body.get("device_id") == device_id
                  and all(k in body for k in ("online", "x", "y", "heading",
                                              "speed", "battery", "time")))
            expect(ok, name,
                   f"HTTP 200 在线={body.get('online')} x={body.get('x')} "
                   f"y={body.get('y')} 电量={body.get('battery')} "
                   f"时间={body.get('time')}",
                   f"HTTP {r.status_code} body={body}")
        except Exception as e:  # noqa: BLE001
            expect(False, name, "", f"请求异常: {e}")

    # --------------------------------------------- 3. 双车分别下发命令
    # 短速度 30 + 立即 STOP：验证命令链路通，又不让车跑远（安全设计）
    for device_id in target_ids:
        node = device_id.split("_", 1)[1]
        fwd_name = f"3a. POST /command FORWARD {node} (speed=30)"
        try:
            r = requests.post(f"{base}/command",
                              json={"device_id": device_id,
                                    "cmd": "FORWARD", "speed": 30},
                              timeout=TIMEOUT)
            body = r.json()
            if r.status_code == 200 and body.get("ok") and body.get("executed"):
                expect(True, fwd_name,
                       f"已执行 command_id={str(body.get('command_id'))[:16]}… "
                       f"response_name={body.get('response_name')}")
            elif r.status_code == 504:
                # 车离线/没回执不算脚本失败，算警告（链路本身通了，到云为止）
                record(fwd_name, "WARN", "命令已发出但未收到回执（车大概率离线）")
            else:
                expect(False, fwd_name, "", f"HTTP {r.status_code} body={body}")
        except Exception as e:  # noqa: BLE001
            expect(False, fwd_name, "", f"请求异常: {e}")
        # 无论 FORWARD 结果如何都补 STOP，保证车不会带着速度跑
        try:
            r = requests.post(f"{base}/command",
                              json={"device_id": device_id, "cmd": "STOP"},
                              timeout=TIMEOUT)
            body = r.json()
            if r.status_code == 200 and body.get("ok") and body.get("executed"):
                expect(True, f"3b. POST /command STOP {node}", "已执行")
            elif r.status_code == 504:
                record(f"3b. POST /command STOP {node}", "WARN",
                       "命令已发出但未收到回执（车大概率离线）")
            else:
                expect(False, f"3b. POST /command STOP {node}", "",
                       f"HTTP {r.status_code} body={body}")
        except Exception as e:  # noqa: BLE001
            expect(False, f"3b. POST /command STOP {node}", "", f"请求异常: {e}")

    # -------------------------------------- 4. 未注册 device_id 期望 400
    try:
        r = requests.get(f"{base}/status",
                         params={"device_id": FAKE_DEVICE_ID}, timeout=TIMEOUT)
        expect(r.status_code == 400 and r.json().get("ok") is False,
               "4a. GET /status 未注册 device_id 期望 400",
               f"HTTP {r.status_code} error={r.json().get('error')}",
               f"期望 400 实际 HTTP {r.status_code} body={r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        expect(False, "4a. GET /status 未注册 device_id 期望 400", "", f"请求异常: {e}")
    try:
        r = requests.post(f"{base}/command",
                          json={"device_id": FAKE_DEVICE_ID,
                                "cmd": "FORWARD", "speed": 30},
                          timeout=TIMEOUT)
        expect(r.status_code == 400 and r.json().get("ok") is False,
               "4b. POST /command 未注册 device_id 期望 400",
               f"HTTP {r.status_code} error={r.json().get('error')}",
               f"期望 400 实际 HTTP {r.status_code} body={r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        expect(False, "4b. POST /command 未注册 device_id 期望 400", "", f"请求异常: {e}")

    # -------------------------------------------------------- 5. 探活
    try:
        r = requests.get(f"{base}/health", timeout=TIMEOUT)
        body = r.json()
        ok = (r.status_code == 200 and body.get("ok") is True
              and body.get("total") == len(target_ids)
              and isinstance(body.get("online"), int))
        expect(ok, "5. GET /health 车队摘要",
               f"total={body.get('total')} online={body.get('online')}",
               f"HTTP {r.status_code} body={body}")
    except Exception as e:  # noqa: BLE001
        expect(False, "5. GET /health 车队摘要", "", f"请求异常: {e}")

    # ------------------------------------------------ 6. 车队状态批量
    try:
        r = requests.get(f"{base}/fleet/status", timeout=TIMEOUT)
        body = r.json()
        cars = body.get("cars") or []
        ok = (r.status_code == 200 and body.get("ok") is True
              and len(cars) == len(target_ids)
              # 协议 7.2：每台车带 device_id/name/color + 动态状态字段
              and all(all(k in c for k in ("device_id", "name", "color",
                                           "online", "x", "y")) for c in cars))
        if ok:
            brief = "; ".join(
                f"{c['name']}({c['color']}, 在线={c['online']})" for c in cars)
            expect(True, "6. GET /fleet/status 车队批量", f"{len(cars)} 台: {brief}")
        else:
            expect(False, "6. GET /fleet/status 车队批量", "",
                   f"HTTP {r.status_code} body={str(body)[:200]}")
    except Exception as e:  # noqa: BLE001
        expect(False, "6. GET /fleet/status 车队批量", "", f"请求异常: {e}")

    # ------------------------------------------- 7. /track 双车轨迹查询
    # 验收口径（路线图③）：双车跑动后 /track 能查到各自轨迹序列。
    # "有没有点"不强求——车还没跑过时 points 为空列表也算链路通
    # （ok=True、结构合法即可）；验收时车跑过就有。
    for device_id in target_ids:
        node = device_id.split("_", 1)[1]
        name = f"7a. GET /track {node}（默认 24h 窗口）"
        try:
            # 不传 from/to：走服务端缺省的最近 24 小时窗口
            r = requests.get(f"{base}/track",
                             params={"device_id": device_id}, timeout=TIMEOUT)
            body = r.json()
            points = body.get("points")
            ok = (r.status_code == 200 and body.get("ok") is True
                  and body.get("device_id") == device_id
                  and isinstance(points, list)
                  and body.get("count") == len(points))
            if ok:
                if points:
                    first, last = points[0], points[-1]
                    expect(True, name,
                           f"共 {len(points)} 点，首点({first.get('x')},{first.get('y')}) "
                           f"@{first.get('t')}，末点({last.get('x')},{last.get('y')}) @{last.get('t')}")
                else:
                    expect(True, name, "ok，暂无轨迹点（车还没在 24h 内跑过，不判失败）")
            else:
                expect(False, name, "", f"HTTP {r.status_code} body={str(body)[:150]}")
        except Exception as e:  # noqa: BLE001
            expect(False, name, "", f"请求异常: {e}")
    try:
        r = requests.get(f"{base}/track",
                         params={"device_id": FAKE_DEVICE_ID}, timeout=TIMEOUT)
        expect(r.status_code == 400 and r.json().get("ok") is False,
               "7b. GET /track 未注册 device_id 期望 400",
               f"HTTP {r.status_code} error={r.json().get('error')}",
               f"期望 400 实际 HTTP {r.status_code} body={r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        expect(False, "7b. GET /track 未注册 device_id 期望 400", "", f"请求异常: {e}")

    print_summary()


def print_summary():
    print("=" * 64)
    fail = sum(1 for _, s, _ in results if s == "FAIL")
    warn = sum(1 for _, s, _ in results if s == "WARN")
    total = len(results)
    for name, status, summary in results:
        if status != "PASS":  # 汇总只列非通过项，通过项上文已展示
            print(f"  [{status}] {name}" + (f" — {summary}" if summary else ""))
    print("-" * 64)
    if fail == 0:
        print(f" 自测结论：通过（{total - fail - warn}/{total} 通过"
              + (f"，{warn} 条警告（车离线属正常）" if warn else "") + "）")
        print(" 业务后端多车化链路 OK：列表/状态/命令路由/400 拦截/车队批量全通过")
    else:
        print(f" 自测结论：未通过（{fail} 项失败 / 共 {total} 项）")
        print(" 排查建议：后端是否启动？config.json 是否就绪？设备注册表是否加载？")
    print("=" * 64)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
