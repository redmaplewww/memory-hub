# -*- coding: utf-8 -*-
"""memory_hub 外部依赖接缝（打包分发用）。

本模块只依赖三个外部能力：LLM 对话 / 向量嵌入 / 运行时目录。
- twin-agent 环境内：import 时自动绑定 twin_agent.llm /
  twin_agent.retrieval.embed / twin_agent.config.RUNTIME_DIR（行为与直连一致）。
- 独立部署（打包到其他项目）：import 后调用 configure(...) 注入实现；
  未注入时全部安全降级——LLM 不可用则跳过抽取与冲突裁决，向量不可用则
  关键词单路召回，运行时目录取 TWIN_RUNTIME 环境变量或 ./runtime。

所有访问器永不抛异常（返回 None/False 由调用方静默降级）——这是
memory_hub「绝不阻塞宿主主链路」纪律的一部分。
"""
import os

_impl = {
    "llm_chat": None, "llm_available": None, "parse_json": None,
    "embed": None, "embed_one": None, "embed_available": None,
    "runtime_dir": None,
}


def configure(*, llm_chat=None, llm_available=None, parse_json=None,
              embed=None, embed_one=None, embed_available=None, runtime_dir=None):
    """注入自定义实现（独立部署用；可重复调用，覆盖先前绑定）。

    llm_chat(messages, temperature=0.1, json_mode=True) -> str | None
    llm_available() -> bool
    parse_json(text) -> dict | list | None
    embed(texts: list) -> [[float,...],...] | None
    embed_one(text) -> [float,...] | None
    embed_available() -> bool
    runtime_dir() -> str
    """
    for k, v in dict(llm_chat=llm_chat, llm_available=llm_available,
                     parse_json=parse_json, embed=embed, embed_one=embed_one,
                     embed_available=embed_available,
                     runtime_dir=runtime_dir).items():
        if v is not None:
            _impl[k] = v


def _auto_bind():
    """twin-agent 包内自动绑定（独立部署时 ImportError 静默失败）。"""
    try:
        from .. import llm as _tllm
        _impl["llm_chat"] = lambda msgs, temperature=0.1, json_mode=True: _tllm.chat(
            msgs, segment="extract", temperature=temperature, json_mode=json_mode)
        _impl["llm_available"] = lambda: _tllm.available("extract")
        _impl["parse_json"] = _tllm._parse_json
    except ImportError:
        pass
    try:
        from ..retrieval import embed as _te
        _impl["embed"] = _te.embed
        _impl["embed_one"] = _te.embed_one
        _impl["embed_available"] = _te.available
    except ImportError:
        pass
    try:
        from .. import config as _tc
        _impl["runtime_dir"] = lambda: _tc.RUNTIME_DIR
    except ImportError:
        pass


_auto_bind()


# ---------------- 访问器（永不抛异常） ----------------

def llm_chat(messages, temperature=0.1, json_mode=True, max_tokens=None):
    fn = _impl["llm_chat"]
    if fn is None:
        return None
    try:
        kwargs = {}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return fn(messages, temperature=temperature, json_mode=json_mode,
                  **kwargs)
    except Exception:
        return None


def llm_available():
    fn = _impl["llm_available"]
    try:
        return bool(fn()) if fn else False
    except Exception:
        return False


def parse_json(text):
    fn = _impl["parse_json"]
    try:
        return fn(text) if fn else None
    except Exception:
        return None


def embed_one(text):
    fn = _impl["embed_one"]
    try:
        return fn(text) if fn else None
    except Exception:
        return None


def embed(texts):
    fn = _impl["embed"]
    try:
        return fn(texts) if fn else None
    except Exception:
        return None


def embed_available():
    fn = _impl["embed_available"]
    try:
        return bool(fn()) if fn else False
    except Exception:
        return False


def runtime_dir():
    fn = _impl["runtime_dir"]
    if fn is not None:
        try:
            return fn()
        except Exception:
            pass
    return os.environ.get("TWIN_RUNTIME", os.path.join(os.getcwd(), "runtime"))
