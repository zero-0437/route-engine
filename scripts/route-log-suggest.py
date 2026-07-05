#!/usr/bin/env python3
"""
llm-rule-suggest.py — LLM 辅助规则生成工具

根据历史路由日志（JSONL）分析误匹配案例，输出 YAML diff 格式的规则调整建议，
供人工审核。

分析维度:
  1. 低置信度路由 (confidence < 0.5 但仍有路由结果)
  2. 误匹配候选 (路由结果与预期不符)
  3. 高频未路由 (短时间同一输入反复出现且从未匹配)
  4. 规则覆盖间隙 (语义明确但被路由到 fallback/triage)

用法:
  python scripts/llm-rule-suggest.py
  python scripts/llm-rule-suggest.py --log /path/to/logs.jsonl
  python scripts/llm-rule-suggest.py --log /path/to/logs.jsonl --output suggestions.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any


# ── 默认路径 ─────────────────────────────────────────────────────────
_LOG_PATH_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "logs", "route-engine.jsonl"
)
# 任务要求的默认路径 (生产环境)
_ALT_LOG_PATH = "/opt/data/logs/route-engine.jsonl"

# ── 分析阈值 ─────────────────────────────────────────────────────────
LOW_CONF_THRESHOLD = 0.5         # 低置信度判定阈值
HIGH_FREQ_WINDOW = timedelta(hours=1)   # 高频窗口
HIGH_FREQ_MIN_COUNT = 3          # 高频判定最小出现次数

# ── 常见 pattern → 预期 Agent 映射（用于"误匹配"启发式推断）─────────
_PATTERN_TO_AGENT_HINTS: dict[str, list[str]] = {
    "搜索": ["data-analyst"],
    "查询": ["data-analyst"],
    "检索": ["data-analyst"],
    "查找": ["data-analyst"],
    "调查": ["data-analyst"],
    "研究": ["data-analyst"],
    "创建": ["spec-agent"],
    "实现": ["spec-agent", "programmer"],
    "开发": ["spec-agent"],
    "构建": ["spec-agent"],
    "制作": ["spec-agent"],
    "编码": ["programmer"],
    "编程": ["programmer"],
    "函数": ["programmer"],
    "脚本": ["programmer"],
    "测试": ["programmer"],
    "文档": ["docs-writer"],
    "写文档": ["docs-writer"],
    "README": ["docs-writer"],
    "崩溃": ["error-analyst", "triage"],
    "异常": ["error-analyst"],
    "错误": ["error-analyst"],
    "bug": ["error-analyst", "triage"],
    "审查": ["error-analyst"],
    "代码审查": ["error-analyst"],
    "架构": ["pm-agent"],
    "方案": ["pm-agent"],
    "拆解": ["pm-agent"],
    "技术选型": ["pm-agent"],
    "UI": ["ui-designer"],
    "界面": ["ui-designer"],
    "设计.*界面": ["ui-designer"],
    "文件": ["file-ops"],
    "目录": ["file-ops"],
    "文件管理": ["file-ops"],
    "prompt": ["prompt-engineer"],
    "提示词": ["prompt-engineer"],
    "NAS": ["synology-helper"],
    "synology": ["synology-helper"],
}

# 分词语义提示词（短文本关键字匹配用）
_SEMANTIC_CLUES: dict[str, list[str]] = {
    "spec-agent": ["创建", "实现", "开发", "构建", "制作", "新功能", "新项目"],
    "programmer": ["写.*代码", "编程", "编码", "修复.*bug", "测试", "函数", "脚本"],
    "error-analyst": ["崩溃", "错误", "异常", "诊断", "审查", "bug", "故障"],
    "docs-writer": ["文档", "写文档", "README", "手册", "说明"],
    "ui-designer": ["UI", "界面", "设计.*界面", "布局", "交互"],
    "data-analyst": ["搜索", "查询", "检索", "查找", "调查", "研究"],
    "pm-agent": ["拆解", "架构", "方案", "技术选型", "可行性"],
    "file-ops": ["文件", "目录", "文件管理", "文件操作"],
    "prompt-engineer": ["prompt", "提示词"],
    "synology-helper": ["NAS", "synology", "DiskStation"],
}


# ── 辅助函数 ─────────────────────────────────────────────────────────


def resolve_log_path(args_log: str | None) -> str | None:
    """解析日志路径，返回第一个存在的路径，或 None"""
    candidates = []
    if args_log:
        candidates.append(args_log)
    candidates.append(_LOG_PATH_DEFAULT)
    candidates.append(_ALT_LOG_PATH)

    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def load_entries(log_path: str) -> list[dict]:
    """加载 JSONL 日志文件"""
    entries: list[dict] = []
    with open(log_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[警告] 第 {lineno} 行格式错误，已跳过: {e}", file=sys.stderr)
    return entries


def extract_cjk(text: str) -> str:
    """抽取中文字符"""
    return "".join(re.findall(r"[\u4e00-\u9fff]+", text))


def match_pattern_in_input(input_text: str, pattern: str) -> bool:
    """检查输入是否匹配某个规则 pattern（支持 regex 和普通文本）"""
    try:
        return bool(re.search(pattern, input_text, re.IGNORECASE))
    except re.error:
        return pattern.lower() in input_text.lower()


def suggest_expected_agent(input_text: str, matched_rules: list[str]) -> list[str]:
    """根据输入文本和已匹配规则，推测预期 Agent"""
    hinted: list[str] = []
    for pat, agents in _PATTERN_TO_AGENT_HINTS.items():
        if match_pattern_in_input(input_text, pat):
            hinted.extend(agents)

    input_lower = input_text.lower()
    for agent, keywords in _SEMANTIC_CLUES.items():
        for kw in keywords:
            try:
                if re.search(kw, input_lower, re.IGNORECASE):
                    hinted.append(agent)
                    break
            except re.error:
                if kw.lower() in input_lower:
                    hinted.append(agent)
                    break

    seen: set[str] = set()
    unique: list[str] = []
    for a in hinted:
        if a not in seen:
            seen.add(a)
            unique.append(a)
    return unique


# ── 分析器 ───────────────────────────────────────────────────────────


def analyze_low_confidence(entries: list[dict]) -> list[dict[str, Any]]:
    """分析 1. 低置信度路由 (confidence < 0.5 但仍有路由结果)"""
    suggestions: list[dict[str, Any]] = []
    for e in entries:
        agent = e.get("agent", "")
        conf = e.get("confidence", 0)
        if agent and conf < LOW_CONF_THRESHOLD and conf > 0:
            suggestions.append({
                "type": "low_confidence",
                "input": e.get("input", ""),
                "agent": agent,
                "confidence": conf,
                "matched": e.get("matched", []),
                "method": e.get("method", ""),
            })
    return suggestions


def analyze_misroute(entries: list[dict]) -> list[dict[str, Any]]:
    """分析 2. 误匹配候选 — 路由结果与预期不符"""
    suggestions: list[dict[str, Any]] = []
    for e in entries:
        agent = e.get("agent", "")
        if not agent:
            continue
        input_text = e.get("input", "")
        matched = e.get("matched", [])

        expected = suggest_expected_agent(input_text, matched)
        if expected and agent not in expected:
            suggestions.append({
                "type": "misroute",
                "input": input_text,
                "agent": agent,
                "confidence": e.get("confidence", 0),
                "matched": matched,
                "expected": expected,
                "method": e.get("method", ""),
            })
    return suggestions


def analyze_high_freq_unrouted(entries: list[dict]) -> list[dict[str, Any]]:
    """分析 3. 高频未路由 — 同一输入短时间重复出现且从未匹配"""
    by_input: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        inp = e.get("input", "").strip()
        if inp:
            by_input[inp].append(e)

    suggestions: list[dict[str, Any]] = []
    for inp, group in by_input.items():
        if len(group) < HIGH_FREQ_MIN_COUNT:
            continue

        group.sort(key=lambda x: x.get("ts", ""))
        window_start = None
        window_entries: list[dict] = []
        for g in group:
            ts_str = g.get("ts", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                continue
            if window_start is None:
                window_start = ts
                window_entries = [g]
            elif ts - window_start <= HIGH_FREQ_WINDOW:
                window_entries.append(g)
            else:
                window_start = ts
                window_entries = [g]

            if len(window_entries) >= HIGH_FREQ_MIN_COUNT:
                break

        if len(window_entries) < HIGH_FREQ_MIN_COUNT:
            continue

        all_unrouted = all(
            e.get("method") in ("unrouted", "llm_fallback") or not e.get("agent")
            for e in window_entries
        )
        if all_unrouted:
            suggestions.append({
                "type": "high_freq_unrouted",
                "input": inp,
                "count": len(window_entries),
                "first_ts": window_entries[0].get("ts", ""),
                "last_ts": window_entries[-1].get("ts", ""),
            })
    return suggestions


def analyze_coverage_gaps(entries: list[dict]) -> list[dict[str, Any]]:
    """分析 4. 规则覆盖间隙 — 语义明确但被路由到 fallback/triage"""
    suggestions: list[dict[str, Any]] = []
    for e in entries:
        agent = e.get("agent", "")
        input_text = e.get("input", "")
        method = e.get("method", "")
        matched = e.get("matched", [])

        if method not in ("unrouted", "llm_fallback") and agent not in ("triage", ""):
            continue

        expected = suggest_expected_agent(input_text, matched)
        if expected:
            suggestions.append({
                "type": "coverage_gap",
                "input": input_text,
                "current_agent": agent or "(unrouted)",
                "expected": expected,
                "method": method,
                "matched": matched,
            })
    return suggestions


# ── 建议生成 ─────────────────────────────────────────────────────────


def generate_suggestion_yaml(suggestion: dict[str, Any], index: int) -> str:
    """将一条分析结果渲染为 YAML diff 块"""

    stype = suggestion["type"]
    input_text = suggestion.get("input", "")
    lines: list[str] = []

    if stype == "low_confidence":
        agent = suggestion["agent"]
        conf = suggestion["confidence"]
        matched = suggestion.get("matched", [])
        expected = suggest_expected_agent(input_text, matched)

        lines.append(f"# Suggestion {index} — 低置信度: \"{input_text[:50]}\" 路由到 {agent} 置信度不足")
        lines.append("## 现象")
        lines.append(f'- 用户输入: "{input_text}"')
        lines.append(f"- 路由结果: {agent} (confidence: {conf})")
        lines.append(f"- 匹配规则: {matched}")
        lines.append(f"- 预期 Agent: {expected[0] if expected else '(待确认)'}")
        lines.append("")
        lines.append("## 建议")
        target = expected[0] if expected else agent
        lines.append(f"在 {target}.yaml 中补充规则以提高识别率:")

        cjk_chars = extract_cjk(input_text)
        if cjk_chars:
            blocks = re.findall(r"[\u4e00-\u9fff]{2,}", input_text)
            if blocks:
                pattern = blocks[0]
            else:
                pattern = cjk_chars[:4]
        else:
            words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]*", input_text.lower())
            pattern = " ".join(words[:3]) if words else input_text[:20]

        lines.append(f"  - type: phrase")
        lines.append(f"    pattern: {pattern}")
        lines.append(f"    weight: 0.8")
        lines.append(f"    skills: []")
        lines.append(f"    fuzzy: true")

    elif stype == "misroute":
        agent = suggestion["agent"]
        expected = suggestion.get("expected", [])
        matched = suggestion.get("matched", [])

        lines.append(f"# Suggestion {index} — 误匹配候选: \"{input_text[:50]}\" 应路由到 {expected[0] if expected else '?'}")
        lines.append("## 现象")
        lines.append(f'- 用户输入: "{input_text}"')
        lines.append(f"- 当前路由: {agent} (confidence: {suggestion['confidence']})")
        lines.append(f"- 预期路由: {expected[0] if expected else '(未知)'}")
        lines.append(f"- 已匹配规则: {matched}")
        lines.append("")
        if expected:
            lines.append("## 建议")
            lines.append(f"在 {expected[0]}.yaml 中添加规则以优先捕获这类输入:")

            cjk_blocks = re.findall(r"[\u4e00-\u9fff]{2,}", input_text)
            if cjk_blocks:
                pattern = cjk_blocks[0]
            else:
                en_words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]*", input_text.lower())
                pattern = en_words[0] if en_words else input_text[:20]

            lines.append(f"  - type: phrase")
            lines.append(f"    pattern: {pattern}")
            lines.append(f"    weight: 0.8")
            lines.append(f"    skills: []")
            lines.append(f"    fuzzy: true")
        else:
            lines.append("## 建议")
            lines.append("  建议人工审核此案例，确定正确的目标 Agent 后补充规则。")

    elif stype == "high_freq_unrouted":
        count = suggestion["count"]

        lines.append(f"# Suggestion {index} — 高频未路由: \"{input_text[:50]}\" 出现 {count} 次均无匹配")
        lines.append("## 现象")
        lines.append(f'- 用户输入: "{input_text}"')
        lines.append(f"- 出现次数: {count} 次")
        lines.append(f"- 时间范围: {suggestion.get('first_ts', '')} ~ {suggestion.get('last_ts', '')}")
        lines.append("- 状态: 从未匹配到任何规则")
        lines.append("")
        lines.append("## 建议")
        lines.append("根据语义推断，建议在以下文件中添加规则:")

        expected = suggest_expected_agent(input_text, [])
        if expected:
            for ea in expected[:2]:
                cjk_blocks = re.findall(r"[\u4e00-\u9fff]{2,}", input_text)
                pattern = cjk_blocks[0] if cjk_blocks else input_text[:20]
                lines.append(f"在 {ea}.yaml 中添加:")
                lines.append(f"  - type: phrase")
                lines.append(f"    pattern: {pattern}")
                lines.append(f"    weight: 0.8")
                lines.append(f"    skills: []")
                lines.append(f"    fuzzy: true")
                lines.append("")
        else:
            lines.append("  建议人工确认目标 Agent 后补充规则。")

    elif stype == "coverage_gap":
        current = suggestion.get("current_agent", "?")
        expected = suggestion.get("expected", [])

        lines.append(f"# Suggestion {index} — 规则覆盖间隙: \"{input_text[:50]}\" 落入 {current}")
        lines.append("## 现象")
        lines.append(f'- 用户输入: "{input_text}"')
        lines.append(f"- 当前路由: {current}")
        lines.append(f"- 预期 Agent: {expected[0] if expected else '(待确认)'}")
        lines.append(f"- 方法: {suggestion.get('method', '')}")
        lines.append("")
        if expected:
            lines.append("## 建议")
            lines.append(f"在 {expected[0]}.yaml 中新增规则以覆盖这类语义:")

            cjk_blocks = re.findall(r"[\u4e00-\u9fff]{2,}", input_text)
            if cjk_blocks:
                pattern = cjk_blocks[0]
            else:
                en_words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]*", input_text.lower())
                pattern = " ".join(en_words[:2]) if en_words else input_text[:20]

            lines.append(f"  - type: phrase")
            lines.append(f"    pattern: {pattern}")
            lines.append(f"    weight: 0.8")
            lines.append(f"    skills: []")
            lines.append(f"    fuzzy: true")
        else:
            lines.append("## 建议")
            lines.append("  建议人工审核此案例，确定目标 Agent 后补充规则。")

    return "\n".join(lines) + "\n"


# ── 主流程 ───────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="LLM 辅助规则生成工具 — 分析路由日志，输出 YAML diff 建议",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  %(prog)s                                          # 使用默认路径\n"
            "  %(prog)s --log /custom/path/logs.jsonl            # 指定日志\n"
            "  %(prog)s --log logs.jsonl --output suggestions.yaml\n"
        ),
    )
    parser.add_argument(
        "--log",
        type=str,
        default=None,
        help=f"路由日志路径 (默认: {_ALT_LOG_PATH} 或 {_LOG_PATH_DEFAULT})",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出文件路径 (默认: stdout)",
    )
    args = parser.parse_args()

    # 1. 解析日志路径
    log_path = resolve_log_path(args.log)
    if log_path is None:
        msg = (
            f"[信息] 未找到路由日志文件。\n"
            f"  查找路径:\n"
            f"    - {args.log or '(未指定)'}\n"
            f"    - {_LOG_PATH_DEFAULT}\n"
            f"    - {_ALT_LOG_PATH}\n"
            f"  请先运行路由引擎产生日志，或使用 --log 指定路径。\n"
            f"  正常退出（非错误）。"
        )
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(f"# {msg}\n")
        else:
            print(msg)
        return 0

    # 2. 加载日志
    entries = load_entries(log_path)
    if not entries:
        msg = f"[信息] 日志文件 {log_path} 为空，无数据可分析。正常退出。"
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(f"# {msg}\n")
        else:
            print(msg)
        return 0

    print(f"[信息] 加载了 {len(entries)} 条日志条目 from {log_path}", file=sys.stderr)

    # 3. 执行分析
    low_conf = analyze_low_confidence(entries)
    misroutes = analyze_misroute(entries)
    high_freq = analyze_high_freq_unrouted(entries)
    gaps = analyze_coverage_gaps(entries)

    # 去重 — 相同输入可能出现在多个分析维度
    seen_inputs: set[str] = set()
    all_suggestions: list[dict[str, Any]] = []

    for s in low_conf:
        inp = s.get("input", "")
        if inp not in seen_inputs:
            seen_inputs.add(inp)
            all_suggestions.append(s)

    for s in misroutes:
        inp = s.get("input", "")
        if inp not in seen_inputs:
            seen_inputs.add(inp)
            all_suggestions.append(s)

    for s in high_freq:
        inp = s.get("input", "")
        if inp not in seen_inputs:
            seen_inputs.add(inp)
            all_suggestions.append(s)

    for s in gaps:
        inp = s.get("input", "")
        if inp not in seen_inputs:
            seen_inputs.add(inp)
            all_suggestions.append(s)

    # 4. 生成输出
    output_lines: list[str] = [
        f"# LLM 辅助规则生成 — 分析报告",
        f"# 来源: {log_path}",
        f"# 条目数: {len(entries)}",
        f"# 生成的建议: {len(all_suggestions)}",
        f"#",
        f"# 分析维度:",
        f"#   - 低置信度路由: {len(low_conf)} 条",
        f"#   - 误匹配候选: {len(misroutes)} 条",
        f"#   - 高频未路由: {len(high_freq)} 条",
        f"#   - 规则覆盖间隙: {len(gaps)} 条",
        f"",
    ]

    if not all_suggestions:
        output_lines.append("# 未发现需要调整的案例，当前规则覆盖良好。")
    else:
        for i, suggestion in enumerate(all_suggestions, 1):
            block = generate_suggestion_yaml(suggestion, i)
            output_lines.append(block)
            output_lines.append("---")
            output_lines.append("")

    output_text = "\n".join(output_lines)

    # 5. 输出
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_text)
        print(f"[完成] 建议已写入: {args.output}", file=sys.stderr)
    else:
        print(output_text)

    print(
        f"[摘要] 低置信度={len(low_conf)}, "
        f"误匹配={len(misroutes)}, "
        f"高频未路由={len(high_freq)}, "
        f"覆盖间隙={len(gaps)}, "
        f"总建议={len(all_suggestions)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
