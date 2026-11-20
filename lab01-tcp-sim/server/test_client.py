#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab01 - TCP 测试客户端
=======================
两种用法：
  1) 自测：python test_client.py <IP> --auto
     自动发送一组指令并打印服务端全部回包，用于快速验收服务端。
  2) 交互：python test_client.py <IP> [端口]
     手动输入指令（如 CMD:FORWARD:60 / PING），回车发送，quit 退出。
"""
import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 自测脚本：(要发送的指令, 发送后等待秒数)
AUTO_SCRIPT = [
    ("PING", 0.2),
    ("CMD:STATUS", 0.2),
    ("CMD:FORWARD:70", 0.6),   # 前进 0.6 秒，观察 STATE 中 x/y 变化
    ("CMD:LEFT", 0.2),         # 原地左转 15°
    ("CMD:FORWARD:70", 0.6),   # 沿新朝向前进
    ("CMD:RIGHT", 0.2),        # 原地右转 15°
    ("CMD:BACK:40", 0.6),      # 后退
    ("CMD:STOP", 0.3),         # 停止
    ("CMD:FOO", 0.2),          # 未知指令 -> 应回 ERR:UNKNOWN_CMD
    ("BADLINE", 0.2),          # 非法帧   -> 应回 ERR:BAD_FRAME
    ("CMD:STATUS", 0.3),       # 最终状态
]


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    ip = sys.argv[1]
    auto = "--auto" in sys.argv
    port = 8888
    for arg in sys.argv[2:]:
        if arg.isdigit():
            port = int(arg)

    reader, writer = await asyncio.open_connection(ip, port)
    print(f"[连接] {ip}:{port} 已建立，等待服务端数据...")

    received = {"n": 0}

    async def recv_loop():
        while True:
            line = await reader.readline()
            if not line:
                print("[断开] 服务端关闭了连接")
                break
            received["n"] += 1
            print(f"[<-车端] {line.decode('utf-8').strip()}")

    task = asyncio.create_task(recv_loop())

    if auto:
        for payload, wait in AUTO_SCRIPT:
            writer.write((payload + "\n").encode("utf-8"))
            await writer.drain()
            print(f"[->车端] {payload}")
            await asyncio.sleep(wait)
        await asyncio.sleep(0.5)
        print("\n" + "=" * 46)
        print(f"自测完成：共收到 {received['n']} 帧数据")
        print("检查要点：每条 CMD 都有 ACK 回执；STATE 持续广播（约 100ms/帧）；")
        print("CMD:FOO / BADLINE 分别回 ERR:UNKNOWN_CMD / ERR:BAD_FRAME")
        print("=" * 46)
        writer.close()
    else:
        print("输入指令（如 CMD:FORWARD:60 / PING），回车发送，quit 退出")
        loop = asyncio.get_running_loop()
        while True:
            text = await loop.run_in_executor(None, input, "> ")
            if text.strip() in ("quit", "exit"):
                break
            if text.strip():
                writer.write(text.strip().encode("utf-8") + b"\n")
                await writer.drain()
        writer.close()

    task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
