# Lab01 - 模拟车服务端（阶段 b：TCP + WebSocket + 俯视图）

模拟一台智能寻检车，同时服务两类客户端：

```
鸿蒙 APP（模拟器，TCP 客户端）
        │  TCP :8888 —— 指令帧 / ACK / STATE
        ▼
car_server.py（小车端）
        │  WebSocket :8889 —— 状态 JSON 推送（每 100ms）
        ▼
浏览器 car_view.html —— Canvas 俯视图，实时渲染小车移动与轨迹
```

> 浏览器不允许裸 TCP 连接，所以可视化页面走 WebSocket 中转；
> 浏览器是「只看不控」的观察端，控制权始终在 APP 端。

## 文件说明

| 文件 | 作用 |
|---|---|
| `server/car_server.py` | 模拟车服务端：TCP 8888（对 APP）+ WebSocket 8889（对浏览器） |
| `server/test_client.py` | TCP 测试客户端（自测/交互两种模式） |
| `web/car_view.html` | 浏览器俯视图页面（双击即可打开，无任何外部依赖） |

## 使用步骤

### 1. 安装依赖（仅一次）

```bash
pip install websockets
```

### 2. 启动服务端

```bash
cd server
python car_server.py          # TCP 默认 8888，WebSocket 固定 8889
```

启动横幅会打印本机局域网 IP 和两个监听端口。首次运行允许防火墙访问。

### 3. 打开俯视图

直接双击 `web/car_view.html`（或拖进浏览器）。连上后能看到：
- 场地网格中的小车（朝向随 heading 旋转，前进蓝色 / 后退橙色 / 停止灰色）
- 最近 240 帧的行驶轨迹（越新越亮）
- 底部 HUD：坐标、朝向、速度、电量实时刷新
- 右上角连接徽标：绿 = WebSocket 已连接；断线每 2 秒自动重连

### 4. 用 APP 或测试客户端控车

- 鸿蒙 APP 连 `tcp://10.0.2.2:8888`（模拟器连本机）
- 或 `python test_client.py <服务端IP> --auto`
- 按方向键 → 浏览器里小车实时移动、转向，轨迹跟着画出来

## 协议速查

**TCP（文本帧，\n 结尾，UTF-8）—— APP ↔ 小车端**

| 方向 | 帧 | 说明 |
|---|---|---|
| APP→车 | `PING` | 心跳，回 `PONG` |
| APP→车 | `CMD:FORWARD[:80]` / `CMD:BACK[:40]` | 前进/后退，速度 0~100，默认 60 |
| APP→车 | `CMD:LEFT` / `CMD:RIGHT` | 原地转向，每次 ±15° |
| APP→车 | `CMD:STOP` / `CMD:STATUS` | 停止 / 立即回状态 |
| 车→APP | `ACK:FORWARD:80` 等 | 指令回执（已收到并执行） |
| 车→APP | `STATE:x=400,y=300,heading=0,speed=60,battery=87` | 每 100ms 广播 |
| 车→APP | `ERR:UNKNOWN_CMD:FOO` / `ERR:BAD_FRAME:xxx` | 非法指令 |

**WebSocket（JSON 文本）—— 小车端 → 浏览器，每 100ms 一帧**

```json
{"x": 400.0, "y": 300.0, "heading": 0, "speed": 80, "battery": 86.9}
```

坐标系约定（TCP/WS/页面三处一致）：场地 800×600，(0,0) 在左上角，heading 0°=朝上、顺时针增大。

## 排错速查

| 现象 | 大概率原因 |
|---|---|
| APP 连不上 | Windows 防火墙拦了 8888 入站；热点开了 AP 隔离（两设备互 ping 验证） |
| 脚本启动报 `websockets` 找不到 | `pip install websockets` |
| 8888 端口被占用（10048） | 上一份 car_server 还在跑，关掉旧窗口再启动 |
| 俯视图一直"等待小车状态" | 服务端没启动，或 8889 被占用/被拦；页面每 2 秒自动重连 |
| IP 打印出来是 127.0.0.1 | 无网络环境，换有网卡的机器或手动改 |
| 连上但收不到 STATE | TCP 客户端没按 \n 分帧读取（用 readline） |
