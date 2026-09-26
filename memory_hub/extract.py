# -*- coding: utf-8 -*-
"""记忆条目抽取（从对话中提取值得长期记住的内容）。

Online 模式的抽取器（design 参照 Mem0 custom instructions + LangMem hot path）：
提示词注入已有记忆防重，只提取对话明文出现的信息；调用方可传
custom_instructions 定制领域抽取口径（Mem0 custom_instructions 思路）。
依赖经 deps 接缝注入——LLM 不可用直接返回 []（静默降级）。
"""
from . import deps

KINDS = ("preference", "fact", "decision", "person", "project", "procedure")

EXTRACT_SYSTEM = """你是长期记忆提取器。从一轮对话中提取 0~3 条值得跨会话长期记住的信息。

只提取以下类别：
- preference：用户明确表达的偏好/习惯（如汇报格式、沟通习惯）
- fact：用户告知的稳定事实（与自己或公司相关，且答案本身之外的）
- decision：用户做出的决定/拍板
- person：人物与关系信息（谁负责什么、怎么联系）
- project：项目关键节点信息
- procedure：用户认可的可复用做法/流程（怎么做事的方法，如「对账先导出流水再核对」）

纪律：
1. 只提取对话明文出现的信息，禁止推断、脑补。
2. 一次性内容（本次提问的答案本身、寒暄、临时指令）不要记。
3. 与已有记忆重复或语义被已有记忆覆盖的不要记。
4. 每条同时给出 entities：该条提到的人名/机构/项目等专有实体（≤3 个，无则空数组）。
输出 JSON：{"memories": [{"kind": "preference", "content": "…",
"entities": ["老王", "对账"]}]}
无值得记的内容时输出 {"memories": []}。
"""


def extract_items(user_msg: str, ai_msg: str, existing: list = None,
                  custom_instructions: str = "") -> list:
    """LLM 抽取候选条目 [{kind, content, entities?}]；不可用/不合法返回 []（不写库）。

    custom_instructions：领域定制抽取口径（允许记什么、格式、few-shot 示例），
    追加在系统提示词之后——只收窄不放宽基础纪律。"""
    if not deps.llm_available() or not (user_msg or "").strip():
        return []
    existing = existing or []
    system = EXTRACT_SYSTEM
    if custom_instructions and custom_instructions.strip():
        system += "\n调用方定制要求（优先级高于以上类别示例，但不得违反纪律）：\n%s" \
            % custom_instructions.strip()[:2000]
    content = deps.llm_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": "已有记忆：\n%s\n\n本轮对话：\n用户：%s\nAI：%s" % (
             "\n".join("- " + e for e in existing[-20:]) or "（无）",
             user_msg[:600], (ai_msg or "")[:600])}],
        temperature=0.0)
    if not content:
        return []
    data = deps.parse_json(content)
    if not data or not isinstance(data.get("memories"), list):
        return []
    out = []
    for m in data["memories"]:
        if not isinstance(m, dict):
            continue
        kind = m.get("kind") if m.get("kind") in KINDS else "fact"
        ctext = str(m.get("content", "")).strip()
        if 2 <= len(ctext) <= 200:
            item = {"kind": kind, "content": ctext}
            ents = [str(x).strip()[:30] for x in (m.get("entities") or [])
                    if str(x).strip()]
            ents = list(dict.fromkeys(ents))[:5]
            if ents:
                item["entities"] = ents
            out.append(item)
    return out
