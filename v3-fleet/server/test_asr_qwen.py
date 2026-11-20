# -*- coding: utf-8 -*-
"""云端 ASR（qwen-audio-3.1-asr-flash-message）冒烟测试，DashScope 原生协议。

跑法：
    D:/anaconda3/python.exe test_asr_qwen.py [音频文件]

- 踩坑记录：该模型不走 OpenAI 兼容 HTTP（/compatible-mode/v1 传音频一律
  400 "url error"），官方姿势是 dashscope SDK 的 Recognition 类走
  WebSocket 推理端点（/api-ws/v1/inference）——本脚本即官方参考代码的
  参数化版本，端点与 key 从 config.json 的 ai 段读取。
- 与 FunASR 本地方案互为备胎：config 里 ai.asr_provider 一行切换。
"""
from http import HTTPStatus
import json
import sys
import time

import dashscope
from dashscope.audio.asr import Recognition

WAV = sys.argv[1] if len(sys.argv) > 1 else "_tts_test.wav"

AI = json.load(open("config.json", encoding="utf-8"))["ai"]
dashscope.api_key = AI["asr_api_key"]
# 中转站把官方 wss://dashscope.aliyuncs.com/api-ws/v1/inference 换成了自己的域名
dashscope.base_websocket_api_url = (
    AI["asr_base_url"].replace("https://", "wss://")
    .replace("/compatible-mode/v1", "/api-ws/v1/inference")
)
print(f"推理端点: {dashscope.base_websocket_api_url}", flush=True)

recognition = Recognition(
    model=AI["asr_model"],
    format="wav",
    sample_rate=16000,
    callback=None,
)

t0 = time.time()
result = recognition.call(WAV)
dt = time.time() - t0

if result.status_code == HTTPStatus.OK:
    sentences = result.get_sentence()
    text = sentences[-1]["text"] if sentences else "(无结果)"
    print(f"识别结果: {text}", flush=True)
    print(f"耗时 {dt:.2f} 秒 | first_pkg={recognition.get_first_package_delay()}ms "
          f"last_pkg={recognition.get_last_package_delay()}ms", flush=True)
else:
    print(f"Error: {result.message}", flush=True)
