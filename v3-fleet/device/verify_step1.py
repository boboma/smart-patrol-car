#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3-fleet - 步骤① 云端验收脚本（多车模拟底座）
==============================================
验收标准（路线图①）：双实例同时连 MQTT，控制台/影子查询能看到
两台设备各自独立的位置数据。

本脚本从云端视角完成验收（不需要开控制台）：
  对 devices.json 里每台车：
    1. ShowDevice        —— 设备在线状态（ONLINE/OFFLINE/INACTIVE）
    2. ShowDeviceShadow  —— 最新设备影子，打印 x/y/heading/speed/battery
  最后汇总：两台设备的影子是否各自独立有数据 → 给出验收结论

华为云应用侧 SDK 正确用法（沿用 V2 app.py 已验证的写法，见
iot-stage/server/app.py 第 59~71 行注释）：
  1. 标准版实例必须用实例专属端点（iotda-app 域名，读 config.json 的 endpoint）
  2. BasicCredentials + with_derived_predicate(DerivedCredentials...)：
     专属端点自动启用 V11 衍生签名
  3. 用实例专属端点时，请求一律不带 instance_id（域名已隐含实例）
  4. AK/SK 凭证从 ../../iot-stage/server/config.json 读取（凭证隔离原则：
     只存本机、不入仓不入 README），本脚本不硬编码任何密钥

运行：D:/anaconda3/python.exe verify_step1.py
     建议先跑 start_fleet.py 让双车在线至少 1 分钟（有上报才有影子数据）
依赖：huaweicloudsdkcore、huaweicloudsdkiotda（V2 阶段已装好并打过补丁）
"""
import json
import os
from decimal import Decimal

from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
from huaweicloudsdkcore.exceptions import exceptions as hc_exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkiotda.v5 import IoTDAClient
from huaweicloudsdkiotda.v5.model import (
    ShowDeviceRequest,
    ShowDeviceShadowRequest,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 配置加载
# 设备注册表：V3 自己的 devices.json（协议文档第 3 节）
with open(os.path.join(BASE_DIR, "..", "server", "devices.json"), encoding="utf-8") as f:
    REGISTRY = json.load(f)

# 应用侧凭证：跨工程借用 V2 的 config.json（AK/SK 只存在这一处，不复制不硬编码）
V2_CONFIG = os.path.join(BASE_DIR, "..", "..", "iot-stage", "server", "config.json")
with open(V2_CONFIG, encoding="utf-8") as f:
    CFG = json.load(f)

# ---------------------------------------------------------------- 云客户端（全局一份）
# 与 V2 app.py 完全同款的构建姿势（标准版实例三要素）：
#   实例专属端点 + 衍生签名谓词 + 不带 instance_id
credentials = BasicCredentials(CFG["ak"], CFG["sk"], CFG["project_id"])
credentials.with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
iot_client = (
    IoTDAClient.new_builder()
    .with_credentials(credentials)
    .with_region(Region(CFG["region"], CFG["endpoint"]))
    .build()
)


def shadow_to_state(shadow_resp) -> dict:
    """把设备影子响应压平成易读的状态字典（照搬 V2 app.py 同名函数逻辑）"""
    state = {"x": None, "y": None, "heading": None,
             "speed": None, "battery": None, "time": None}
    try:
        for s in shadow_resp.shadow:
            if s.reported and s.reported.properties:
                for k in state:
                    if k in s.reported.properties:
                        v = s.reported.properties[k]
                        # SDK 数值可能反序列化成 Decimal，统一转 float 好打印
                        state[k] = float(v) if isinstance(v, Decimal) else v
                state["time"] = s.reported.event_time
    except (AttributeError, TypeError):
        pass
    return state


def check_device(client: IoTDAClient, dev: dict) -> dict:
    """单台车验收：查在线状态 + 查影子，返回汇总结果"""
    device_id = dev["device_id"]
    result = {"device_id": device_id, "name": dev.get("name", device_id),
              "online": None, "state": None, "error": None}
    try:
        # 在线状态（请求不带 instance_id：实例专属端点的域名已隐含实例）
        dev_resp = client.show_device(ShowDeviceRequest(device_id=device_id))
        result["online"] = getattr(dev_resp, "status", None)

        # 最新设备影子
        shadow_resp = client.show_device_shadow(
            ShowDeviceShadowRequest(device_id=device_id))
        result["state"] = shadow_to_state(shadow_resp)
    except hc_exceptions.SdkException as e:
        result["error"] = str(e)
    return result


def main():
    print("=" * 60)
    print(" V3 步骤① 云端验收：双车影子独立上报检查")
    print(f" 应用侧端点: {CFG['endpoint']}")
    print("=" * 60)

    results = []
    for dev in REGISTRY["devices"]:
        print(f"\n--- {dev['name']}（{dev['device_id']}，渲染色 {dev['color']}）---")
        r = check_device(iot_client, dev)
        results.append(r)

        if r["error"]:
            print(f"  [失败] 云端调用出错: {r['error']}")
            print("         （常见原因：设备从未上线过（影子不存在）/ 网络不通 / 凭证失效）")
            continue
        print(f"  在线状态 : {r['online']}")
        s = r["state"]
        print(f"  影子数据 : x={s['x']}  y={s['y']}  heading={s['heading']}  "
              f"speed={s['speed']}  battery={s['battery']}")
        print(f"  上报时间 : {s['time']}（UTC）")

    # ---------------------------------------------------------------- 汇总结论
    print("\n" + "=" * 60)
    ok_count = 0
    for r in results:
        s = r["state"] or {}
        # "独立有数据"判定：影子里 x/y 都有值（说明该车自己上报过，不是空壳）
        has_data = s.get("x") is not None and s.get("y") is not None
        if has_data:
            ok_count += 1
            print(f"[通过] {r['name']} 影子有独立位置数据 "
                  f"(x={s.get('x')}, y={s.get('y')}, 上报于 {s.get('time')})")
        else:
            print(f"[未过] {r['name']} 影子缺少位置数据"
                  f"{'（调用出错）' if r['error'] else ''}，"
                  f"请确认该车模拟器已在跑并完成至少一次上报")
    total = len(results)
    print("-" * 60)
    if total and ok_count == total:
        print(f" 验收结论：通过（{ok_count}/{total} 台设备影子各自独立有数据）")
        print(" 双车模拟底座就绪，可进入步骤②（业务后端多车化）")
    else:
        print(f" 验收结论：未通过（{ok_count}/{total}）")
        print(" 排查建议：1) 跑 start_fleet.py 并等 1 分钟以上；"
              "2) 确认 V2 同名模拟器没在跑（互踢）")
    print("=" * 60)


if __name__ == "__main__":
    if hasattr(__import__("sys").stdout, "reconfigure"):
        __import__("sys").stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
