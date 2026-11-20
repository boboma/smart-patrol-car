#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3-fleet - 车队模拟器启动器（步骤⑤ 升级：自动扫描注册表）
==========================================================
作用：一个命令拉起注册表里所有"未冻结"的模拟车进程，
      替代"每台车开一个终端"的手工操作。

v1.1 升级说明（为什么改）：
  步骤⑤ 之前注册表只有 001/002 两台静态车，启动器写死两个脚本即可；
  步骤⑤ 引入管理后台后，新设备（如 car_003）注册即生效，启动器若仍
  写死名单，每注册一辆车就得改一次代码——违背"数据驱动"的初衷。
  现在启动时自动扫描 ../server/devices.json，按车的"出身"选择脚本：
    - car_001/car_002 → V2 收编的 car_mqtt_client_001/002.py（密钥
      硬编码在脚本里，行为与 V2 完全一致，这两份定版件不动）
    - 其他（动态注册的新车）→ run_car.py --node <节点ID>（密钥从
      ../server/devices_secret.json 读，管理后台注册时已落盘）
  跳过规则：frozen=true 的车（冻结的意义就是禁止连云）；
           密钥查不到的车（提示先去管理后台注册）。

⚠️ 启动前必须确认：V2（iot-stage/server）的同名模拟器没有在跑！
   华为云 IoTDA 同一 device_id 只允许一个 MQTT 连接，
   两处同时跑会互相踢下线（表现为反复重连、rc=4/5 或频繁掉线），
   影子数据也会被踢线方污染。这是 V2 已踩过的坑（双实例互踩）。
   同理，本启动器也别重复启动（先 Ctrl+C 旧实例再跑新的）。

实现要点（为什么不用 .bat）：
  1. Windows 的 cmd 对中文路径编码不可控（GBK/UTF-8 混杂），Python 脚本
     内部全程 UTF-8 处理路径，避开这个坑
  2. subprocess.Popen 各起一个子进程，Ctrl+C 时向每个子进程发
     CTRL_BREAK_EVENT，让它们各自走自己的 KeyboardInterrupt 收尾逻辑
     （正常断开 MQTT、打印退出日志），比直接 kill 干净

运行：D:/anaconda3/python.exe start_fleet.py
依赖：无额外依赖（paho-mqtt 由子脚本自己用）
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "server"))
REGISTRY_PATH = os.path.join(SERVER_DIR, "devices.json")
SECRETS_PATH = os.path.join(SERVER_DIR, "devices_secret.json")

# 与主脚本同一解释器（D:/anaconda3/python.exe，SDK/paho 均已就绪）
PYTHON = sys.executable

# V2 收编的静态脚本（节点ID → 脚本名）。这两份文件密钥硬编码、行为定版，
# 优先匹配它们以保证 001/002 行为与步骤①②③ 验收时完全一致；
# 注册表里其余的车一律走 run_car.py（密钥从 devices_secret.json 读）
LEGACY_SCRIPTS = {
    "car_001": "car_mqtt_client_001.py",
    "car_002": "car_mqtt_client_002.py",
}


def log(msg):
    print(f"[fleet {datetime.now():%H:%M:%S}] {msg}", flush=True)


def scan_workers():
    """扫描设备注册表，返回 [(标签, 命令参数列表), ...]。

    只做"读注册表 + 拼命令"，不连云、不做网络操作——启动器保持
    纯本地逻辑，坏数据（格式不对）在这里就该暴露，而不是上云才炸。
    """
    if not os.path.isfile(REGISTRY_PATH):
        log(f"[中止] 找不到注册表 {REGISTRY_PATH}")
        sys.exit(1)
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        registry = json.load(f)
    devices = registry.get("devices", [])
    if not devices:
        log("[中止] 注册表为空：请先在管理后台注册设备，或检查 devices.json")
        sys.exit(1)

    secrets = {}
    if os.path.isfile(SECRETS_PATH):
        with open(SECRETS_PATH, encoding="utf-8") as f:
            # 文件结构为 {"_comment": ..., "secrets": {device_id: 密钥}}，
            # 密钥表嵌在 secrets 键下（与 admin.py/run_car.py 的写读约定一致）
            secrets = json.load(f).get("secrets", {})

    workers = []
    for dev in devices:
        device_id = dev.get("device_id", "")
        name = dev.get("name", device_id)
        # device_id 格式为 {产品ID}_{节点ID}，节点ID 自身可含下划线，
        # 因此只在第一个下划线处切一次
        node_id = device_id.split("_", 1)[1] if "_" in device_id else device_id

        if dev.get("frozen"):
            log(f"[跳过] {name} 已冻结（云侧禁连），不启动")
            continue
        if dev.get("frozen") is None and "frozen" not in dev:
            pass  # 旧格式无 frozen 字段，视为未冻结，正常启动

        legacy = LEGACY_SCRIPTS.get(node_id)
        if legacy and os.path.isfile(os.path.join(BASE_DIR, legacy)):
            workers.append((f"{name}[{node_id}]", [PYTHON, os.path.join(BASE_DIR, legacy)]))
        elif device_id in secrets:
            # 动态注册的新车：run_car.py 自己会校验密钥与参数，
            # 这里只负责把节点 ID 递给它
            workers.append((f"{name}[{node_id}]",
                            [PYTHON, os.path.join(BASE_DIR, "run_car.py"), "--node", node_id]))
        else:
            log(f"[跳过] {name} 密钥不在 devices_secret.json："
                f"请先在管理后台注册，或补录密钥后重试")
    return workers


def main():
    log("=" * 56)
    log(" V3 车队模拟器启动器（自动扫描注册表）")
    log(" ⚠️ 启动前确认 V2 同名模拟器与旧的本启动器实例都没在跑")
    log("=" * 56)

    workers = scan_workers()
    if not workers:
        log("[退出] 没有可启动的车辆（全部被冻结或缺少密钥）")
        sys.exit(0)
    for label, _ in workers:
        log(f"[计划] {label}")

    # CREATE_NEW_PROCESS_GROUP：让子进程成为独立进程组，
    # 之后才能对它发 CTRL_BREAK_EVENT（CTRL_C_EVENT 只发给调用者所在组）
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    procs = []
    for label, cmd in workers:
        if not os.path.isfile(cmd[1]):
            log(f"[中止] 找不到 {cmd[1]}，请检查 device/ 目录完整性")
            for p in procs:                 # 已启动的先收掉，不留半截
                p.terminate()
            sys.exit(1)
        log(f"[启动] {label}")
        procs.append(subprocess.Popen(cmd, creationflags=creationflags))

    log(f"[就绪] {len(procs)} 台模拟车已拉起，Ctrl+C 可同时停止全部")

    try:
        # 主进程只负责"等待"，子进程各自打印自己的连接/上报日志
        while True:
            time.sleep(1)
            for (label, _), p in zip(workers, procs):
                rc = p.poll()
                if rc is not None:
                    # 某台车意外退出（比如被别的实例踢线后崩了）：提醒但不自动重启，
                    # 教学阶段宁可显式重跑，避免掩盖问题
                    log(f"[警告] {label} 已退出 rc={rc}，如需重跑请 Ctrl+C 后重新启动")
            if all(p.poll() is not None for p in procs):
                log("[退出] 所有子进程均已结束")
                break
    except KeyboardInterrupt:
        log("[停止] 收到 Ctrl+C，正在结束所有子进程…")
        for (label, _), p in zip(workers, procs):
            if p.poll() is None:
                try:
                    if os.name == "nt":
                        # 发 CTRL_BREAK_EVENT 而不是 terminate()：
                        # 让子脚本走自己的 KeyboardInterrupt 分支，
                        # 正常断开 MQTT 连接、打印退出日志
                        p.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        p.terminate()
                except OSError as e:
                    log(f"[警告] 结束 {label} 失败: {e}，改用强杀")
                    p.kill()
        for p in procs:
            p.wait(timeout=10)
        log("[完成] 全部子进程已停止")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
