# -*- coding: utf-8 -*-
"""MemoryHub 通用记忆模块（M8，DESIGN-MEMORY.md）。

三层记忆 + 治理面，承接后续所有涉及记忆的系统开发：
  L1 profiles   当前态层（用户画像/偏好聚合，整体重写、版本化）
  L2 memories   语义条目层（向量+关键词 RRF 召回、双时间戳 invalidate）
  L3 episodes   事件溯源层（对话轮次摘要，只增不删）
  治理面        memory_events 审计流（照 work_events 模式）+ ACL + 生命周期

用法：
    from twin_agent.memory_hub import MemoryHub
    hub = MemoryHub()                      # 默认 runtime/dialogue.db
    hub.write([...], actor=...)            # 落库（含去重与冲突消解）
    hub.recall("问题", subject=user_id)     # 跨层召回
"""
from .core import MemoryHub, WriteReceipt

__all__ = ["MemoryHub", "WriteReceipt"]
