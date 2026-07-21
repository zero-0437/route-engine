"""
zero-token-router plugin — 前置路由引擎，用户消息进入 Agent 前自动路由。

工作原理：
  pre_llm_call hook：在每个 LLM 调用前，检测到新的用户消息时，调用路由引擎
  获取路由结果，作为 context 注入到用户消息中。

  pre_gateway_dispatch hook：在网关模式下，直接重写消息文本，将路由结果
  作为前置信息注入。

基础设施层保证，不依赖 Agent 记忆。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────────

ROUTER_PROJECT = "/opt/data/hermes-zero-token-router"
ROUTER_SCRIPT = os.path.join(ROUTER_PROJECT, "scripts", "mcp-router-server.py")
ENGINE_SCRIPT = os.path.join(ROUTER_PROJECT, "src", "route_engine.py")
PYTHON = sys.executable

# 已路由的 turn_id 缓存，避免 pre_llm_call 同一轮重复路由
_routed_turns: set[int] = set()
# 已路由的消息哈希缓存，避免 pre_gateway_dispatch 重复重写
_gateway_seen: set[int] = set()
_routed_lock = threading.Lock()
ROUTING_MARKER = "[路由引擎前置路由]"


# ── 路由引擎调用 ─────────────────────────────────────────────────────────

def _route(text: str) -> Optional[str]:
    """调用路由引擎，返回路由结果 JSON 字符串。失败时返回 None。"""
    try:
        result = subprocess.run(
            [PYTHON, ENGINE_SCRIPT, text],
            capture_output=True, text=True, timeout=5,
            cwd=ROUTER_PROJECT,
            env={**os.environ, "PYTHONPATH": os.path.join(ROUTER_PROJECT, "src")},
        )
        if result.returncode != 0:
            logger.warning("route engine stderr: %s", result.stderr.strip())
            return None
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("route engine timed out (5s)")
        return None
    except Exception as e:
        logger.warning("route engine error: %s", e)
        return None


def _format_routing_context(raw: str) -> str:
    """将路由引擎的输出格式化为注入 context。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""

    agent = data.get("agent", "?")
    confidence = data.get("confidence", 0)
    method = data.get("method", "?")
    mode = data.get("mode", "auto")

    if method == "llm_fallback" or confidence == 0:
        # 无匹配 → 提示主 Agent 兜底
        return (
            f"\n\n[路由引擎前置路由]\n"
            f"状态: 无明确匹配 (llm_fallback)\n"
            f"→ 请由主 Agent (LLM) 自行判断路由目标。"
        )

    if mode == "auto_tiebreak":
        candidates = data.get("candidates", []) or [
            a for a, s in data.get("details", {}).get("scores", []) if s > 0
        ]
        return (
            f"\n\n[路由引擎前置路由]\n"
            f"状态: 平局裁决\n"
            f"候选: {', '.join(candidates)}\n"
            f"→ 请由主 Agent 二次判断，选择最合适的 Agent 委派。"
        )

    # auto 模式：明确路由目标
    return (
        f"\n\n[路由引擎前置路由]\n"
        f"目标 Agent: {agent}\n"
        f"置信度: {confidence}\n"
        f"→ 明确单意图任务，可直接 delegate_task 到 {agent}。"
    )


# ── Hook: pre_llm_call ───────────────────────────────────────────────────

def _on_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """在 LLM 调用前，对新的用户消息执行前置路由注入。"""
    turn_id = kwargs.get("turn_id")
    user_message = kwargs.get("user_message", "")

    if not turn_id or not user_message:
        return None

    # 避免同一 turn 重复路由（retry/重新生成时跳过）
    with _routed_lock:
        if turn_id in _routed_turns:
            return None
        _routed_turns.add(turn_id)

    # pre_gateway_dispatch 已注入过，跳过避免重复
    if ROUTING_MARKER in user_message:
        return None

    # 调用路由引擎
    raw = _route(user_message)
    if not raw:
        return None

    context = _format_routing_context(raw)
    if not context:
        return None

    logger.info(
        "路由引擎前置路由: turn=%s, msg=%s, result=%s",
        turn_id, user_message[:40], raw[:100],
    )

    return {"context": context}


# ── Hook: pre_gateway_dispatch ───────────────────────────────────────────

def _on_pre_gateway_dispatch(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """在网关消息分发前，将路由结果注入消息文本。"""
    event = kwargs.get("event")
    if not event or not hasattr(event, "text") or not event.text:
        return None

    user_text = event.text

    # 已通过消息哈希去重，避免同一消息被多次重写
    text_hash = hash(user_text)
    if text_hash in _gateway_seen:
        return None
    _gateway_seen.add(text_hash)

    raw = _route(user_text)
    if not raw:
        return None

    context = _format_routing_context(raw)
    if not context:
        return None

    # 重写消息文本：保留原消息 + 追加路由上下文
    new_text = user_text + context
    logger.info(
        "路由引擎前置路由(网关): %s → %s",
        user_text[:40], raw[:100],
    )

    return {"action": "rewrite", "text": new_text}


# ── 插件入口 ──────────────────────────────────────────────────────────────

from .patch_skills import patch as _patch_skills_prompt


def register(ctx) -> None:
    """注册插件钩子。"""
    _patch_skills_prompt()
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    logger.info("zero-token-router 插件已注册: pre_llm_call + pre_gateway_dispatch")
