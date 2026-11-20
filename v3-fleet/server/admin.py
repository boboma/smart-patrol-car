# -*- coding: utf-8 -*-
"""
v3-fleet - 管理后台接口（/admin/*，协议文档 v1.2 第 13 节，步骤⑤）
====================================================================
职责：给 B/S 管理后台（web/admin.html）提供四组能力：
  1. 登录保护：单管理员账号（config.json 的 admin_user/admin_pass，
     仅教学环境、仅存本机），签发一次性内存 token，重启即失效；
  2. 设备管理：列表 / 注册新设备（云侧注册 + 注册表落盘 + 密钥
     落盘 + 缓存/WS 刷新）/ 冻结解冻（云侧禁连 + 本地标记）；
  3. 命令历史：commands.log 结构化解析；
  4. 车队总览：在线/离线/冻结统计 + 电量排行 + 消息量按天统计
     （额度监控打底，口径见 track_store.stats_by_day 注释）。

为什么是 Blueprint 挂进 app.py 而不是独立服务：
  管理接口与业务接口共享同一批单例（云客户端 iot_client、车队
  缓存 FLEET_CACHE、注册表 registry）——这些对象由 app.py 构建，
  拆独立进程要么重复构建（凭证两份、缓存两份，状态不同步），
  要么引入进程间通信（复杂度远超收益）。Flask Blueprint 一行
  注册即可复用 CORS 三件套与日志配置，教学项目清晰优先。

依赖注入（app.py 调 admin.init 传入，本模块不反向 import app）：
  - fleet_cache 实例：读设备在线/电量、注册后推送新设备清单；
  - iot_client：云侧 AddDevice / FreezeDevice / UnfreezeDevice；
  - cfg：admin 账号与 product_id。
  注入而非 import 的理由同 fleet_cache/track_store：可脱离 app
  单独测试，也避免模块环。

SDK 类名（v5 实测验证，2026-11-07，import 实测非猜测）：
  - 注册设备：IoTDAClient.add_device(AddDeviceRequest(body=AddDevice(...)))
    注意类名是 AddDevice 而不是 CreateDevice（后者在 iotda v5 里
    只有 AuthenticationTemplate 相关类）；AddDevice 不带 device_id
    时平台按 "{product_id}_{node_id}" 自动拼接——与本工程 device_id
    规则一致，故本地/云侧天然对齐；密钥经 auth_info=AuthInfo(secret=...)
    下发（SECRET 密钥认证，8~32 位字母数字）。
  - 冻结/解冻：freeze_device(FreezeDeviceRequest(device_id=...)) /
    unfreeze_device(UnfreezeDeviceRequest(device_id=...))。
  - 请求一律不带 instance_id（实例专属端点域名已隐含实例，V2 三要素）。
"""
import ast
import json
import logging
import os
import re
import secrets
import threading

from flask import Blueprint, jsonify, request

from huaweicloudsdkcore.exceptions import exceptions as hc_exceptions
from huaweicloudsdkiotda.v5.model import (
    AddDevice,
    AddDeviceRequest,
    AuthInfo,
    FreezeDeviceRequest,
    UnfreezeDeviceRequest,
)

import registry
import track_store
import ws_server

log = logging.getLogger("admin")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMMANDS_LOG = os.path.join(BASE_DIR, "commands.log")
SECRET_PATH = os.path.join(BASE_DIR, "devices_secret.json")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# ------------------------------------------------ 依赖（app.py init 注入）
_fleet_cache = None       # FleetCache 实例（在线/电量数据源）
_iot = None               # IoTDAClient 实例（云侧注册/冻结）
_cfg = None               # config.json 内容（admin 账号 / product_id）

# ------------------------------------------------ 登录态（内存 token）
# 教学环境单管理员：token 只存内存 set，后端重启全部失效（可接受：
# 管理页 401 后自动弹回登录卡片重新登录即可）。不做持久化——
# 持久化 token 就得考虑过期/吊销，复杂度对教学场景不划算。
_tokens: set = set()
_token_lock = threading.Lock()

# node_id 与颜色格式校验（协议 v1.2 第 13.3 节）
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# 新设备密钥：16 位字母数字（华为云 SECRET 密钥要求 8~32 位）
_SECRET_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def init(fleet_cache, iot_client, cfg: dict):
    """app.py 启动时注入依赖并挂载本 blueprint 前调用。"""
    global _fleet_cache, _iot, _cfg
    _fleet_cache = fleet_cache
    _iot = iot_client
    _cfg = cfg


# ---------------------------------------------------------------- 鉴权
def _make_token() -> str:
    token = secrets.token_hex(16)
    with _token_lock:
        _tokens.add(token)
    return token


def _check_token() -> str | None:
    """校验 X-Admin-Token 头。无效返回错误文案，有效返回 None。"""
    token = request.headers.get("X-Admin-Token", "")
    with _token_lock:
        if token and token in _tokens:
            return None
    return "未登录或登录已过期（后端重启会使 token 失效，请重新登录）"


@admin_bp.post("/login")
def login():
    """登录：账号密码比对 config.json 的 admin_user/admin_pass。

    比对用 secrets.compare_digest 常量时间比较（防时序侧信道的
    教学示范；教学场景账号就一对，成本为零）。
    """
    body = request.get_json(silent=True) or {}
    username = body.get("username") or ""
    password = body.get("password") or ""
    ok_u = secrets.compare_digest(username, _cfg.get("admin_user", ""))
    ok_p = secrets.compare_digest(password, _cfg.get("admin_pass", ""))
    if not (ok_u and ok_p):
        log.info("管理后台登录失败 user=%s", username)
        return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
    log.info("管理后台登录成功 user=%s", username)
    return jsonify({"ok": True, "token": _make_token()})


# ---------------------------------------------------------------- 设备管理
@admin_bp.get("/devices")
def list_devices():
    """注册表全量 + 每台的在线状态/电量（读缓存快照）+ frozen 标记。"""
    err = _check_token()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    snapshot = _fleet_cache.get_all()
    result = []
    for dev in registry.devices():
        state = snapshot.get(dev["device_id"]) or {}
        result.append({
            "device_id": dev["device_id"],
            "name": dev["name"],
            "color": dev["color"],
            "frozen": bool(dev.get("frozen")),
            "online": state.get("online", False),
            "battery": state.get("battery"),
        })
    return jsonify({"ok": True, "devices": result})


def _secret_path_load() -> dict:
    """读密钥表（devices_secret.json）。缺文件/缺 _comment 时兜底构造。"""
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    secrets_map = data.get("secrets")
    if not isinstance(secrets_map, dict):
        secrets_map = {}
    return secrets_map


def _secret_path_save(secrets_map: dict):
    """原子写密钥表（与 registry 同款"临时文件 + os.replace"）。

    顺序纪律：注册流程里密钥先于注册表落盘——宁可密钥表多一条
    孤儿记录（无害，可手删），也不能注册表里有设备但密钥丢失
    （那台车永远连不上云）。
    """
    data = {
        "_comment": "设备密钥表（管理后台 v1.2 新增）：device_id -> 设备接入密钥。"
                    "敏感文件，仅存本机，严禁入仓/入 README/入协议文档。",
        "secrets": secrets_map,
    }
    tmp = SECRET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SECRET_PATH)


@admin_bp.post("/devices")
def register_device():
    """注册新设备（完整闭环，任一步失败都不落半截状态）：

    ① 参数校验（node_id/名称/颜色，device_id 查重）
    ② 生成 16 位随机密钥
    ③ 调云侧 add_device 真正注册（失败 502，本地零改动）
    ④ 密钥写 devices_secret.json（先于注册表，理由见 _secret_path_save）
    ⑤ 注册表 add（写 devices.json + 内存更新）
    ⑥ 推送缓存设备清单 + WS 静态映射（新设备进实时广播）
    ⑦ 返回 {ok, device_id, secret}——密钥只此一次明文返回，前端
       醒目展示一次提示管理员保存（云侧查询接口也不回显密钥）。
    """
    err = _check_token()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    body = request.get_json(silent=True) or {}
    node_id = (body.get("node_id") or "").strip()
    name = (body.get("name") or "").strip()
    color = (body.get("color") or "").strip()

    if not NODE_ID_RE.match(node_id):
        return jsonify({"ok": False,
                        "error": "node_id 须为 3~32 位字母/数字/下划线"}), 400
    if not name or len(name) > 64:
        return jsonify({"ok": False, "error": "name 须为 1~64 位非空字符"}), 400
    if not COLOR_RE.match(color):
        return jsonify({"ok": False, "error": "color 须为 #RRGGBB 格式"}), 400

    product_id = _cfg["product_id"]
    device_id = f"{product_id}_{node_id}"
    if registry.get(device_id) is not None:
        return jsonify({"ok": False, "error": f"device_id 已注册: {device_id}"}), 400

    secret = "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(16))

    # ③ 云侧注册：不带 device_id → 平台按 {product_id}_{node_id} 拼接，
    # 与本地规则一致；不带 instance_id（实例专属端点三要素，见头注释）
    req = AddDeviceRequest(body=AddDevice(
        node_id=node_id,
        device_name=name,
        product_id=product_id,
        auth_info=AuthInfo(secret=secret),
    ))
    try:
        _iot.add_device(req)
    except hc_exceptions.SdkException as e:
        log.error("云端注册设备失败 node_id=%s err=%s", node_id, e)
        return jsonify({"ok": False, "error": f"云端注册失败: {e}"}), 502

    # ④⑤ 本地落盘（密钥先于注册表，顺序理由见 _secret_path_save 注释）
    secrets_map = _secret_path_load()
    secrets_map[device_id] = secret
    _secret_path_save(secrets_map)
    entry = registry.add(node_id, name, color, product_id)

    # ⑥ 让新设备进入缓存轮询与 WS 广播（最迟 5 秒后出现在监控页）
    _fleet_cache.set_device_ids(registry.device_ids())
    ws_server.set_static_map(
        {d["device_id"]: {"name": d["name"], "color": d["color"]}
         for d in registry.devices()})

    log.info("设备注册 -> %s name=%s（云侧+注册表+密钥均已落盘）", device_id, name)
    return jsonify({"ok": True, "device_id": entry["device_id"], "secret": secret})


def _set_frozen(device_id: str, frozen: bool):
    """冻结/解冻公共实现：先云侧（真正禁连 MQTT）再本地（注册表标记）。

    顺序理由：云侧失败就 502 且本地不动，保证"本地 frozen=true"
    永远意味着"云侧确实已冻结"；反过来（先本地后云侧）一旦云侧
    失败就出现"本地以为冻了、车其实还能跑"的假象，审计口径被破坏。
    幂等：状态没变直接返回成功，不重复调云。
    """
    err = _check_token()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    if registry.get(device_id) is None:
        return jsonify({"ok": False, "error": f"device_id 未注册: {device_id}"}), 400

    before = registry.is_frozen(device_id)
    if before != frozen:
        req = (FreezeDeviceRequest if frozen else UnfreezeDeviceRequest)(
            device_id=device_id)
        try:
            (_iot.freeze_device if frozen else _iot.unfreeze_device)(req)
        except hc_exceptions.SdkException as e:
            log.error("云侧冻结/解冻失败 device_id=%s frozen=%s err=%s",
                      device_id, frozen, e)
            return jsonify({"ok": False, "error": f"云侧操作失败: {e}"}), 502
        registry.set_frozen(device_id, frozen)
    log.info("设备%s -> %s（云侧已生效，注册表已写回）",
             "冻结" if frozen else "解冻", device_id)
    return jsonify({"ok": True, "device_id": device_id,
                    "frozen": frozen, "before": before})


@admin_bp.post("/devices/<device_id>/freeze")
def freeze_device(device_id):
    return _set_frozen(device_id, True)


@admin_bp.post("/devices/<device_id>/unfreeze")
def unfreeze_device(device_id):
    return _set_frozen(device_id, False)


# ---------------------------------------------------------------- 命令历史
# commands.log 是 logging %(asctime)s %(message)s 格式，命令相关的
# 四类行（app.py /command 打点）：
#   命令下发 -> device_id=X cmd=Y speed=Z            （Z 可能为 None）
#   命令回执 -> device_id=X cmd=Y speed=Z result={'command_id':...,...}
#   命令拒绝 -> X Y 原因=...                          （未注册/冻结/参数错）
#   命令失败 -> device_id=X cmd=Y err=...             （云侧 502）
_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d+)? "
    r"(命令下发|命令回执|命令拒绝|命令失败) -> (.*)$")
_DISPATCH_RE = re.compile(r"^device_id=(\S+) cmd=(\S+)(?: speed=(\S+))?$")
_RECEIPT_RE = re.compile(r"^device_id=(\S+) cmd=(\S+) speed=(\S+) result=(\{.*\})$")
_REJECT_RE = re.compile(r"^(\S+) (\S+) 原因=(.*)$")
_FAIL_RE = re.compile(r"^device_id=(\S+) cmd=(\S+) err=(.*)$")


def _parse_speed(s):
    """日志里的 speed 字段："None" → None，数字串 → int，其他原样。"""
    if s is None or s == "None":
        return None
    try:
        return int(s)
    except ValueError:
        return s


def _parse_log_events():
    """读 commands.log 并解析为事件列表（时间正序）。

    取舍说明：文件里还混有 Flask 访问日志、WS 连接日志等行——它们
    不是命令历史，直接跳过（若全部塞进响应，一条 GET 命令记录都要
    背上几十行噪音）。凡是含四类命令关键字但细节解析失败的行，原样
    放进 raw 字段返回，不丢数据。
    """
    if not os.path.exists(COMMANDS_LOG):
        return []
    events = []
    with open(COMMANDS_LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = _LINE_RE.match(line)
            if not m:
                continue  # 非命令行（访问日志等），跳过
            ts, event, rest = m.group(1), m.group(2), m.group(3)
            ev = {"time": ts, "event": event, "raw": None}
            if event == "命令下发":
                dm = _DISPATCH_RE.match(rest)
                if dm:
                    ev.update(device_id=dm.group(1), cmd=dm.group(2),
                              speed=_parse_speed(dm.group(3)))
            elif event == "命令回执":
                rm = _RECEIPT_RE.match(rest)
                if rm:
                    try:
                        result = ast.literal_eval(rm.group(4))
                    except (ValueError, SyntaxError):
                        result = rm.group(4)  # 字面量解析失败不丢数据
                    ev.update(device_id=rm.group(1), cmd=rm.group(2),
                              speed=_parse_speed(rm.group(3)), result=result)
            elif event == "命令拒绝":
                rjm = _REJECT_RE.match(rest)
                if rjm:
                    ev.update(device_id=rjm.group(1), cmd=rjm.group(2),
                              reason=rjm.group(3))
            else:  # 命令失败
                fm = _FAIL_RE.match(rest)
                if fm:
                    ev.update(device_id=fm.group(1), cmd=fm.group(2),
                              error=fm.group(3))
            if ev.get("device_id") is None:
                # 关键字命中但细节没解析出来：整行放 raw，不丢数据
                ev["raw"] = line
            events.append(ev)
    return events


@admin_bp.get("/commands")
def command_history():
    """命令历史（结构化，最新在前）。

    "下发→回执"配对成一条记录（谁、何时、对哪台车、发了什么、
    结果）：按 (device_id, cmd) 把最近的回执挂到未结的下发事件上；
    配不上的下发记"无回执"（对应 20 秒超时的 504 场景）。拒绝/
    失败本身就是终态，单独成条。
    """
    err = _check_token()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 500))  # 默认 50，上限 500（防单响应过大）

    events = _parse_log_events()
    items = []
    pending = {}  # (device_id, cmd) -> 待结的下发 item（时间正序遍历）
    for ev in events:
        event = ev["event"]
        if event == "命令下发":
            item = {"time": ev["time"], "device_id": ev.get("device_id"),
                    "cmd": ev.get("cmd"), "paras": {}, "status": "已下发",
                    "detail": None, "raw": ev.get("raw")}
            if ev.get("speed") is not None:
                item["paras"]["speed"] = ev["speed"]
            items.append(item)
            if ev.get("device_id") and ev.get("cmd"):
                pending[(ev["device_id"], ev["cmd"])] = item
        elif event == "命令回执":
            item = pending.get((ev.get("device_id"), ev.get("cmd")))
            if item is not None:
                result = ev.get("result") or {}
                item["status"] = "已执行" if result.get("executed") else "无回执"
                item["detail"] = (result.get("response_name")
                                  or result.get("error_code") or result)
            pending.pop((ev.get("device_id"), ev.get("cmd")), None)
        elif event == "命令拒绝":
            items.append({"time": ev["time"], "device_id": ev.get("device_id"),
                          "cmd": ev.get("cmd"), "paras": {}, "status": "已拒绝",
                          "detail": ev.get("reason"), "raw": ev.get("raw")})
        else:  # 命令失败
            items.append({"time": ev["time"], "device_id": ev.get("device_id"),
                          "cmd": ev.get("cmd"), "paras": {}, "status": "下发失败",
                          "detail": ev.get("error"), "raw": ev.get("raw")})

    items.reverse()  # 最新在前
    return jsonify({"ok": True, "items": items[:limit]})


# ---------------------------------------------------------------- 总览
@admin_bp.get("/overview")
def overview():
    """车队总览：统计 + 电量排行 + 消息量（额度监控打底）。"""
    err = _check_token()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    snapshot = _fleet_cache.get_all()
    devices = registry.devices()

    online = sum(1 for d in devices
                 if (snapshot.get(d["device_id"]) or {}).get("online"))
    frozen = sum(1 for d in devices if d.get("frozen"))

    # 电量排行：降序，null（从未上报）排最后——None 参与 max 排序会
    # TypeError，先按 has_battery 分两组再各自排序拼接
    rank = [{"device_id": d["device_id"], "name": d["name"],
             "battery": (snapshot.get(d["device_id"]) or {}).get("battery")}
            for d in devices]
    with_battery = sorted((r for r in rank if r["battery"] is not None),
                          key=lambda r: r["battery"], reverse=True)
    without = [r for r in rank if r["battery"] is None]

    # 消息量：track_points 行数 ≈ 上报帧数 ≈ 额度消耗（近似口径，
    # 为什么是低估的安全侧，见 track_store.stats_by_day 注释）
    stats = track_store.stats_by_day(7)

    return jsonify({
        "ok": True,
        "total": len(devices),
        "online": online,
        "offline": len(devices) - online,
        "frozen": frozen,
        "battery_rank": with_battery + without,
        "messages_today": stats["today"],
        "messages_by_day": stats["by_day"],
    })
