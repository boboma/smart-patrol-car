# -*- coding: utf-8 -*-
"""
v3-fleet - 业务后端（车队版，从 V2 iot-stage/server/app.py 改造）
================================================================
架构角色：APP（控制端）/ 网页端 → 业务后端（本服务）→ 华为云 IoTDA → 多台小车
协议依据：《V3-接口协议设计.md》v1.0 第 2/3/4 节 + 第 9 节差异清单

V2 → V3 的三点核心差异（其余 SDK 用法、命令白名单、CORS 均沿用 V2）：
  1. 多车路由：V2 的 device_id 写死在 config.json（单台）；V3 从
     devices.json 设备注册表加载，每个请求按 body/参数里的 device_id
     路由（云侧天然按 device_id 路由，V3 零改动，改的只是调用来源）。
  2. 设备注册表：启动时加载并校验 devices.json（registry.py），
     校验失败拒绝启动；未注册的 device_id 一律 400。
  3. 车队状态缓存：新增 fleet_cache.py 后台线程定时轮询全部设备的
     在线状态与影子，/devices、/fleet/status、/health 直接读缓存，
     为步骤④的 WebSocket 广播打底（5 秒节奏对齐设备上报，见 D3）。
  4. 轨迹落库（步骤③）：缓存刷新处经 on_record 回调把每台车的
     状态变化写入 SQLite（track_store.py，"变化才记"），/track 按
     时间窗查询轨迹供回放。回调注入而非直接 import，保持缓存与
     存储解耦（详见 fleet_cache.py / track_store.py 头注释）。
  5. WS 广播（步骤④）：ws_server.py 在独立线程/端口 8889 跑
     asyncio 事件循环，每 1 秒把缓存快照广播成 fleet_state 帧
     （协议 7.1）；Flask 与 websockets 是 WSGI/asyncio 两套模型，
     独立共存的理由见 ws_server.py 头注释。
  6. 管理后台（步骤⑤，v1.2）：admin.py 以 Blueprint 挂载 /admin/*
     六个接口（登录/设备增查/冻结解冻/命令历史/总览），共享本
     文件的云客户端与缓存单例（为什么不拆独立服务见 admin.py
     头注释）；/command 增加 frozen 拦截，/devices 响应增加
     frozen 字段（超集，向后兼容）。
  7. AI 增强（步骤⑦，v1.4）：ai_service.py 提供 ASR（FunASR 本地 /
     qwen 云端二选一，config.json 的 ai.asr_provider 切换、改后需
     重启）与 LLM 指令解析（DeepSeek）。新增 POST /ai/asr 与
     POST /ai/command（协议文档第 16 节）。设计原则：AI 负责听懂、
     后端负责只允许安全的事发生——命令下发抽成 dispatch_command()
     与 /command 复用同一条链路，冻结拦截/审计日志/回执后立即刷
     缓存这些既有能力对 AI 指令自然生效；启动时 preload_async()
     后台预热 FunASR（约 30 秒），不阻塞 Flask 起端口。

HTTP 接口（协议文档第 4/7/13/16 节，响应包络 {"ok":true,...} / {"ok":false,"error":...}）：
  GET  /devices              车列表（device_id/name/online/battery/frozen）【升级】
  GET  /status?device_id=    单车状态（device_id 必填，未注册 400）【升级】
  POST /command              下发命令（device_id 必填，路由到对应车）【升级】
       body: {"device_id": "...", "cmd": "FORWARD", "speed": 80}
       cmd   ∈ FORWARD / BACK / LEFT / RIGHT / STOP
       speed 仅 FORWARD/BACK 需要，0~100 整数
       冻结设备返回 400 "设备已冻结，命令已拒绝"
  GET  /fleet/status         车队状态批量（缓存快照）【新增】
  GET  /track                轨迹查询（协议 7.3，时间窗可省略默认 24h）【新增】
  GET  /health               探活 + 车队摘要 total/online【升级】
  *    /admin/*              管理后台六接口（v1.2，鉴权见协议 13.1）【新增】
  POST /ai/asr               语音转文字（步骤⑦，body=音频字节，协议 16.2）【新增】
  POST /ai/command           自然语言控车（步骤⑦，body={"text","device_id"?}，协议 16.3）【新增】

错误码（协议文档 4.5 / 16.5）：400 参数错误 / 502 云 API 或 AI 服务失败 / 504 命令无回执
审计日志：commands.log（每条命令记录 device_id + cmd + speed + 结果）
运行：python app.py（读取同目录 config.json，默认端口 8000）
依赖：flask、huaweicloudsdkcore、huaweicloudsdkiotda（v5）、requests、
      registry/fleet_cache/track_store/ws_server/admin/ai_service
"""
import io
import json
import logging
import os
import threading
import time
import wave
from datetime import datetime
from decimal import Decimal

import requests
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

import admin
import ai_service
import fleet_cache
import registry
import track_store
import ws_server

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 配置加载
# V3 config.json 只存凭证与端口，不再有 device_id（V2 写死单台的历史遗留，
# V3 设备清单以 devices.json 为唯一真相来源）。缺文件直接报错退出，
# 错误信息里给出生成指引（该文件含 AK/SK 只存本机勿入仓）。
_cfg_path = os.path.join(BASE_DIR, "config.json")
if not os.path.exists(_cfg_path):
    raise SystemExit(
        f"[启动失败] 找不到 {_cfg_path}\n"
        "请从 iot-stage/server/config.json 复制 ak/sk/project_id/region/"
        "endpoint/http_port 生成（不要带 device_id，仅存本机勿入仓）。")
with open(_cfg_path, encoding="utf-8") as f:
    CFG = json.load(f)

# ---------------------------------------------------------------- 设备注册表
# 启动即校验（格式/必填字段/重复 id），脏数据宁可拒绝启动也不带病路由。
# v1.2：registry 升级为可写模块（管理后台注册/冻结经它落盘），
# 全部读写走 registry.devices()/get()/is_frozen()——app.py 不再持有
# 启动时刻的旧列表引用，否则管理端注册的新车对本进程不可见。
registry.init()

# ---------------------------------------------------------------- 审计日志
# 管控层可见的证据：谁在何时对哪台车下发了什么、结果如何。
# V3 相对 V2：每条记录必须带 device_id（多车场景下没有它审计无从谈起）。
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
# 官方指南用法（标准版实例三要素，V2 已验证，原样沿用）：
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

# ---------------------------------------------------------------- 轨迹存储（步骤③）
# 启动即建库建表（幂等）并跑一轮数据治理：把停机期间到期的旧数据
# 清掉（7 天过期 + 单车 2 万条封顶），清理结果打日志留痕。
track_store.init()
_cleaned = track_store.cleanup()
log.info("轨迹库就绪: %s（现有 %d 行）；启动清理: 过期删除 %d 行, 单车封顶删除 %s",
         track_store.DB_PATH, track_store.count(), _cleaned["expired"],
         _cleaned["capped"] or "无")


def _on_record(device_id: str, state: dict, changed: bool):
    """FleetCache → track_store 的落库回调（缓存刷新处挂点，步骤③）。

    去重逻辑收在 track_store.record 内部（"变化才记"），这里只做
    转发；changed 参数留给将来需要区分"首帧/变化帧"的消费者。
    该回调在缓存锁外被调用，record 内部再拿 store 自己的锁，
    两把锁不嵌套（详见 fleet_cache.py 头注释）。
    """
    track_store.record(device_id, state)


# ---------------------------------------------------------------- 车队状态缓存
# 后台守护线程每 5 秒轮询全部注册设备的在线状态与影子（决策 D3），
# /devices、/fleet/status、/health 读缓存；/status、/command 走实时调用。
# on_record 注入轨迹落库回调：缓存是后端唯一的数据汇聚点，步骤④
# WS 广播也从这走（见 fleet_cache.py 头注释）。
FLEET_CACHE = fleet_cache.FleetCache(iot_client, registry.devices(), on_record=_on_record)
FLEET_CACHE.start()

# ---------------------------------------------------------------- WS 广播（步骤④）
# 协议 7.1 主通道：每 1 秒向网页广播 fleet_state。放在缓存启动之后
# （广播的数据源就是缓存）；静态信息映射由这里从注册表组装传入——
# ws_server 反向 import registry 会造成模块环，单向注入保持解耦。
# v1.2：管理端注册新设备后经 ws_server.set_static_map() 换新映射。
ws_server.start(FLEET_CACHE, {d["device_id"]: {"name": d["name"], "color": d["color"]}
                              for d in registry.devices()})

COMMAND_PARAS = {"FORWARD": ("speed",), "BACK": ("speed",), "LEFT": (), "RIGHT": (), "STOP": ()}

app = Flask(__name__)

# ---------------------------------------------------------------- 管理后台（步骤⑤，v1.2）
# 依赖注入 + Blueprint 挂载：管理接口与业务接口共享云客户端/缓存/
# 注册表单例（不拆独立服务的理由见 admin.py 头注释）。
admin.init(FLEET_CACHE, iot_client, CFG)
app.register_blueprint(admin.admin_bp)

# ---------------------------------------------------------------- AI 预热（步骤⑦，v1.4）
# 后台守护线程提前加载 FunASR（约 30 秒）：放在这里而不是 __main__，
# 这样无论用哪种方式拉起 app（python app.py / 外部 wsgi 加载本模块）
# 预热都会发生；加载在子线程进行，完全不阻塞下面的 Flask 起端口。
ai_service.preload_async()


# ---------------------------------------------------------------- CORS
# 网页端以 file:// 打开时向本服务发起跨源 fetch，浏览器要求响应带 CORS
# 头，否则拦截。教学环境放行所有来源（照抄 V2，决策 D4：维持 HTTP 明文）。
# 三件套缺一不可（2026-11-06 测试台实测踩坑）：
#   GET 是"简单请求"不触发预检，V2 网页只看不控（纯 GET）所以从没暴露；
#   POST + Content-Type: application/json 会先发 OPTIONS 预检，浏览器要求
#   预检响应明确 Allow-Headers 放行 Content-Type，否则报 Failed to fetch。
# v1.2 再补一课：管理页所有请求带 X-Admin-Token 自定义头——带自定义
# 头的请求（哪怕是 GET）也触发预检，Allow-Headers 不放行 X-Admin-Token
# 的话管理页一个请求都发不出去。
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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
    if not isinstance(speed, int) or isinstance(speed, bool) or not 0 <= speed <= 100:
        return None, "speed 必须是 0~100 的整数"
    return {"speed": speed}, None


def shadow_to_state(shadow_resp) -> dict:
    """把设备影子响应压平成状态字典（沿用 V2 逻辑，Decimal 统一转 float）"""
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


def device_online(device_id: str) -> bool:
    """设备是否在线（设备详情的 status 字段：ONLINE/OFFLINE/INACTIVE）"""
    try:
        resp = iot_client.show_device(ShowDeviceRequest(device_id=device_id))
        return resp.status == "ONLINE"
    except hc_exceptions.SdkException as e:
        log.error("查设备状态失败 %s: %s", device_id, e)
        return False


def check_registered(device_id):
    """device_id 参数校验：缺失/未注册返回错误文案，合法返回 None。"""
    if not device_id:
        return "缺少 device_id 参数"
    if registry.get(device_id) is None:
        return f"device_id 未注册: {device_id}"
    return None


# ---------------------------------------------------------------- 接口
@app.get("/devices")
def devices():
    """车列表（APP 选车页用，协议 4.1）。数据源：注册表 + 缓存。

    battery 从缓存取：从未上报过为 null（协议明确离线车取影子最后一次
    上报值，缓存里正好存的就是影子最新值）。
    """
    snapshot = FLEET_CACHE.get_all()
    result = []
    for dev in registry.devices():
        state = snapshot.get(dev["device_id"]) or {}
        result.append({
            "device_id": dev["device_id"],
            "name": dev["name"],
            "online": state.get("online", False),
            "battery": state.get("battery"),  # 从未上报过为 None → null
            # v1.2 超集字段：frozen（协议 13.3），老客户端不读不受影响
            "frozen": bool(dev.get("frozen")),
        })
    return jsonify({"ok": True, "devices": result})


@app.get("/status")
def status():
    """单车状态（APP 控制页轮询，协议 4.2）。device_id 必填。

    数据源走实时影子查询（V2 同款路径）：控制页的读频与缓存刷新节奏
    无关，实时查保证 APP 看到的是云侧此刻的最新值。
    """
    device_id = request.args.get("device_id")
    err = check_registered(device_id)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    try:
        resp = iot_client.show_device_shadow(ShowDeviceShadowRequest(device_id=device_id))
    except hc_exceptions.SdkException as e:
        return jsonify({"ok": False, "error": f"查影子失败: {e}"}), 502
    return jsonify({"ok": True, "device_id": device_id,
                    "online": device_online(device_id), **shadow_to_state(resp)})


def dispatch_command(device_id: str, cmd: str, paras: dict):
    """下发单条命令到云并等回执——/command 与 /ai/command 共用的内部逻辑
    （步骤⑦ 抽函数：AI 指令必须走与手控完全相同的下发链路，冻结拦截/
    审计日志/回执后立即刷缓存这些既有能力才"自然生效"，而不是复制一份）。

    返回 (result, http_err)：
      result    = {"command_id", "executed", "response_name"}（502 时为 None）
      http_err  = None 或 (错误文案, HTTP 状态码)
                  400 设备冻结 / 502 云 API 失败 / 504 超时无回执
    """
    # v1.2：冻结设备直接拒绝（协议 13.4）——冻结的意义就是"云侧禁连 +
    # 后端拒令"双保险；拒绝也记审计日志，管理后台命令历史可见
    if registry.is_frozen(device_id):
        log.info("命令拒绝 -> %s %s 原因=设备已冻结，命令已拒绝", device_id, cmd)
        return None, ("设备已冻结，命令已拒绝", 400)

    req = CreateCommandRequest(
        device_id=device_id,
        # 云端校验要求 paras 恒非空：无参命令也传 {}（传 None 报 IOTDA.000006，V2 踩坑）
        body=DeviceCommandRequest(service_id="car", command_name=cmd, paras=paras or {}),
    )
    log.info("命令下发 -> device_id=%s cmd=%s speed=%s",
             device_id, cmd, paras.get("speed") if paras else None)
    try:
        resp = iot_client.create_command(req)
    except hc_exceptions.SdkException as e:
        log.error("命令失败 -> device_id=%s cmd=%s err=%s", device_id, cmd, e)
        return None, (str(e), 502)

    # 同步命令：resp.response 有值（response_name=命令名）说明车已执行并回执
    # 注意：SDK 反序列化后 response 可能是 dict 也可能是模型对象，统一压成 dict 处理（V2 踩坑）
    resp_info = getattr(resp, "response", None)
    if isinstance(resp_info, dict):
        info = resp_info
    elif resp_info is not None:
        info = {k: getattr(resp_info, k, None)
                for k in ("response_name", "result", "paras", "error_code")}
    else:
        info = None
    response_name = info.get("response_name") if info else None
    result = {
        "command_id": getattr(resp, "command_id", None),
        "executed": bool(response_name),
        "response_name": response_name,
    }
    log.info("命令回执 -> device_id=%s cmd=%s speed=%s result=%s",
             device_id, cmd, paras.get("speed") if paras else None, result)

    if not response_name:
        # 命令已发出但约 20 秒内没等到车的执行回执（协议 4.5：504）
        return result, ("命令下发超时未收到回执", 504)

    # 步骤④ 优化：车执行完命令会立即补报一帧属性，1 秒后主动刷一次
    # 该车缓存，让这帧马上进入 WS 广播流（网页 1~2 秒即可见位移，
    # 否则要等下一轮 5 秒周期）。Timer 异步调度，不阻塞本请求返回。
    threading.Timer(1.0, FLEET_CACHE.refresh_device,
                    args=(device_id,)).start()
    return result, None


@app.post("/command")
def command():
    """下发命令（协议 4.3）。device_id 必填，路由到对应设备。"""
    body = request.get_json(silent=True) or {}
    device_id = body.get("device_id")
    cmd = (body.get("cmd") or "").upper()

    err = check_registered(device_id)
    if err:
        log.info("命令拒绝 -> %s %s 原因=%s", device_id, cmd, err)
        return jsonify({"ok": False, "error": err}), 400
    paras, err = build_paras(cmd, body)
    if err:
        log.info("命令拒绝 -> %s %s 原因=%s", device_id, cmd, err)
        return jsonify({"ok": False, "error": err}), 400

    result, http_err = dispatch_command(device_id, cmd, paras)
    if http_err:
        payload = {"ok": False, "error": http_err[0]}
        # 504 时带上 command_id/device_id（协议 4.5 原样保留，便于排查）
        if result:
            payload["device_id"] = device_id
            payload["command_id"] = result["command_id"]
        return jsonify(payload), http_err[1]
    return jsonify({"ok": True, "device_id": device_id, **result})


@app.get("/fleet/status")
def fleet_status():
    """车队状态批量（协议 7.2 兜底通道）。数据源：缓存快照。

    每台车带 device_id/name/color（静态信息来自注册表）+ 动态状态
    （online/x/y/heading/speed/battery/time，来自缓存）。
    """
    snapshot = FLEET_CACHE.get_all()
    cars = []
    for dev in registry.devices():
        state = snapshot.get(dev["device_id"]) or {}
        cars.append({
            "device_id": dev["device_id"],
            "name": dev["name"],
            "color": dev["color"],
            **state,
        })
    return jsonify({"ok": True, "cars": cars})


def _parse_time(s):
    """解析 /track 的 from/to 时间参数。返回 (Unix秒, 错误文案)，合法时错误文案为 None。

    接受两种格式（协议 7.3）：
      1. 本地时间 "YYYY-MM-DD HH:MM[:SS]" → datetime.fromisoformat
         解析后 .timestamp() 转本地时区下的 Unix 秒（3.10 的
         fromisoformat 支持 HH:MM 省略秒）；
      2. Unix 秒数字符串 → float。
    先试 fromisoformat 再试 float（3.10 兼容写法，两段各自 try）。
    """
    if s is None or s == "":
        return None, None
    try:
        return datetime.fromisoformat(s).timestamp(), None
    except ValueError:
        pass
    try:
        return float(s), None
    except ValueError:
        return None, f"时间格式非法: {s}（应为 YYYY-MM-DD HH:MM[:SS] 或 Unix 秒）"


@app.get("/track")
def track():
    """轨迹查询（协议 7.3）。数据源：SQLite 轨迹表（缓存刷新处落库）。

    device_id 必填且须已注册（否则 400，与 /status 口径一致）；
    from/to 可选，缺省窗口 = 最近 24 小时。

    响应点字段是协议 7.3 示例 {x, y, t} 的【有意超集】：额外带
    heading/speed——回放动画要用朝向摆正车的图形、用速度调播放
    节奏；多给字段不影响按 7.3 示例消费的客户端（多取少不取均可）。
    t 在服务端就转成本地时间可读格式（存储是 UTC Unix 秒，
    展示转本地，见 track_store.py 头注释），前端零时区换算。
    """
    device_id = request.args.get("device_id")
    err = check_registered(device_id)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    from_ts, err = _parse_time(request.args.get("from"))
    if err:
        return jsonify({"ok": False, "error": f"from 参数错误: {err}"}), 400
    to_ts, err = _parse_time(request.args.get("to"))
    if err:
        return jsonify({"ok": False, "error": f"to 参数错误: {err}"}), 400

    # 缺省窗口：最近 24 小时（路线图验收口径"双车跑过即可查到"）
    now = time.time()
    if from_ts is None:
        from_ts = now - 24 * 3600
    if to_ts is None:
        to_ts = now
    if from_ts > to_ts:
        return jsonify({"ok": False, "error": "from 不能晚于 to"}), 400

    rows = track_store.query(device_id, from_ts, to_ts)
    points = [{
        "x": r["x"],
        "y": r["y"],
        "t": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        # heading/speed：协议 7.3 的有意超集（回放动画要用，见函数注释）
        "heading": r["heading"],
        "speed": r["speed"],
    } for r in rows]
    return jsonify({"ok": True, "device_id": device_id,
                    "count": len(points), "points": points})


@app.get("/health")
def health():
    """探活 + 车队摘要（协议 4.4）。total/online 从缓存快照统计。"""
    snapshot = FLEET_CACHE.get_all()
    online = sum(1 for s in snapshot.values() if s.get("online"))
    return jsonify({"ok": True, "total": len(registry.devices()), "online": online})


# ================================================================ AI 接口（步骤⑦，v1.4，协议 16 节）
def _car_nodes() -> list:
    """注册表 → LLM 车清单投影 [{node_id, name, device_id}]。

    node_id 从 device_id 剥前缀得到（device_id = {product_id}_{node_id}，
    product_id 是 24 位 hex 不含下划线，split 一次即安全）。只投影这三个
    字段——密钥等敏感字段不该进 LLM 的上下文。
    """
    return [{"node_id": d["device_id"].split("_", 1)[1], "name": d["name"],
             "device_id": d["device_id"]} for d in registry.devices()]


def _node_to_device_id(car_node: str):
    """node_id → device_id（在注册表里反查，查不到 None）。

    为什么不直接用 config 的 product_id 拼：注册表是唯一真相来源，
    反查天然兼容"管理端将来换 product_id"的情况。
    """
    for c in _car_nodes():
        if c["node_id"] == car_node:
            return c["device_id"]
    return None


@app.post("/ai/asr")
def ai_asr():
    """语音转文字（协议 16.2）。请求体 = 原始音频字节，响应 {"text": ...}。

    Content-Type 两种吃法：
      audio/wav       → 字节本身就是完整 WAV，直接喂 ASR；
      audio/pcm 等    → 裸 PCM 流，按 query 参数 rate/channels/bits 用
                        wave 模块补一个 44 字节头包成 WAV（网页端录的
                        就是 16k/单声道/16bit，参数缺省即此配置）。
    ASR 内部异常（模型未就绪/云侧失败/音频损坏）一律 502：这不是调用方
    能改参数解决的，是服务端依赖的问题，语义上属"上游故障"。
    """
    audio = request.get_data()
    if not audio:
        return jsonify({"ok": False, "error": "请求体为空，应为音频字节"}), 400

    ctype = (request.content_type or "").lower()
    if ctype.startswith("audio/wav"):
        wav_bytes = audio
    else:
        # 裸 PCM → 包 WAV 头。浏览器录音链路固定 16k/1ch/16bit，参数化只为通用
        try:
            rate = int(request.args.get("rate", 16000))
            channels = int(request.args.get("channels", 1))
            bits = int(request.args.get("bits", 16))
        except ValueError:
            return jsonify({"ok": False, "error": "rate/channels/bits 必须是整数"}), 400
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(bits // 8)
            w.setframerate(rate)
            w.writeframes(audio)
        wav_bytes = buf.getvalue()

    try:
        text = ai_service.transcribe(wav_bytes)
    except Exception as e:  # noqa: BLE001 统一转 502（含 FunASR 未就绪/云侧失败）
        log.error("ASR 失败 provider=%s err=%s", ai_service.provider(), e)
        return jsonify({"ok": False, "error": f"语音识别失败: {e}"}), 502
    return jsonify({"ok": True, "text": text, "provider": ai_service.provider()})


@app.post("/ai/command")
def ai_command():
    """自然语言控车（协议 16.3）：文字 → LLM 解析 → 白名单校验 → 复用下发链路。

    body: {"text": 必填, "device_id": 可选}
    device_id 的语义 = "页面上已绑定哪台车就控哪台"：带它时 LLM 解析出的
    car_node 被忽略（用户在 UI 选中的车优先级高于话里说的车号，避免
    "看着 A 车卡片说'前进'结果 B 车动了"的惊吓场景）。
    每条动作独立执行、独立记结果（部分成功也如实返回），响应里的
    results 与 actions 按下标一一对应。
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "缺少 text（要解析的指令文本）"}), 400

    device_id = body.get("device_id")
    if device_id:
        err = check_registered(device_id)
        if err:
            return jsonify({"ok": False, "error": err}), 400

    cars = _car_nodes()
    node_ids = {c["node_id"] for c in cars}

    # ---- LLM 解析：网络问题 502 / 两轮重试后仍非 JSON 502 ----
    try:
        parsed = ai_service.parse_command(text, cars)
    except requests.RequestException as e:
        log.error("LLM 调用失败: %s", e)
        return jsonify({"ok": False, "error": f"LLM 调用失败: {e}"}), 502
    except RuntimeError as e:
        log.error("LLM 解析失败: %s", e)
        return jsonify({"ok": False, "error": "AI 解析失败"}), 502

    # ---- 白名单校验：LLM 输出不可信，非法项丢弃（协议 16.4）----
    try:
        car_node, actions = ai_service.validate_actions(parsed, node_ids)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    # ---- 目标车：指定车优先，其次话里的车号；两者皆无则拒绝 ----
    if device_id:
        final_device = device_id
    else:
        final_device = _node_to_device_id(car_node) if car_node else None
        if not final_device:
            return jsonify({"ok": False,
                            "error": "指令中没有指明要控制哪台车，请先在页面选中车，或说出车号（如：1号车前进）"}), 400

    car = registry.get(final_device)
    reply = str(parsed.get("reply") or "").strip()

    # ---- 逐条执行：走与手控 /command 完全相同的 dispatch_command ----
    results = []
    for act in actions:
        # STOP/LEFT/RIGHT 无参（paras={}，dispatch 内已处理云端恒非空约定）
        paras = {"speed": act["speed"]} if act["speed"] is not None else {}
        result, http_err = dispatch_command(final_device, act["cmd"], paras)
        if http_err:
            # 冻结 400 / 云失败 502 / 无回执 504 → 记一条失败结果，继续下一条
            log.info("AI 命令失败 -> %s %s 原因=%s", final_device, act["cmd"], http_err[0])
            results.append({"cmd": act["cmd"], "speed": act["speed"],
                            "ok": False, "executed": False, "detail": http_err[0]})
        else:
            results.append({"cmd": act["cmd"], "speed": act["speed"],
                            "ok": True, "executed": result["executed"],
                            "detail": result["response_name"] or act["cmd"]})

    return jsonify({
        "ok": True,
        "text": text,
        "reply": reply,
        "car": {"device_id": final_device, "name": car["name"] if car else final_device},
        "actions": actions,
        "results": results,
    })


if __name__ == "__main__":
    # 本机局域网 IP：UDP connect 不发任何包，仅让协议栈选路由，
    # 离线环境安全；失败（无网络）兜底 127.0.0.1
    import socket
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("10.255.255.255", 1))
        _lan_ip = _s.getsockname()[0]
        _s.close()
    except OSError:
        _lan_ip = "127.0.0.1"
    print("=" * 60)
    print(" 业务后端已启动（APP/网页 -> 本服务 -> 华为云 IoTDA）")
    print(f"   GET  http://127.0.0.1:{CFG['http_port']}/devices")
    print(f"   GET  http://127.0.0.1:{CFG['http_port']}/status?device_id=...")
    print(f"   POST http://127.0.0.1:{CFG['http_port']}/command")
    print(f"   GET  http://127.0.0.1:{CFG['http_port']}/fleet/status")
    print(f"   GET  http://127.0.0.1:{CFG['http_port']}/track?device_id=...")
    print(f"   GET  http://127.0.0.1:{CFG['http_port']}/health")
    print(f"   POST http://127.0.0.1:{CFG['http_port']}/ai/asr          （语音转文字，步骤⑦）")
    print(f"   POST http://127.0.0.1:{CFG['http_port']}/ai/command      （自然语言控车，步骤⑦）")
    print(f"   */   http://127.0.0.1:{CFG['http_port']}/admin/*   （管理后台 v1.2，六接口）")
    print(f"   WS   ws://{_lan_ip}:8889/ws   （协议 7.1 车队广播，步骤④）")
    print(f" AI: ASR provider={ai_service.provider()}（config.json ai 段切换，改后需重启）, "
          f"LLM={CFG.get('ai', {}).get('llm_model', '?')}")
    print(f" 注册设备 {len(registry.devices())} 台:")
    for d in registry.devices():
        frozen = "（已冻结）" if d.get("frozen") else ""
        print(f"   {d['device_id']}  {d['name']} {frozen}")
    print("=" * 60, flush=True)
    app.run(host="0.0.0.0", port=CFG["http_port"], debug=False)
