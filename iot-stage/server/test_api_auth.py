"""
iot-stage - 应用侧 API 认证测试（官方指南标准版用法）
=====================================================
依据官方文档 https://support.huaweicloud.com/sdkreference-iothub/iot_10_10003.html
标准版/企业版实例正确用法：
  1. BasicCredentials + with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
  2. 自建 Region(region_id, 应用侧专属endpoint) + .with_region()
测试矩阵：新管理员 iot-aa 的 AK/SK × 两个候选 endpoint
"""
import sys
import traceback

from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkiotda.v5 import IoTDAClient, ListDevicesRequest

AK = "HPUA****（AK已脱敏，请填入自己的）"
SK = "****（SK已脱敏，请填入自己的）"
PROJECT_ID = "01a18ad51bfd7b3696ca4c0c1a3c298d"
REGION_ID = "cn-north-4"
INSTANCE_ID = "71b55ab7e9"
DEVICE_ID = "6ae440347f2e6c302fa846ee_car_001"

# 候选 endpoint：①实例专属应用侧地址（官方推荐，标准版）②通用共享版地址
ENDPOINTS = [
    ("实例专属应用侧", f"https://{INSTANCE_ID}.st1.iotda-app.{REGION_ID}.myhuaweicloud.com"),
    ("通用应用侧", f"https://iotda.{REGION_ID}.myhuaweicloud.com"),
]


def make_client(endpoint: str) -> IoTDAClient:
    """官方指南：标准版实例用法"""
    credentials = BasicCredentials(AK, SK, PROJECT_ID)
    credentials.with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
    region = Region(REGION_ID, endpoint)
    return (
        IoTDAClient.new_builder()
        .with_credentials(credentials)
        .with_region(region)
        .build()
    )


def try_list_devices(label: str, endpoint: str, use_instance: bool):
    print(f"\n{'=' * 60}")
    print(f"[{label}] endpoint = {endpoint}")
    print(f"  ListDevices with instance_id={INSTANCE_ID if use_instance else '(不带)'}")
    client = make_client(endpoint)
    try:
        req = ListDevicesRequest()
        if use_instance:
            req.instance_id = INSTANCE_ID
        req.limit = 5
        resp = client.list_devices(req)
        devices = list(resp.devices or [])
        print(f"  ✅ 成功！HTTP 200，设备数：{len(devices)}")
        for d in devices:
            print(f"     - {d.device_id}  status={d.status}")
        return True
    except exceptions.ClientRequestException as e:
        print(f"  ❌ 失败 status={e.status_code} error_code={e.error_code}")
        print(f"     error_msg={e.error_msg}")
        print(f"     request_id={e.request_id}")
        return False
    except Exception as e:
        print(f"  ❌ 异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("华为云 IoTDA 应用侧认证测试（新管理员 iot-aa）")
    print(f"AK={AK[:6]}...  project_id={PROJECT_ID}")

    results = {}
    for label, ep in ENDPOINTS:
        for use_inst in (True, False):
            key = f"{label}+{'带instance' if use_inst else '不带instance'}"
            results[key] = try_list_devices(label, ep, use_inst)

    print("\n" + "=" * 60)
    print("汇总：")
    for k, v in results.items():
        print(f"  {'✅' if v else '❌'}  {k}")
    sys.exit(0 if any(results.values()) else 1)
