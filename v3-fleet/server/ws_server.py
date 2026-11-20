# -*- coding: utf-8 -*-
"""
v3-fleet - WebSocket 车队状态广播服务（步骤④，协议 7.1 主通道）
================================================================
职责：独立端口监听 ws://0.0.0.0:8889/ws，每 1 秒向所有已连接的
网页客户端广播一帧 fleet_state 车队状态；同时响应客户端心跳
ping → pong。数据源 = FleetCache.get_all()（传入的缓存对象），
本模块自己不碰华为云 SDK——一次拉取、处处消费，广播数量增长
不带动云端调用增长（与 fleet_cache.py 的设计说明一脉相承）。

为什么用独立端口 8889 而不是塞进 Flask 的 8000？
  1. Flask 本体（WSGI 同步模型）不支持 WebSocket：WS 需要
     服务端在一条已建立的 HTTP 连接上持续"推"，而 Flask 的
     每个 view 函数都是"请求进→响应出"就结束，没有长连接
     的位置。要塞进 8000 得引入 flask-sock/gevent 这类扩展
     换线程模型，复杂度陡增。
  2. websockets 库是纯 asyncio 实现：它要一个 asyncio 事件
     循环才能跑，与 Flask 的 WSGI 模型是"不同路"的两套世界。
     把事件循环丢进一个 daemon 线程独立运行，是两者共存最
     简单干净的方式（app.py 里 start(cache) 一行接入）。
  3. 技术路线 V1 已验证：lab01-tcp-sim/web/car_view.html 用的
     就是 8889 端口的 ws 广播，前端写法可以直接复用。
  协议文档 7.1 只规定了路径名 /ws 与帧格式，未规定端口；选
  8889 是沿用 V1 的端口习惯（教学项目端口稳定有助记忆）。

帧格式（严格按协议 7.1，color 为超集字段）：
  {"type":"fleet_state",
   "ts":"2026-11-07 20:30:00",
   "cars":[{"device_id":..., "name":..., "online":true,
            "x":1.2,"y":3.4,"heading":90,"speed":80,
            "battery":95,"time":"..."}, ...]}
  color 是协议 7.1 最小集之外的超集字段：devices.json 里登记
  的渲染色（协议第 3 节"color 供网页端多车分轨渲染用"），网页
  分轨渲染要靠它给每车上色，随帧一起下发可让前端零额外请求。
  按最小集消费的客户端不受影响（多取少不取均可，同 /track 超集
  的设计口径）。

广播纪律：
  - 没有客户端时不构造/不广播（省资源；缓存反正一直在刷，
    WS 只是它的"显示窗口"，窗口没人看就不用点亮）；
  - 某个客户端发送失败（掉线/半关闭）就剔除它，绝不影响
    其他客户端的广播节奏；
  - 心跳：客户端发 {"type":"ping"} → 回 {"type":"pong"}
    （协议 7.1）；非 ping 消息不响应（网页端"只看不控"，
    本通道没有上行业务）。

线程模型：start(cache) 起 daemon 线程，线程内 asyncio.run()
跑事件循环；任何异常打日志不崩（与 fleet_cache 的守护线程
纪律一致：广播挂了不能拖垮 HTTP 主服务）。

依赖：websockets 15.x（asyncio 风格 API：websockets.serve +
单参数 handler(websocket)，V1 同款技术路线）。
运行：随 app.py 启动，不单独运行（自测见 test_ws.py）。
"""
import asyncio
import json
import logging
import threading
from datetime import datetime

import websockets

log = logging.getLogger("ws_server")

WS_HOST = "0.0.0.0"
WS_PORT = 8889
WS_PATH = "/ws"          # 协议 7.1 规定的路径名
BROADCAST_INTERVAL = 1   # 协议 7.1：每 1 秒广播一帧


class _FleetBroadcaster:
    """持有一份客户端集合 + 缓存引用，提供 handler 与广播协程。

    客户端集合的并发说明：asyncio 单线程事件循环内，handler 的
    收发与广播协程的 send 虽是交替执行，但每个 await 点之间不会
    被打断到"集合增删的中途"（无 await 的集合操作是原子的），
    因此不需要额外加锁——这正是 asyncio "单线程协作式"模型的
    省心之处。
    """

    def __init__(self, cache, static_map: dict):
        self._cache = cache
        # 注册表静态信息映射 device_id -> {"name","color"}，由 app.py
        # 组装传入（app.py 单向依赖 ws_server，反向 import 会成环）。
        # v1.2 步骤⑤：管理后台注册新设备后经 set_static_map() 换新
        # 映射，新设备下一帧广播即出现（赋值替换见该函数注释）。
        self._static_map = static_map
        self._clients: set = set()

    # -------------------------------------------- v1.2：静态映射动态更新
    def set_static_map(self, static_map: dict):
        """更新注册表静态映射（管理后台注册设备后由 admin.py 调用）。

        并发说明：本方法在 Flask HTTP 线程调用，_build_frame 在
        asyncio 事件循环线程调用。这里只做一次"替换引用"（self.
        _static_map = 新 dict），CPython 下引用赋值是原子的，读方
        要么看到旧映射要么看到新映射，不存在"读一半"的状态，
        因此不需要加锁。传入前拷贝一份由调用方保证（admin.py
        每次构造全新 dict）。
        """
        self._static_map = static_map

    # -------------------------------------------- 连接处理（协议 7.1）
    async def handler(self, ws):
        """每个客户端连接一个 task：登记 → 收消息（心跳）→ 注销。

        websockets 15.x：handler 只收 websocket 一个参数
        （旧版 (ws, path) 双参数签名已在 14.0 移除，路径判断改用
        ws.request.path）。
        """
        # 路径校验：协议只约定 /ws，其他路径一律拒绝（防止误连）
        req_path = getattr(getattr(ws, "request", None), "path", WS_PATH)
        if req_path != WS_PATH:
            await ws.close(code=1008, reason="invalid path")
            return

        self._clients.add(ws)
        log.info("WS 客户端接入 %s:%s（当前在线 %d）",
                 ws.remote_address[0], ws.remote_address[1], len(self._clients))
        try:
            async for message in ws:
                # 网页端"只看不控"，上行只有心跳一种合法消息
                try:
                    msg = json.loads(message)
                except (ValueError, TypeError):
                    continue
                if msg.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
        except websockets.ConnectionClosed:
            pass  # 正常断线，走 finally 注销即可
        finally:
            self._clients.discard(ws)
            log.info("WS 客户端断开 %s:%s（当前在线 %d）",
                     ws.remote_address[0], ws.remote_address[1], len(self._clients))

    # -------------------------------------------- 广播（协议 7.1）
    def _build_frame(self) -> dict:
        """从缓存快照组装一帧 fleet_state。

        name/color 是注册表静态信息，缓存里只有动态状态——但 WS
        广播绕过 app.py 的响应组装，所以这里需要注册表。为保持
        "本模块不 import registry/app"的解耦（app.py 单向依赖
        ws_server，反向依赖会成环），由 start() 把组装好的静态
        信息映射传进来，本函数只做合并。
        """
        snapshot = self._cache.get_all()
        cars = []
        for device_id, static in self._static_map.items():
            state = snapshot.get(device_id) or {}
            cars.append({
                "device_id": device_id,
                "name": static["name"],
                "color": static["color"],  # 超集字段：网页分轨渲染色
                "online": state.get("online", False),
                "x": state.get("x"),
                "y": state.get("y"),
                "heading": state.get("heading"),
                "speed": state.get("speed"),
                "battery": state.get("battery"),
                "time": state.get("time"),
            })
        # ts 用本地可读格式（协议 7.1 示例即本地时间语义，与
        # /track 的 t 同一口径：给人看的展示时间，可读优先）
        return {"type": "fleet_state",
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cars": cars}

    async def broadcast_loop(self):
        """每 1 秒向所有客户端广播一帧；无人收听就跳过（省资源）。"""
        while True:
            await asyncio.sleep(BROADCAST_INTERVAL)
            if not self._clients:
                continue
            frame = json.dumps(self._build_frame(), ensure_ascii=False)
            # 快照一次集合再逐个发送：某客户端失败只剔它自己，不影响
            # 别人（send 对已关闭连接抛 ConnectionClosed，gather 收集）
            snapshot_clients = list(self._clients)
            results = await asyncio.gather(
                *(self._safe_send(ws, frame) for ws in snapshot_clients),
                return_exceptions=True)
            for ws, r in zip(snapshot_clients, results):
                if isinstance(r, Exception):
                    self._clients.discard(ws)
                    log.info("WS 发送失败剔除客户端（当前在线 %d）",
                             len(self._clients))

    async def _safe_send(self, ws, frame: str):
        """单客户端发送：连接已关闭/半关闭会抛 ConnectionClosed，
        由 gather(return_exceptions=True) 收集后统一剔除，本函数
        不自行捕获（避免吞掉真正需要日志的未知异常）。"""
        await ws.send(frame)


# ---------------------------------------------------------------- 启动入口
_broadcaster: _FleetBroadcaster = None


def _run_loop(cache, static_map):
    """线程体：在新线程里跑 asyncio 事件循环（Flask 线程模型之外）。"""
    global _broadcaster
    _broadcaster = _FleetBroadcaster(cache, static_map)

    async def main():
        # websockets.serve：15.x asyncio 风格，handler 单参数
        async with websockets.serve(_broadcaster.handler, WS_HOST, WS_PORT):
            log.info("WS 广播服务就绪 ws://%s:%s%s（每 %d 秒广播 fleet_state）",
                     WS_HOST, WS_PORT, WS_PATH, BROADCAST_INTERVAL)
            await _broadcaster.broadcast_loop()

    asyncio.run(main())


def start(cache, static_map: dict = None):
    """启动 WS 广播服务（daemon 线程）。

    :param cache:      FleetCache 实例（广播数据源 get_all()）
    :param static_map: device_id -> {"name":..., "color":...}，由
                       app.py 从注册表组装传入（见 _build_frame 注释）
    """
    global _broadcaster
    if _broadcaster is not None:
        return  # 幂等：重复调用不重复起服务

    if static_map is None:
        # 允许不传静态映射（退化：帧内 name/color 为空串），正常
        # 用法是 app.py 传注册表映射进来
        static_map = {}

    t = threading.Thread(target=_run_loop, args=(cache, static_map),
                         name="ws-server", daemon=True)
    t.start()

    def _watchdog():
        # 线程意外退出打日志（不重启——WS 挂了网页会走 7.2 兜底通道，
        # 提醒人来修比静默重启更符合教学项目的可见性原则）
        t.join()
        log.error("WS 广播线程已退出（网页端将退化为 HTTP 轮询兜底）")

    threading.Thread(target=_watchdog, name="ws-watchdog",
                     daemon=True).start()


def set_static_map(static_map: dict):
    """模块级入口：更新广播用的注册表静态映射（v1.2 步骤⑤）。

    admin.py 注册新设备后调用，把含新车的映射推给广播器；服务未
    启动时静默忽略（测试场景/降级运行不报错）。
    """
    if _broadcaster is not None:
        _broadcaster.set_static_map(static_map)
