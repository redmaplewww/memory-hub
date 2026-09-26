# -*- coding: utf-8 -*-
"""Ollama 本地后端适配器（纯标准库，可选接入，不影响零依赖纪律）。

用法：
    from memory_hub import backends_ollama
    backends_ollama.configure()                    # 默认 qwen35-fix:9b + qwen3-embedding:0.6b
    backends_ollama.configure(chat_model="qwen2.5:7b")
    print(backends_ollama.probe())                 # 体检：模型可用性 / 延迟

本机实测注意事项（2026-09）：
- qwen35-fix:9b 是**思考型模型**：不关思考时内容全部写进 message.thinking、
  content 为空（单次 47~96 秒且拿不到结果）。适配器默认 think=False（实测 2 秒
  返回有效 JSON）；若该模型不支持 think 参数（部分 ollama 版本报 500），
  自动回退到普通调用并从 thinking 里摘取 JSON。
- keep_alive 让模型常驻，避免反复加载/卸载（本机切换大模型曾把服务压崩）。
- 失败（HTTP 500 / 连接被拒）自动重试一次；仍失败返回 None，由 core 静默降级。
"""
import json
import math
import time
import urllib.request

HOST = "http://127.0.0.1:11434"
CHAT_MODEL = "qwen35-fix:9b"
EMBED_MODEL = "qwen3-embedding:0.6b"
TIMEOUT = 300              # 本地大模型兜底
NUM_PREDICT = 1024         # 输出上限（防跑飞；思考型模型在 think=False 下够用）
KEEP_ALIVE = "30m"         # 模型常驻，避免换模型导致的加载抖动
NO_THINK = True            # 优先尝试 think=False（思考型模型提速关键）


def _post(path, payload, timeout=TIMEOUT):
    req = urllib.request.Request(
        HOST + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _last_json(text):
    """取文本中最后一个可解析的 JSON 对象（思考文本里可能夹带示例/推理）。

    优先取「含已知业务键」的对象；否则退回最后一个 dict。
    """
    if not text:
        return None
    dec = json.JSONDecoder()
    found = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                if isinstance(obj, dict):
                    found.append(obj)
            except Exception:
                pass
        i += 1
    if not found:
        return None
    known = ("relation", "relations", "memories", "order", "preferences", "关系")
    for obj in reversed(found):
        if any(k in obj for k in known):
            return obj
    return found[-1]


def llm_chat(messages, temperature=0.1, json_mode=True):
    """对话补全。返回字符串（json_mode 下保证是 JSON 文本）或 None（失败降级）。"""
    payload = {"model": CHAT_MODEL, "messages": messages, "stream": False,
               "options": {"temperature": temperature,
                           "num_predict": NUM_PREDICT},
               "keep_alive": KEEP_ALIVE}
    if json_mode:
        payload["format"] = "json"
    think_opts = (False, None) if NO_THINK else (None,)
    for attempt in range(2):
        for think in think_opts:
            req = dict(payload)
            if think is not None:
                req["think"] = think
            try:
                d = _post("/api/chat", req)
            except Exception:
                continue
            msg = d.get("message", {}) or {}
            txt = (msg.get("content") or "").strip()
            if txt:
                if json_mode:
                    obj = _last_json(txt)
                    if obj is not None:
                        return json.dumps(obj, ensure_ascii=False)
                return txt
            # content 为空（思考型模型）→ 从 thinking 里摘
            th = msg.get("thinking") or ""
            if th:
                obj = _last_json(th)
                if obj is not None:
                    return json.dumps(obj, ensure_ascii=False)
        time.sleep(1.0)
    return None


def llm_available():
    try:
        with urllib.request.urlopen(HOST + "/api/tags", timeout=5) as r:
            json.loads(r.read().decode("utf-8"))
        return True
    except Exception:
        return False


def parse_json(text):
    """尽力解析：直解 → 剥围栏 → 摘取 JSON 对象。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    t = text.strip().strip("`")
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            pass
    return _last_json(text)


def _norm(vec):
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def embed_one(text):
    if not (text or "").strip():
        return None
    for _ in range(2):
        try:
            v = _post("/api/embeddings",
                      {"model": EMBED_MODEL, "prompt": text,
                       "keep_alive": KEEP_ALIVE},
                      timeout=120).get("embedding")
            return _norm(v) if v else None
        except Exception:
            time.sleep(0.6)
    return None


def embed(texts):
    out = [embed_one(t) for t in texts or []]
    return out or None


def embed_available():
    try:
        return embed_one("ping") is not None
    except Exception:
        return False


def probe(chat_model=None, embed_model=None):
    """体检：返回各模型可用性与单次延迟（不改变已注入配置）。"""
    out = {"host": HOST, "chat_model": chat_model or CHAT_MODEL,
           "embed_model": embed_model or EMBED_MODEL}
    out["server"] = llm_available()
    if out["server"]:
        t0 = time.time()
        try:
            r = _post("/api/chat", {
                "model": out["chat_model"], "stream": False, "format": "json",
                "think": False, "keep_alive": KEEP_ALIVE,
                "messages": [{"role": "user", "content": "输出 {\"ok\": true}"}],
                "options": {"temperature": 0.0, "num_predict": 64}})
            out["chat_ok"] = bool((r.get("message", {}) or {}).get("content"))
        except Exception as e:
            out["chat_ok"] = False
            out["chat_error"] = str(e)[:80]
        out["chat_seconds"] = round(time.time() - t0, 1)
        t0 = time.time()
        v = embed_one("健康检查")
        out["embed_ok"] = bool(v)
        out["embed_dim"] = len(v) if v else 0
        out["embed_seconds"] = round(time.time() - t0, 1)
    return out


def configure(chat_model=None, embed_model=None, host=None, no_think=None):
    """注入 deps（可重复调用换模型）。"""
    global CHAT_MODEL, EMBED_MODEL, HOST, NO_THINK
    if chat_model:
        CHAT_MODEL = chat_model
    if embed_model:
        EMBED_MODEL = embed_model
    if host:
        HOST = host
    if no_think is not None:
        NO_THINK = no_think
    from . import deps
    deps.configure(llm_chat=llm_chat, llm_available=llm_available,
                   parse_json=parse_json, embed=embed, embed_one=embed_one,
                   embed_available=embed_available)
