# -*- coding: utf-8 -*-
"""
v3-fleet - 车队状态缓存模块（为步骤④ WebSocket 广播打底）
==========================================================
职责：把"每台车的最新状态"常驻内存（device_id → 状态 dict），
由后台守护线程定时从华为云拉取刷新。步骤②的 /devices、
/fleet/status、/health 直接读它；步骤④的 WebSocket /ws 每秒
广播帧也从它取数——一次拉取，处处消费，接口数量增长不带动
云端调用增长。

设计说明（为什么是 5 秒轮询）：
  1. 5 秒间隔对齐设备上报节奏（协议文档决策 D3）：设备端运动
     状态每 5 秒上报一次属性，刷新快于 5 秒也拿不到更新的数据，
     纯属浪费调用；慢于 5 秒则白白放大展示延迟。
  2. 轮询消耗的是应用侧 API 调用额度，不是设备消息额度——
     设备消息额度（IoTDA S0 每日约 1 万条）只统计设备 MQTT
     上行/下行，应用侧 ShowDeviceShadow/ShowDevice 不占它，
     所以 5 秒 × N 台车的轮询是安全的（N 台车约 N×2 次/5 秒）。
  3. 守护线程 + 异常吞掉打日志：云端抖动不能让缓存线程崩溃，
     更不能拖垮 HTTP 服务；某次失败就保留上一次的值（数据
     稍旧好过没有）。
  4. 所有读写经同一把锁：缓存是 HTTP 线程和后台线程共享的
     可变状态，不加锁会出现"读到写了一半的状态"。

SDK 用法沿用 V2 已验证的三要素（见 iot-stage/server/app.py）：
实例专属端点 + BasicCredentials.with_derived_predicate + 请求不带
instance_id。客户端由 app.py 构建后传入，本模块不碰凭证。

步骤③追加：on_record 回调（构造时可选注入）。
  刷新线程每更新完一台车的缓存就调一次 on_record(device_id, state,
  changed)——这是步骤③轨迹落库的挂点。为什么挂这里而不挂在设备
  上报处：后端是拉模式，唯一能观测到"有新数据"的时刻就是缓存刷新
  这一拍；缓存是后端唯一的数据汇聚点，步骤④ WS 广播也从这走。
  注意两条纪律：
    1. 回调在锁外调用——回调内部会拿 track_store 自己的锁，若在
       缓存锁内调用就形成锁嵌套，两把锁的获取顺序一旦颠倒就是死锁；
    2. 回调异常一律吞掉打日志——落库失败可以容忍（丢一个点），
       缓存线程绝不能因此死掉。
  本模块不 import track_store（回调由 app.py 注入），保持缓存与
  存储解耦：缓存可脱离 SQLite 单独测试/复用。

已知坑沿用 V2 经验：
  - 影子数值可能反序列化成 Decimal → 统一转 float（jsonify 不能
    序列化 Decimal）
"""
import logging
import threading
from decimal import Decimal

from huaweicloudsdkcore.exceptions import exceptions as hc_exceptions
from huaweicloudsdkiotda.v5.model import (
    ShowDeviceRequest,
    ShowDeviceShadowRequest,
)

log = logging.getLogger("fleet_cache")

# 刷新间隔（秒）：对齐设备 5 秒上报节奏（决策 D3）
REFRESH_INTERVAL = 5


def _shadow_to_state(shadow_resp) -> dict:
    """把影子响应压平成标准状态 dict（照搬 V2 app.py 同名函数逻辑）。

    任一字段缺失/从未上报过就保持 None——"没数据"和"数据是 0"
    是两回事，上层（如 /devices 的 battery）靠 None 区分。
    """
    state = {"x": None, "y": None, "heading": None,
             "speed": None, "battery": None, "time": None}
    try:
        for s in shadow_resp.shadow:
            if s.reported and s.reported.properties:
                for k in state:
                    if k in s.reported.properties:
                        v = s.reported.properties[k]
                        # 影子数值可能返回 Decimal，统一转 float 才能 jsonify
                        state[k] = float(v) if isinstance(v, Decimal) else v
                state["time"] = s.reported.event_time
    except (AttributeError, TypeError):
        pass
    return state


class FleetCache:
    """车队状态缓存：内存 dict + 后台守护线程定时刷新 + 线程安全读取。"""

    def __init__(self, iot_client, devices: list, on_record=None):
        """
        :param iot_client: 已构建好的 IoTDAClient（凭证在 app.py 统一处理）
        :param devices:    registry.load_registry() 返回的设备列表
        :param on_record:  可选回调 (device_id, state, changed)，每台车
                           缓存更新后锁外调用（步骤③轨迹落库挂点，
                           见类头注释；None 表示不关心落库）
        """
        self._client = iot_client
        # 只存 id 列表即可：name/color 属于注册表的静态信息，
        # 由 app.py 组装响应时按 device_id 回查，缓存只管"动态状态"。
        # v1.2 步骤⑤：列表不再是构造时定死的——管理后台注册新设备后
        # 经 set_device_ids() 推送最新清单（见下）。
        self._device_ids = [d["device_id"] for d in devices]
        self._on_record = on_record
        self._lock = threading.Lock()
        # device_id -> {"online": bool, "x":.., "y":.., "heading":..,
        #               "speed":.., "battery":.., "time":..}
        self._cache: dict = {}
        self._thread = None
        self._stop = threading.Event()

    # ------------------------------------------------ 对外接口
    def start(self):
        """启动后台刷新线程（守护线程：主进程退出即随之消亡）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="fleet-cache", daemon=True)
        self._thread.start()
        log.info("车队状态缓存线程已启动，刷新间隔 %s 秒，监测 %d 台设备",
                 REFRESH_INTERVAL, len(self._device_ids))

    def stop(self):
        """停线程（主要给测试/优雅退出用，守护线程本身不阻塞进程退出）。"""
        self._stop.set()

    def get_all(self) -> dict:
        """取全部车最新状态。返回副本，调用方改不动缓存本体。"""
        with self._lock:
            return {device_id: dict(state)
                    for device_id, state in self._cache.items()}

    def get_one(self, device_id: str):
        """取单台车最新状态；尚未刷到过（刚启动/从未成功）返回 None。"""
        with self._lock:
            state = self._cache.get(device_id)
            return dict(state) if state else None

    def set_device_ids(self, ids: list):
        """v1.2 步骤⑤：更新监测设备清单（管理后台注册新设备后调用）。

        为什么是"显式推送"而不是"每轮循环重读 registry"：
          后一种省一次调用但要 import registry，缓存模块就与注册表
          耦合（目前本模块连华为云凭证都不碰，客户端由外部注入，
          就是为了能脱离工程单独测试/复用）；显式推送由 admin.py 在
          注册成功的同一事务里调一次即可，新设备最迟 5 秒后进入
          缓存与 WS 广播，代价为零。列表赋值在持锁内完成，与刷新
          线程互不踩踏。
        """
        with self._lock:
            self._device_ids = list(ids)

    # ------------------------------------------------ 后台线程
    def _loop(self):
        # 先立即刷一次再进入节奏：避免服务刚启动的几秒内缓存全空
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as e:  # noqa: BLE001
                # 兜底：任何未知异常都不允许杀死线程，打日志继续下一轮
                log.error("车队缓存刷新出现未预期异常: %s", e)
            self._stop.wait(REFRESH_INTERVAL)

    def refresh_once(self):
        """轮询一遍全部注册设备（在线状态 + 影子），刷新缓存。

        单台失败只影响那一台（保留旧值），不中断其余设备的刷新。
        先把 id 列表拷贝出来再遍历：管理线程可能在遍历中途调
        set_device_ids 换掉列表本体，拷贝保证本轮遍历的一致性
        （新增设备本轮不刷、下一轮 5 秒后自然补上）。
        """
        with self._lock:
            ids = list(self._device_ids)
        for device_id in ids:
            self.refresh_device(device_id)

    def refresh_device(self, device_id: str):
        """刷新单台设备的缓存（在线状态 + 影子），并触发落库回调。

        步骤④ 新增的单台入口：POST /command 成功后调度一次"立即刷新"——
        设备端执行完命令会立即补报一帧属性，主动刷一次能让这一帧马上
        进入缓存与 WS 广播流，网页不用干等下一轮 5 秒周期（实测把
        "控车→网页见位移"从约 5~6 秒压到 1~2 秒）。额外代价是每条命令
        多两次应用侧查询（ShowDevice+ShowDeviceShadow），相对额度可忽略。
        线程安全性：供 Timer 线程与缓存线程并发调用，全路径持锁。
        """
        state = {"online": False, "x": None, "y": None, "heading": None,
                 "speed": None, "battery": None, "time": None}

        # 1) 在线状态（ONLINE/OFFLINE/INACTIVE）
        try:
            resp = self._client.show_device(ShowDeviceRequest(device_id=device_id))
            state["online"] = getattr(resp, "status", None) == "ONLINE"
        except hc_exceptions.SdkException as e:
            # 查不到在线状态：本台保持 offline + 旧影子值，下一轮再试
            log.error("缓存刷新查在线状态失败 %s: %s", device_id, e)

        # 2) 影子（位置/速度/电量等最新上报值）
        try:
            resp = self._client.show_device_shadow(
                ShowDeviceShadowRequest(device_id=device_id))
            state.update(_shadow_to_state(resp))
        except hc_exceptions.SdkException as e:
            log.error("缓存刷新查影子失败 %s: %s", device_id, e)

        with self._lock:
            old = self._cache.get(device_id)
            self._cache[device_id] = state

        # changed = 与旧缓存相比 x/y/speed/battery 任一有变。
        # 首次见到该车（旧缓存不存在）视为 changed——新车的第一帧
        # 状态必须让下游（落库）拿到。
        if old is None:
            changed = True
        else:
            changed = any(state.get(k) != old.get(k)
                          for k in ("x", "y", "speed", "battery"))

        # 回调在锁外调用（回调里会拿 store 的锁，避免锁嵌套/死锁，
        # 见类头注释）；异常吞掉打日志，绝不能影响缓存线程
        if self._on_record:
            try:
                self._on_record(device_id, state, changed)
            except Exception as e:  # noqa: BLE001
                log.error("on_record 回调异常 %s（忽略，不影响缓存）: %s",
                          device_id, e)
