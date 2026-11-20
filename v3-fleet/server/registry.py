# -*- coding: utf-8 -*-
"""
v3-fleet - 设备注册表（协议文档第 3 节，L5 链路；v1.2 起可动态写）
==================================================================
职责：加载 devices.json 为内存注册表并做格式校验（fail-fast，
脏数据宁可拒绝启动也不能带病运行）；v1.2 步骤⑤升级为可写——
管理后台的"注册设备 / 冻结解冻"经本模块落盘，并提供 reload。

为什么独立成模块：
  1. app.py 只管 HTTP，注册表规则集中一处，管理后台（admin.py）
     "注册/冻结设备"复用同一套校验与写盘逻辑；
  2. 校验规则单独可测（命令行直接跑本文件可自检）。

校验规则（轻量正则即可，V3 静态注册表不做重量级 schema 校验）：
  - 每条记录必须有 device_id / name / color 三个非空字符串字段
    （color 供网页端多车分轨渲染，协议文档 3 节）
  - device_id 形如 {24位hex产品ID}_{节点ID}，如
    6ae440347f2e6c302fa846ee_car_001（协议文档第 2 节）
  - device_id 不得重复（重复会让 /command 路由歧义）
  - frozen 字段可选（v1.1 及更早的注册表没有它）：缺省视为
    False，保证旧文件向后兼容

v1.2 并发模型（为什么加锁 + 原子写）：
  注册表从"启动读一次的常量"变成"HTTP 线程会写的可变状态"：
  /command 读 is_frozen、admin.py 写 add/set_frozen，二者并发。
  1. 全模块一把 threading.Lock：内存读写与"读文件→改→写文件"
     整个事务都串行化，避免两个请求同时写互相覆盖；
  2. 原子写（先写临时文件再 os.replace）：写到一半崩溃/断电
     不会留下半个 JSON 让下次启动 fail-fast 退出。os.replace
     在同一文件系统上是原子的（Windows 也支持覆盖目标）。

运行（自检）：D:/anaconda3/python.exe registry.py
"""
import json
import os
import re
import sys
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(BASE_DIR, "devices.json")

# 24 位十六进制产品ID + 下划线 + 节点ID（节点ID 允许字母/数字/下划线/连字符）
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{24}_[A-Za-z0-9_-]+$")

REQUIRED_FIELDS = ("device_id", "name", "color")

# ---------------------------------------------------------------- 内存注册表
# v1.2：模块级单例。load 时填充，admin 写操作同步维护，app.py 接口
# 一律经 devices()/get() 读，不再持有启动时的旧列表引用。
_lock = threading.Lock()
_devices: list = []
# 注册表文件路径：init/reload 时确定，写操作跟随同一路径——保证
# "从哪个文件加载就写回哪个文件"（默认真实 devices.json；测试可
# init(临时文件) 后写操作全部落在临时文件，不碰真实注册表）。
_registry_path = REGISTRY_PATH


def load_registry(path: str = REGISTRY_PATH) -> list:
    """加载并校验设备注册表，返回设备列表；失败打印原因并退出。"""
    # 文件层面：不存在 / 不是合法 JSON，直接报错退出
    if not os.path.exists(path):
        print(f"[注册表错误] 找不到 {path}，无法确定车队设备清单", file=sys.stderr)
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[注册表错误] {path} 不是合法的 UTF-8 JSON: {e}", file=sys.stderr)
        sys.exit(1)

    devices = data.get("devices")
    if not isinstance(devices, list) or not devices:
        print(f"[注册表错误] {path} 缺少非空的 devices 数组", file=sys.stderr)
        sys.exit(1)

    seen = set()
    for i, dev in enumerate(devices):
        where = f"{path} 第 {i + 1} 条记录"
        # 字段层面：三要素齐全且非空字符串
        for field in REQUIRED_FIELDS:
            v = dev.get(field)
            if not isinstance(v, str) or not v.strip():
                print(f"[注册表错误] {where} 缺少字段 {field} 或其值不是非空字符串",
                      file=sys.stderr)
                sys.exit(1)
        device_id = dev["device_id"]
        # 格式层面：{24位hex产品ID}_{节点ID}
        if not DEVICE_ID_RE.match(device_id):
            print(f"[注册表错误] {where} device_id 格式不合法: {device_id} "
                  f"（应为 {{24位hex产品ID}}_{{节点ID}}，如 6ae440347f2e6c302fa846ee_car_001）",
                  file=sys.stderr)
            sys.exit(1)
        # 唯一性层面：重复 id 会让命令路由与状态缓存互相覆盖
        if device_id in seen:
            print(f"[注册表错误] {where} device_id 重复: {device_id}", file=sys.stderr)
            sys.exit(1)
        seen.add(device_id)
    return devices


# ---------------------------------------------------------------- 内存访问
def init(path: str = REGISTRY_PATH):
    """启动入口：加载 + 校验并填充内存注册表。app.py 启动时调一次。"""
    global _devices, _registry_path
    with _lock:
        _registry_path = path
        _devices = load_registry(path)


def devices() -> list:
    """当前注册表（每条记录返回副本，调用方改不动内存本体）。"""
    with _lock:
        return [dict(d) for d in _devices]


def get(device_id: str):
    """单查：返回该设备记录的副本；未注册返回 None。"""
    with _lock:
        for d in _devices:
            if d["device_id"] == device_id:
                return dict(d)
        return None


def is_frozen(device_id: str) -> bool:
    """设备是否已冻结（frozen 字段缺省视为 False，兼容旧注册表）。"""
    with _lock:
        for d in _devices:
            if d["device_id"] == device_id:
                return bool(d.get("frozen"))
        return False


def device_ids() -> list:
    """全部 device_id（缓存刷新列表用）。"""
    with _lock:
        return [d["device_id"] for d in _devices]


# ---------------------------------------------------------------- 写操作（管理后台用）
def _save_unlocked(devices: list, path: str = REGISTRY_PATH):
    """原子写 devices.json（须持 _lock 调用）：先写临时文件再 os.replace。

    为什么要原子写：直接 open("w") 写到一半进程崩溃会留下截断的
    JSON，下次启动 load_registry 直接 fail-fast 拒绝启动；临时文件
    + os.replace 的"改名替换"在操作系统层面是原子的，最坏情况是
    临时文件残留，注册表本体永远完整。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"devices": devices}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def add(node_id: str, name: str, color: str, product_id: str) -> dict:
    """注册一台新设备：写 devices.json + 更新内存。返回新记录。

    调用方（admin.py）负责先做云侧注册并落密钥，本函数只管注册表
    本身。重复 device_id 抛 ValueError（管理端转 400）。
    """
    global _devices
    device_id = f"{product_id}_{node_id}"
    with _lock:
        if any(d["device_id"] == device_id for d in _devices):
            raise ValueError(f"device_id 已存在: {device_id}")
        entry = {"device_id": device_id, "name": name,
                 "color": color, "frozen": False}
        # 写盘内容基于内存列表追加（锁内完成"读-改-写"整个事务）；
        # 写回路径跟随加载路径（_registry_path），见其注释
        _save_unlocked(_devices + [entry], _registry_path)
        _devices = _devices + [entry]
        return dict(entry)


def set_frozen(device_id: str, frozen: bool) -> bool:
    """标记/取消冻结并写回 devices.json。返回是否找到该设备。"""
    global _devices
    with _lock:
        for d in _devices:
            if d["device_id"] == device_id:
                if bool(d.get("frozen")) == frozen:
                    return True  # 幂等：状态没变也不必重写文件
                d["frozen"] = frozen
                _save_unlocked(_devices, _registry_path)
                return True
        return False


def reload(path: str = None):
    """重新加载注册表（管理端改完 devices.json 后调用，或运维手动改文件后）。

    path 缺省用上一次 init/reload 的路径（"从哪加载就重读哪"）。
    校验失败会抛 SystemExit（load_registry 的既有行为）：管理接口
    里的写操作都经本模块的校验路径，正常不会触发；手动改坏文件的
    场景宁可重启失败也不要带病路由。
    """
    global _devices
    with _lock:
        if path is None:
            path = _registry_path
        _devices = load_registry(path)


if __name__ == "__main__":
    # 自检入口：直接运行打印注册表内容，便于人工核对
    devs = load_registry()
    print(f"[注册表自检] 通过，共 {len(devs)} 台设备：")
    for d in devs:
        frozen = "已冻结" if d.get("frozen") else "正常"
        print(f"  {d['device_id']}  {d['name']}  渲染色 {d['color']}  {frozen}")
