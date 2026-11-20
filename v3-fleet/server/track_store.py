# -*- coding: utf-8 -*-
"""
v3-fleet - 轨迹存储模块（SQLite，步骤③：轨迹落库与回放）
==========================================================
职责：把缓存刷新时"看见"的每台车最新状态按变化落库到 SQLite
（track.db），并提供按时间窗查询（给 /track 回放接口）与数据治理
（防库无限膨胀）。全部读写经本模块，其他代码不直接碰 SQL。

【为什么挂在缓存刷新处，而不是设备上报处】
  1. 后端根本"看不见"设备上报——设备走 MQTT(8883) 直达华为云，
     后端是拉模式（ShowDeviceShadow 轮询影子），唯一能观测到
     "有新数据"的时刻就是缓存刷新那一拍。
  2. 缓存是后端唯一的数据汇聚点：步骤④ WebSocket 广播也从这取数。
     在同一处挂钩子，实时广播和历史落库共享同一份数据口径，
     将来换应用侧推送（AMQP/订阅，见协议 D3）只改"喂缓存的动作"，
     轨迹落库零改动。
  3. 具体接线方式：FleetCache 暴露 on_record 回调，由 app.py 把
     track_store.record 注入进去——本模块与缓存互不 import，保持
     存储与缓存解耦（缓存可以脱离 SQLite 单独测试/复用）。

【为什么"变化才记"】
  设备上报节奏是运动 5s / 空闲 60s（保护 S0 每日约 1 万条消息额度）。
  空闲时每 60 秒上报的数据和上一条完全相同，如果照单全收：
  一台停车过夜的车一夜就产生 ~570 行垃圾点，回放时还会画出"原地
  抖动"的假轨迹。所以 x/y/speed/battery 全没变就不插行——轨迹
  语义是"发生了什么变化"，不是"心跳打卡"。

【为什么存 Unix 时间戳、展示用本地时间】
  - 存储：ts 存 UTC 秒（float）。与语言/时区/夏令时无关，范围比较
    （BETWEEN from AND to）就是一个数值比较，索引也友好。
  - 展示：/track 返回的 t 字段在服务端转成本地时间可读格式
    "YYYY-MM-DD HH:MM:SS"——人看的部分就地转好，前端零时区换算。

【并发模型：单写者原则】
  路线图风险表明确"SQLite 单文件并发写"是风险：全模块共用一把
  threading.Lock 串行化所有读写；sqlite3 连接 check_same_thread=False
  全局复用一份（缓存线程写、HTTP 线程读，同一连接不存在多连接
  竞争）；开启 WAL 模式让读写不互相阻塞。不上多进程写（单后端
  进程架构下也没这个需求）。

数据治理（cleanup，防 SQLite 无限膨胀）：
  - 删除 7 天前的记录（巡检轨迹的回查价值随时间快速衰减）
  - 单车超过 20000 条时只保留最新 20000 条（约等于一辆 5s 节奏
    连跑 27 小时的量，正常巡检日碰不到顶）
运维 CLI（可直接运行本文件）：
  python track_store.py stats   # 各车行数统计
  python track_store.py clean   # 手动触发一轮清理
"""
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime

log = logging.getLogger("track_store")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "track.db")

# 数据治理参数：7 天保留期 + 单车 2 万条封顶（设计见头部注释）
RETENTION_DAYS = 7
PER_DEVICE_CAP = 20000

# 单写者锁：本模块所有读写都经它串行（单写者原则，见头部注释）
_lock = threading.Lock()
_conn = None

# 内存里的"该车上一条入库值"签名 (x, y, speed, battery)，用于"变化才记"
# 去重。放内存而非查库取末行：省一次查询；代价是重启后首条必入库——
# 重启后首条本来就该记（作为新一段轨迹的起点），语义恰好正确。
_last_sig: dict = {}


# ---------------------------------------------------------------- 初始化
def init(db_path: str = DB_PATH):
    """建库建表建索引（幂等，可重复调用）。app.py 启动时调一次。"""
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(db_path, check_same_thread=False)
            # WAL：读写不互斥，HTTP 查询不会被缓存线程的写卡住
            _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS track_points (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                x         REAL,
                y         REAL,
                heading   REAL,
                speed     REAL,
                battery   REAL,
                ts        REAL NOT NULL
            )
            """
        )
        # 查询永远按 (device_id, 时间窗) 走，复合索引正中查询模式
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_track ON track_points(device_id, ts)"
        )
        _conn.commit()


# ---------------------------------------------------------------- 写入
def record(device_id: str, state: dict) -> bool:
    """记录一个轨迹点。state 为缓存里的状态 dict（含 x/y/heading/speed/battery）。

    两种情况不记（返回 False）：
      1. x 为 None——该车从未上报过位置，没有可回放的坐标；
      2. 与该车上一条入库记录相比 x/y/speed/battery 全没变——
         空闲 60s 的重复上报不产生垃圾行（"变化才记"，见头部注释）。
    返回是否真正插入了行。
    """
    x = state.get("x")
    if x is None:
        return False
    # 去重签名只取参与"变化"判断的四个字段；heading 随 speed/x/y 联动，
    # 单独不作为变化依据（原地把头不影响轨迹形状）
    sig = (x, state.get("y"), state.get("speed"), state.get("battery"))
    with _lock:
        if _last_sig.get(device_id) == sig:
            return False
        _conn.execute(
            "INSERT INTO track_points"
            "(device_id, x, y, heading, speed, battery, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (device_id, x, state.get("y"), state.get("heading"),
             state.get("speed"), state.get("battery"), time.time()),
        )
        _conn.commit()
        _last_sig[device_id] = sig
        return True


# ---------------------------------------------------------------- 查询
def query(device_id: str, from_ts: float = None, to_ts: float = None) -> list:
    """按时间窗（闭区间，Unix 秒）升序返回轨迹点。

    返回 dict 列表：x/y/heading/speed/battery/ts。
    from_ts/to_ts 传 None 表示该侧不设限。
    """
    sql = ("SELECT x, y, heading, speed, battery, ts "
           "FROM track_points WHERE device_id = ?")
    params = [device_id]
    if from_ts is not None:
        sql += " AND ts >= ?"
        params.append(from_ts)
    if to_ts is not None:
        sql += " AND ts <= ?"
        params.append(to_ts)
    sql += " ORDER BY ts ASC"
    with _lock:
        cur = _conn.execute(sql, params)
        keys = ("x", "y", "heading", "speed", "battery", "ts")
        return [dict(zip(keys, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------- 数据治理
def cleanup(now: float = None) -> dict:
    """一轮清理：删 7 天前记录 + 单车封顶 2 万条（只留最新）。

    返回 {"expired": 按天删掉的行数, "capped": {device_id: 删掉的行数}}。
    app.py 启动时调一次；之后可由外部定时器周期调用（运维 CLI 也用它）。
    """
    now = time.time() if now is None else now
    cutoff = now - RETENTION_DAYS * 86400
    with _lock:
        cur = _conn.execute("DELETE FROM track_points WHERE ts < ?", (cutoff,))
        expired = cur.rowcount
        capped = {}
        for (device_id,) in _conn.execute(
                "SELECT DISTINCT device_id FROM track_points").fetchall():
            # 保留 ts 最大的 2 万条：子查询按 ts 倒序取 id 集合，删除不在其中的行
            cur = _conn.execute(
                "DELETE FROM track_points WHERE device_id = ? AND id NOT IN "
                "(SELECT id FROM track_points WHERE device_id = ? "
                " ORDER BY ts DESC LIMIT ?)",
                (device_id, device_id, PER_DEVICE_CAP),
            )
            if cur.rowcount > 0:
                capped[device_id] = cur.rowcount
        _conn.commit()
        return {"expired": expired, "capped": capped}


def count(device_id: str = None) -> int:
    """统计行数（运维/日志用）。device_id 为 None 统计全库。"""
    with _lock:
        if device_id is None:
            row = _conn.execute("SELECT COUNT(*) FROM track_points").fetchone()
        else:
            row = _conn.execute(
                "SELECT COUNT(*) FROM track_points WHERE device_id = ?",
                (device_id,)).fetchone()
        return row[0]


def device_ids() -> list:
    """库中出现过的全部 device_id（CLI stats 用）。"""
    with _lock:
        return [r[0] for r in
                _conn.execute("SELECT DISTINCT device_id FROM track_points")]


# ---------------------------------------------------------------- 消息量统计（v1.2 步骤⑤）
def stats_by_day(days: int = 7, now: float = None) -> dict:
    """按本地日分组统计 track_points 行数（最近 N 天，含今天）。

    【口径说明（额度监控打底）】这里统计的是"轨迹点行数"，即缓存
    刷新处看见的状态变化帧数——它是设备上报帧数（≈IoTDA 消息额度
    消耗）的近似值而非精确值：空闲 60s 的重复上报因"变化才记"不落
    库（会低估），云端影子合并也可能吞帧（同样低估）。低估方向对
    额度监控是安全侧：这里不超 1 万，真实消耗一定也不超。
    """
    now = time.time() if now is None else now
    # 本地日的起点：今天 0 点往前推 (days-1) 天；分组键用 SQLite 的
    # date(ts,'unixepoch','localtime')——与 /track 的"存储 UTC、展示
    # 本地"口径一致，人看的日子必须是本地日。
    today0 = datetime.fromtimestamp(now).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    start = today0 - (days - 1) * 86400
    with _lock:
        rows = _conn.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) "
            "FROM track_points WHERE ts >= ? GROUP BY day",
            (start,)).fetchall()
    by_day = {r[0]: r[1] for r in rows}
    # 补全没有数据的天为 0：前端画 7 天条形图需要连续的横轴
    result = []
    for i in range(days):
        day = datetime.fromtimestamp(start + i * 86400).strftime("%Y-%m-%d")
        result.append({"day": day, "count": by_day.get(day, 0)})
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    return {"today": by_day.get(today, 0), "by_day": result}


# ---------------------------------------------------------------- 运维 CLI
def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "stats"
    if cmd not in ("stats", "clean"):
        print("用法: python track_store.py [stats|clean]\n"
              "  stats  各车轨迹行数统计（默认）\n"
              "  clean  手动触发一轮数据治理（7 天过期 + 单车 2 万条封顶）")
        return
    init()
    if cmd == "stats":
        print(f"轨迹库: {DB_PATH}")
        print(f"总行数: {count()}")
        ids = device_ids()
        if not ids:
            print("（库中还没有任何车的轨迹）")
        for device_id in ids:
            print(f"  {device_id}  {count(device_id)} 行")
    else:  # clean
        result = cleanup()
        print(f"清理完成：过期删除 {result['expired']} 行；"
              f"单车封顶删除 {result['capped'] or '无'}")
        print(f"当前总行数: {count()}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
