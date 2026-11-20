"""
多账号连通性测试：用「官方标准版正确用法」验证 4 份凭证
正确用法 = 实例专属应用侧端点 + BasicCredentials + derived_predicate + with_region + 不带 instance_id
"""
from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkiotda.v5 import IoTDAClient, ListDevicesRequest

PROJECT_ID = "01a18ad51bfd7b3696ca4c0c1a3c298d"
REGION_ID = "cn-north-4"
ENDPOINT = "https://71b55ab7e9.st1.iotda-app.cn-north-4.myhuaweicloud.com"

ACCOUNTS = [
    ("iot-aa（现任管理员）", "HPUA****（AK已脱敏，请填入自己的）", "****（SK已脱敏，请填入自己的）"),
    ("iotda-teacher（旧配置在用）", "HPUA****（AK已脱敏，请填入自己的）", "****（SK已脱敏，请填入自己的）"),
    ("iotda-t2", "HPUA****（AK已脱敏，请填入自己的）", "****（SK已脱敏，请填入自己的）"),
    ("主账号 hid_****", "HPUA****（AK已脱敏，请填入自己的）", "****（SK已脱敏，请填入自己的）"),
]


def test(name: str, ak: str, sk: str) -> bool:
    try:
        credentials = BasicCredentials(ak, sk, PROJECT_ID)
        credentials.with_derived_predicate(DerivedCredentials.get_default_derived_predicate())
        client = (
            IoTDAClient.new_builder()
            .with_credentials(credentials)
            .with_region(Region(REGION_ID, ENDPOINT))
            .build()
        )
        req = ListDevicesRequest()
        req.limit = 5
        resp = client.list_devices(req)
        devices = list(resp.devices or [])
        ids = ", ".join(d.device_id.split("_")[-1] + "=" + d.status for d in devices)
        print(f"  ✅ {name}: HTTP 200, 设备[{ids}]")
        return True
    except exceptions.ClientRequestException as e:
        print(f"  ❌ {name}: {e.status_code} {e.error_code}")
        return False
    except Exception as e:
        print(f"  ❌ {name}: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    print("四账号连通性测试（正确 SDK 用法 + 实例专属端点）\n")
    results = {name: test(name, ak, sk) for name, ak, sk in ACCOUNTS}
    print("\n汇总：", " / ".join(f"{k}={'通' if v else '不通'}" for k, v in results.items()))
