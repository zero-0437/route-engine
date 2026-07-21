"""patch_skills.py — 截停 skills prompt 注入的 monkey-patch。

路由引擎已接管技能→Agent 映射，system prompt 中的 <available_skills> 列表完全冗余。
本模块在插件 register() 入口调用，替换所有命名空间中的 build_skills_system_prompt()。

恢复方式：禁用插件或删除本模块中的 _patch() 调用。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

logger = logging.getLogger("zero-token-router.patch")


def _empty_skills_prompt(*args: Any, **kwargs: Any) -> str:
    """替换函数：返回空字符串，跳过 ~2K tokens 的技能列表注入。"""
    return ""


def patch() -> None:
    """Monkey-patch build_skills_system_prompt 在所有已加载命名空间中。

    Hermes 调用链路：
      system_prompt.py -> _r.build_skills_system_prompt(...)
                           ^ run_agent 模块（from agent.prompt_builder import ... 复制引用）
      run_agent.py        from agent.prompt_builder import build_skills_system_prompt

    因此必须在 agent.prompt_builder 和所有已复制的命名空间中同时替换。
    """
    # 1. 替换源头
    try:
        import agent.prompt_builder as pb

        pb.build_skills_system_prompt = _empty_skills_prompt
        logger.info("agent.prompt_builder.build_skills_system_prompt 已替换")
    except Exception as e:
        logger.warning("prompt_builder patch 失败: %s", e)

    # 2. 遍历已加载模块，替换所有 from-import 的副本引用
    count = 0
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        try:
            if (
                hasattr(mod, "build_skills_system_prompt")
                and getattr(mod, "build_skills_system_prompt", None) is not _empty_skills_prompt
            ):
                mod.build_skills_system_prompt = _empty_skills_prompt
                count += 1
                logger.debug("  -> patched %s.build_skills_system_prompt", mod_name)
        except Exception:
            continue

    logger.info("Skills prompt 注入已截停，共 patch %d 个模块", count)


def unpatch() -> None:
    """恢复原函数（重启进程更可靠，此函数仅供文档参考）。"""
    removed = 0
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        try:
            if (
                hasattr(mod, "build_skills_system_prompt")
                and getattr(mod, "build_skills_system_prompt", None) is _empty_skills_prompt
            ):
                del mod.build_skills_system_prompt
                removed += 1
        except Exception:
            continue
    logger.info("Skills prompt 已恢复，从 %d 个模块移除补丁", removed)
