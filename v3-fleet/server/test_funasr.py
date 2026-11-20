# -*- coding: utf-8 -*-
"""FunASR 本地语音识别冒烟测试（步骤⑦ 前置验证）。

跑法：
    D:/anaconda3/python.exe test_funasr.py [音频文件]

- 首次运行会从 ModelScope（国内源）自动下载 paraformer-zh 模型（约 1GB），
  缓存在用户目录 ~/.cache/modelscope，之后离线可用——这正是"本地推理"
  对课堂演示的价值：装好之后断网也能跑。
- paraformer-zh 是阿里达摩院开源的中文Paraformer大模型（16kHz 采样率），
  FunASR 对它做了别名映射，不用记完整模型 ID。
"""
import sys
import time

from funasr import AutoModel

WAV = sys.argv[1] if len(sys.argv) > 1 else "_tts_test.wav"

print(f"== 加载 paraformer-zh 模型（首次运行会先下载）==", flush=True)
t0 = time.time()
model = AutoModel(
    model="paraformer-zh",        # 中文 Paraformer 大模型
    vad_model="fsmn-vad",         # 语音活动检测：自动截掉静音段
    punc_model="ct-punc",         # 标点恢复：让转写文本更自然
    disable_update=True,          # 关闭版本检查提示
)
print(f"   模型就绪，耗时 {time.time() - t0:.1f} 秒（含首次下载）", flush=True)

print(f"== 识别 {WAV} ==", flush=True)
t0 = time.time()
res = model.generate(input=WAV, batch_size_s=300)
dt = time.time() - t0

text = res[0]["text"] if res else "(无结果)"
print(f"   识别结果: {text}", flush=True)
print(f"   推理耗时 {dt:.2f} 秒（CPU，后续调用不再有模型加载开销）", flush=True)
