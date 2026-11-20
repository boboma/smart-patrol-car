# -*- coding: utf-8 -*-
"""
v3-fleet - AI 服务模块（步骤⑦，协议文档第 16 节）
==================================================
职责：把"自然语言"变成"合法的控车动作序列"，拆成两段：
  1. ASR 语音转文字：FunASR 本地推理 / qwen 云端推理，二选一
     （config.json 的 ai.asr_provider 切换——【重启后端生效】，
     provider 在 import 时读定，运行中改配置不热切换，理由见 CFG 注释）；
  2. LLM 文字转指令：调 DeepSeek chat/completions，把中文话术解析成
     固定 JSON（目标车 + 动作序列 + 给用户的确认话术）。

设计原则（协议 16.1）：**AI 负责听懂，后端负责只允许安全的事发生**。
本模块只做"翻译"，不做任何下发；产出经 validate_actions 白名单过滤后
交给 app.py 复用既有命令下发链路（冻结拦截/审计日志/回执后刷缓存
全部自然生效）。LLM 输出永远不可信，白名单是最后一道闸。

为什么 funasr 懒加载 + 后台预热：
  FunASR 三模型（paraformer-zh + fsmn-vad + ct-punc）加载约 30 秒，
  若放在 app.py import 路径里同步加载，Flask 起端口就要等 30 秒；
  preload_async() 起守护线程提前加载，首个语音请求到达时模型多半已
  就绪。加载完成的模型常驻内存（单例），后续推理只有毫秒级开销。

密钥安全：全部凭证只从 config.json 的 ai 段读取，本文件不出现任何
字面密钥；异常信息透传上层时注意不要把请求头/密钥拼进去。

自检：D:/anaconda3/python.exe -m py_compile ai_service.py
（真实识别/解析属集成验收，不在这里连云测试。）
"""
import json
import logging
import os
import re
import tempfile
import threading
import time

import requests

log = logging.getLogger("ai_service")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE_DIR, "config.json")

# ---------------------------------------------------------------- 配置
# import 时读定一份（与 app.py 的 CFG 同源同文件）。为什么不做热加载：
# asr_provider 决定"加载哪套模型/SDK"，切换意味着内存形态变化，
# 教学场景一行注释说明"改配置需重启"远比维护热切换状态机划算。
with open(CFG_PATH, encoding="utf-8") as f:
    _CFG = json.load(f)
AI = _CFG.get("ai", {}) or {}


def provider() -> str:
    """当前 ASR 提供方（local / qwen），/ai/asr 响应里回显给前端。"""
    return AI.get("asr_provider", "local")


# ================================================================ ASR
# FunASR 单例：None=尚未加载。双检锁（先无锁读、再加锁确认）让推理
# 热路径零锁开销，只有首次加载那一次会排队。
_funasr_model = None
_funasr_lock = threading.Lock()


def _get_funasr():
    """懒加载 FunASR 三模型（约 30 秒，只发生一次，常驻内存）。

    funasr 的 import 也放在函数内：这个包 import 本身就要数秒，
    不拖累 provider=qwen 时的模块加载速度。
    模型缓存在 D:/model_cache（首次下载约 1GB，之后离线可用），
    环境变量 MODELSCOPE_CACHE 已在系统中设好，代码不依赖它。
    """
    global _funasr_model
    if _funasr_model is not None:
        return _funasr_model
    with _funasr_lock:
        if _funasr_model is None:  # 双检：等锁期间可能已被别的线程加载完
            from funasr import AutoModel
            t0 = time.time()
            log.info("FunASR 模型开始加载（首次约 30 秒）...")
            _funasr_model = AutoModel(
                model="paraformer-zh",   # 中文 Paraformer 大模型（16kHz）
                vad_model="fsmn-vad",    # 语音活动检测：自动截掉静音段
                punc_model="ct-punc",    # 标点恢复：转写文本更自然
                disable_update=True,     # 关闭版本检查联网提示
            )
            log.info("FunASR 模型就绪，耗时 %.1f 秒", time.time() - t0)
    return _funasr_model


def preload_async():
    """启动时后台预热 FunASR（守护线程，不阻塞 Flask 起端口）。

    只有 provider=local 才值得预热（qwen 走云端没有加载开销）；
    加载失败只打日志不崩溃——让首次识别请求去暴露真实错误，
    启动路径永远不该因为可选的 AI 能量起不来。
    """
    if provider() != "local":
        log.info("ASR provider=%s，跳过 FunASR 预热", provider())
        return

    def _load():
        try:
            _get_funasr()
        except Exception as e:  # noqa: BLE001 预热失败不致命，记录等首请求重试
            log.error("FunASR 预热失败（首次识别时会重试）: %s", e)

    threading.Thread(target=_load, daemon=True, name="funasr-preload").start()


def _run_with_temp_wav(wav_bytes: bytes, fn):
    """写临时 wav → 执行 fn(path) → 保证删临时文件。

    FunASR generate 与 dashscope Recognition 都只吃文件路径不吃字节流，
    所以两条路径都需要落盘中转；finally 删文件防止测试音频堆积。
    """
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="_asr_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(wav_bytes)
        return fn(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _transcribe_local(wav_bytes: bytes) -> str:
    """FunASR 本地识别（test_funasr.py 已验证的调用方式）。"""
    def _run(path: str) -> str:
        model = _get_funasr()
        res = model.generate(input=path, batch_size_s=300)
        return res[0]["text"] if res else ""
    return _run_with_temp_wav(wav_bytes, _run).strip()


def _transcribe_qwen(wav_bytes: bytes) -> str:
    """qwen 云端识别（test_asr_qwen.py 已验证的 dashscope 原生协议）。

    端点推导：该模型不走 OpenAI 兼容 HTTP（传音频一律 400 url error），
    必须用 dashscope Recognition 的 WebSocket 端点——把 asr_base_url
    的 https 换 wss、/compatible-mode/v1 换 /api-ws/v1/inference。
    key/端点每次调用前设置（幂等且便宜，避免模块加载顺序陷阱）。
    """
    import dashscope
    from dashscope.audio.asr import Recognition
    from http import HTTPStatus

    dashscope.api_key = AI["asr_api_key"]
    dashscope.base_websocket_api_url = (
        AI["asr_base_url"].replace("https://", "wss://")
        .replace("/compatible-mode/v1", "/api-ws/v1/inference")
    )
    recognition = Recognition(
        model=AI["asr_model"],
        format="wav",
        sample_rate=16000,
        callback=None,
    )

    def _run(path: str) -> str:
        result = recognition.call(path)
        if result.status_code != HTTPStatus.OK:
            # message 是云侧回的错误原因（如鉴权失败/音频格式问题），透传给上层转 502
            raise RuntimeError(f"qwen ASR 失败(HTTP {result.status_code}): {result.message}")
        sentences = result.get_sentence()
        return sentences[-1]["text"] if sentences else ""

    return _run_with_temp_wav(wav_bytes, _run).strip()


def transcribe(wav_bytes: bytes) -> str:
    """语音转文字入口。wav_bytes 必须是完整 WAV 文件字节。

    provider 在 import 时已定死（改 config 需重启后端，见 CFG 注释）；
    任何异常向上抛，由 app.py 统一转 HTTP 502——本模块不决定 HTTP 语义。
    """
    if not wav_bytes:
        raise ValueError("音频内容为空")
    if provider() == "qwen":
        return _transcribe_qwen(wav_bytes)
    return _transcribe_local(wav_bytes)


# ================================================================ LLM 指令解析
# system prompt（教学向）：把"输出格式、车号映射、speed 默认值"全部
# 写死在规则里，temperature=0 保证同类话术解析结果稳定可复现。
SYSTEM_PROMPT = """你是车队语音控车指令解析器。把用户的中文指令解析为 JSON，只输出 JSON，不要输出任何解释或其他内容。

输出格式：
{"car_node": "car_001", "actions": [{"cmd": "FORWARD", "speed": 50}], "reply": "好的，1号车前进"}

规则：
1. car_node：用户指明了哪台车才填对应节点 ID，没提哪台车则填空字符串 ""。
   车号映射："1号车/一号车"→car_001，"2号车/二号车"→car_002，"3号车/三号车"→car_003，以此类推。
2. actions 按话语中出现的顺序排列；cmd 只能取 FORWARD、BACK、LEFT、RIGHT、STOP 之一。
3. FORWARD/BACK 必须带 speed（整数 0~100），用户没说速度就用默认 50；STOP/LEFT/RIGHT 的 speed 固定为 null。
4. reply：用一句简短友好的中文向用户确认将要执行的动作。
5. 车队清单以用户消息中给出的为准。"""

_VALID_CMDS = {"FORWARD", "BACK", "LEFT", "RIGHT", "STOP"}
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _extract_json(content: str) -> dict:
    """从 LLM 回复中抠出 JSON 对象：剥掉可能的 ```json 围栏再解析。

    LLM 即使被要求"只输出 JSON"也可能带上 markdown 围栏或前后寒暄，
    先剥围栏是成本最低的容错；仍失败则交由调用方走重试。
    """
    s = (content or "").strip()
    m = _JSON_FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    obj = json.loads(s)
    if not isinstance(obj, dict):
        raise ValueError("LLM 输出的 JSON 不是对象")
    return obj


def parse_command(text: str, cars: list) -> dict:
    """调 LLM 把中文指令解析成固定结构（协议 16.3）。

    cars：注册表投影 [{node_id, name, device_id}]——只给车号与名字，
    不含密钥等敏感字段（LLM 不需要也不该看到）。
    返回 {"car_node": str, "actions": [...], "reply": str}。
    异常语义：
      - requests.RequestException 子类 = 网络/HTTP 问题（app.py 转 502）；
      - 两次重试后仍非合法 JSON → RuntimeError（app.py 转 502"AI 解析失败"）。
    """
    car_lines = "\n".join(f"- {c['node_id']}  {c['name']}" for c in cars)
    user_msg = f"车队清单：\n{car_lines}\n\n待解析指令：{text}"

    payload = {
        "model": AI["llm_model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        # temperature=0：解析类任务要确定性输出（已实测 1.2s 返回干净 JSON）
        "temperature": 0,
        "max_tokens": 300,
    }
    headers = {
        "Authorization": f"Bearer {AI['llm_api_key']}",
        "Content-Type": "application/json",
    }
    url = AI["llm_base_url"].rstrip("/") + "/chat/completions"

    last_err = None
    for attempt in (1, 2):  # 最多两次：原始请求 + 带错误提示的重试
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        try:
            return _extract_json(content)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            log.warning("LLM 第 %d 次输出非合法 JSON，重试：%s", attempt, content[:120])
            # 把失败输出和纠错指令追加进对话，让模型看着自己的错误改
            payload["messages"].append({"role": "assistant", "content": content})
            payload["messages"].append({
                "role": "user",
                "content": "你上次输出不是合法JSON，请重新只输出JSON",
            })
    raise RuntimeError(f"AI 解析失败（两次输出均非合法 JSON）: {last_err}")


def validate_actions(parsed: dict, valid_node_ids: set):
    """白名单校验 LLM 产出（协议 16.4）。返回 (car_node|None, actions)。

    为什么逐条丢弃而不是整批拒绝：一句话里"前进60然后跳一下"，
    JUMP 非法但 FORWARD 合法——丢掉非法项、保留合法项是对用户意图
    最忠实的执行；全部非法才抛错（没有可执行的就不该动任何车）。
    speed 的 bool 拦截：Python 里 True 是 int 子类，不拦的话
    "speed": true 会被当成 1 混过 0~100 校验。
    """
    if not isinstance(parsed, dict):
        raise ValueError("AI 输出结构异常")

    car_node = parsed.get("car_node") or None
    if car_node is not None and car_node not in valid_node_ids:
        raise ValueError(f"没有找到车：{car_node}")

    raw = parsed.get("actions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("没有可执行的有效指令")

    actions = []
    for i, a in enumerate(raw):
        if len(actions) >= 6:  # 上限 6 条：一句话里更多段落视为解析噪音
            log.warning("动作超过 6 条，丢弃第 %d 条之后的全部动作", i)
            break
        if not isinstance(a, dict):
            log.warning("丢弃非法动作（非对象）: %r", a)
            continue
        cmd = str(a.get("cmd") or "").upper()
        if cmd not in _VALID_CMDS:
            log.warning("丢弃白名单外动作: %r", a.get("cmd"))
            continue
        if cmd in ("FORWARD", "BACK"):
            speed = a.get("speed")
            if not isinstance(speed, int) or isinstance(speed, bool) or not 0 <= speed <= 100:
                log.warning("丢弃非法 %s（speed=%r 不在 0~100）", cmd, speed)
                continue
            actions.append({"cmd": cmd, "speed": speed})
        else:
            actions.append({"cmd": cmd, "speed": None})  # STOP/LEFT/RIGHT 忽略 speed

    if not actions:
        raise ValueError("没有可执行的有效指令")
    return car_node, actions
