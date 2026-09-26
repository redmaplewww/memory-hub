# -*- coding: utf-8 -*-
"""独立包测试引导：把包根加入 sys.path（无 LLM 门禁——独立部署允许降级模式）。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
