# -*- coding: utf-8 -*-
"""云端录音文件转写（paraformer-8k-v2）测试，DashScope Transcription 异步接口。

跑法：
    D:/anaconda3/python.exe test_asr_paraformer8k.py

- 与 Recognition（实时识别、吃本地文件）不同，Transcription 是"提交任务→
  轮询结果"的异步转写，只吃公网 file_urls，适合整段长录音归档。
- base_http_api_url 指向中转站（官方是 https://dashscope.aliyuncs.com/api/v1）。
"""
from http import HTTPStatus
import json

import dashscope
from dashscope.audio.asr import Transcription

AI = json.load(open("config.json", encoding="utf-8"))["ai"]
dashscope.api_key = AI["asr_api_key"]
dashscope.base_http_api_url = "https://cn-beijing.maas.qianwenaiapi.com/api/v1"
print(f"推理端点: {dashscope.base_http_api_url}", flush=True)

task_response = Transcription.async_call(
    model="paraformer-8k-v2",
    file_urls=[
        "https://dashscope.oss-cn-beijing.aliyuncs.com/samples/audio/paraformer/hello_world_female2.wav",
        "https://dashscope.oss-cn-beijing.aliyuncs.com/samples/audio/paraformer/hello_world_male2.wav",
    ],
)

print(f"任务已提交: {task_response.status_code} task_id={task_response.output.task_id}", flush=True)
transcribe_response = Transcription.wait(task=task_response.output.task_id)

if transcribe_response.status_code == HTTPStatus.OK:
    print(json.dumps(transcribe_response.output, indent=4, ensure_ascii=False))
    print("transcription done!")
else:
    print("Error:", transcribe_response.message)
