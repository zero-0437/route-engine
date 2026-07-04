#!/usr/bin/env python3
"""
chain_executor.py — chain 编排引擎（状态机）

调用方式：
  python3 scripts/chain_executor.py advance \\
    --task_id T-001 \\
    --chain_def '[{"agent":"programmer","goal":"..."},{"agent":"error-analyst","goal":"..."}]' \\
    --chain_step_skills '{"programmer_0":["test-driven-development"]}' \\
    --last_result '{"agent":"programmer","status":"DONE","output_path":"..."}'

  python3 scripts/chain_executor.py start \\
    --task_id T-001 \\
    --chain_def '[{"agent":"programmer","goal":"TDD 实现 + self-review"},...]' \\
    --chain_step_skills '{"programmer@0":[...]}' \\
    --chain_owner programmer

  python3 scripts/chain_executor.py run \\
    --task_id T-001 \\
    --chain_agent programmer \\
    --last_result '{"status":"init"}'

chain_executor 不调用 delegate_task — 只产出决策 JSON。
主 Agent 读 JSON → delegate_task → 拿到结果后再调 chain_executor。
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys

try:
    import yaml
except ImportError:
    yaml = None

import sys as _sys
_SCRIPTS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)
del _SCRIPTS_DIR, _sys
from chain_config import SCRIPT_DIR, load_yaml_safe, load_chain

# ── 路径计算 ────────────────────────────────────

# ── 默认配置 ────────────────────────────────────
MAX_RETRY = 3
STATE_DIR = "/opt/data/.shared"
INDEX_YAML_PATH = os.path.join(SCRIPT_DIR, "..", "route-map", "index.yaml")

# ── verification 常量 ────────────────────────────
VERIFICATION_TIMEOUT = 30          # 每个 verify_command 的超时秒数
MAX_OUTPUT_LENGTH = 2000           # stdout/stderr 截断长度
VERIFY_COMMAND_BASENAMES = frozenset({
    "test", "true", "false", "git", "head", "cat", "grep",
    "wc", "diff", "echo", "ls", "sort", "uniq", "cut", "tr",
    "sed", "awk", "find", "xargs",
})

# ── per-step 合法回报状态映射 ────────────────────
STEP_VALID_STATUSES = {
    "tdd":           ["DONE", "APPROVE", "BLOCKED", "NEEDS_FIX", "NEEDS_CONTEXT", "DONE_WITH_CONCERNS"],
    "spec-review":   ["DONE", "NEEDS_FIX", "BLOCKED", "NEEDS_CONTEXT", "DONE_WITH_CONCERNS"],
    "quality-review":["APPROVE", "DONE", "NEEDS_FIX", "BLOCKED", "NEEDS_CONTEXT"],
    "fix":           ["DONE", "BLOCKED", "NEEDS_CONTEXT"],
}

# ── 新增 step type 常量 ───────────────────────────────────
STEP_TYPE_SERIAL = "serial"
STEP_TYPE_PARALLEL = "parallel"
STEP_TYPE_INTERACTIVE = "interactive"
STEP_TYPE_LOOP = "loop"

# 新状态码（返回给主 Agent 的决策）
STATUS_CONTINUE_PARALLEL = "CONTINUE_PARALLEL"
STATUS_CONTINUE_LOOP = "CONTINUE_LOOP"
STATUS_BRANCHES_COMPLETE = "BRANCHES_COMPLETE"
STATUS_LOOP_COMPLETE = "LOOP_COMPLETE"

# Verification gate 状态码
STATUS_VERIFIED = "VERIFIED"
STATUS_VERIFICATION_FAILED = "VERIFICATION_FAILED"
STATUS_NO_CONTRACT = "NO_CONTRACT"

# goal 中是否包含这些关键词来推断步骤类型
STEP_TYPE_KEYWORDS = {
    "quality-review":["质量", "quality"],      # 先匹配（更精确）
    "spec-review":  ["spec", "合规", "规范", "评审"],
    "tdd":          ["tdd", "实现", "implement"],
    "fix":          ["fix", "修复", "根据 review"],
}


def _infer_step_type(goal: str) -> str:
    """从 goal 推断步骤类型（用于合法性校验）"""
    goal_lower = goal.lower()
    for stype, keywords in STEP_TYPE_KEYWORDS.items():
        if any(kw in goal_lower for kw in keywords):
            return stype
    return "tdd"  # 默认


def _get_step_type(step: dict) -> str:
    """判断 step 类型：serial / parallel / interactive / loop"""
    return step.get("type", STEP_TYPE_SERIAL)


def _build_parallel_result(step: dict, step_idx: int) -> dict:
    """为 parallel 步骤构建 CONTINUE_PARALLEL 响应。

    返回结构包含 branches 列表和 join_strategy，
    主 Agent 收到后需并行 delegate_task 所有 branches。
    """
    branches = step.get("branches", [])
    if not branches:
        return {"status": "ERROR", "diagnosis": f"step[{step_idx}] parallel 没有 branches"}

    branch_tasks = []
    for b in branches:
        task = {
            "agent": b["agent"],
            "goal": b["goal"],
            "context": b.get("context", ""),
        }
        if b.get("keywords"):
            task["keywords"] = b["keywords"]
        branch_tasks.append(task)

    return {
        "status": STATUS_CONTINUE_PARALLEL,
        "next": branch_tasks,
        "join_strategy": step.get("join_strategy", "separate"),
        "branch_count": len(branches),
        "context": {"chain_step": step_idx, "step_type": STEP_TYPE_PARALLEL},
    }


def _build_interactive_result(step: dict, step_idx: int) -> dict:
    """为 interactive 步骤构建 NEEDS_CONTEXT 响应（带 interactive 标记）。

    主 Agent 收到后应暂停链并询问用户确认。
    """
    result = {
        "status": "NEEDS_CONTEXT",
        "step_idx": step_idx,
        "agent": step["agent"],
        "goal": step["goal"],
        "question": f"[{step['agent']}] 已完成: {step['goal']}\n"
                    f"请确认结果，或补充修改意见后继续下一步。",
        "interactive": True,
    }
    if step.get("keywords"):
        result["keywords"] = step["keywords"]
    return result


def _build_loop_result(step: dict, step_idx: int, context: dict) -> dict:
    """为 loop 步骤构建 CONTINUE_LOOP 响应。

    从 context 取出 source 数据展开为多个 loop items，
    主 Agent 收到后需逐个执行并按 loop_complete 回传。
    """
    source = step.get("source", "previous_output")
    items = context.get(source, [])

    if not items:
        # 没有数据源 → 返回空循环（主 Agent 可跳过）
        return {
            "status": STATUS_CONTINUE_LOOP,
            "next": [],
            "loop_count": 0,
            "context": {"chain_step": step_idx, "step_type": STEP_TYPE_LOOP},
        }

    loop_items = []
    for i, item in enumerate(items):
        lo = {
            "agent": step["agent"],
            "goal": step["goal"],
            "loop_context": item,
            "loop_index": i,
        }
        if step.get("keywords"):
            lo["keywords"] = step["keywords"]
        loop_items.append(lo)

    return {
        "status": STATUS_CONTINUE_LOOP,
        "next": loop_items,
        "loop_count": len(loop_items),
        "context": {"chain_step": step_idx, "step_type": STEP_TYPE_LOOP},
    }


def _should_skip_step(step: dict, state: dict, step_idx: int) -> dict | None:
    """检查下一步是否应因 retry 计数达到 skip_threshold 而跳过。

    解析 step 中的 skip_threshold（int，默认 0=不跳过），
    从 state 中获取当前 step 的 retry 计数（spec_retry / quality_retry），
    若 retry 计数 ≥ skip_threshold，返回 SKIPPED 决策。

    参数:
        step: 下一步的 chain_def 元素
        state: 当前状态字典
        step_idx: 下一步的索引

    返回:
        SKIPPED 决策字典，或 None（不跳过）
    """
    skip_threshold = step.get("skip_threshold", 0)
    if skip_threshold <= 0:
        return None  # 不跳过

    spec_retry = state.get("spec_retry", 0)
    quality_retry = state.get("quality_retry", 0)
    max_retry = max(spec_retry, quality_retry)

    if max_retry >= skip_threshold:
        return {
            "status": "SKIPPED",
            "step_idx": step_idx,
            "diagnosis": f"达到 skip_threshold {skip_threshold} 次，跳过",
        }
    return None


def aggregate_parallel_results(branch_results: list[dict], join_strategy: str) -> dict:
    """聚合并行分支的结果。

    导出的公共函数，供主 Agent 在收集所有并行委托后调用。

    参数:
        branch_results: 每个元素为 {name, summary, findings, ...}
        join_strategy: "separate" — 不交叉排序，保留各轴完整性
                       "synthesize" — 对比合成

    返回:
        聚合后的结果 dict
    """
    if join_strategy == "separate":
        return {
            "status": "DONE",
            "output_type": "separate_report",
            "reports": {
                r.get("name", f"branch_{i}"): {
                    "content": r.get("summary", ""),
                    "findings": r.get("findings", []),
                }
                for i, r in enumerate(branch_results)
            },
            "summary": "并行结果已按轴独立输出，未做交叉排序",
        }
    elif join_strategy == "synthesize":
        return {
            "status": "DONE",
            "output_type": "synthesized_report",
            "individual_reports": {
                r.get("name", f"branch_{i}"): r.get("summary", "")
                for i, r in enumerate(branch_results)
            },
            "comparison": {
                "common_points": [],
                "divergences": [],
                "_warning": "synthesize 聚合策略为简化实现，仅保留各分支独立报告，未做自动对比合成。如需完整对比，请在主 Agent 层自行实现。",
            },
        }
    else:
        return {"status": "ERROR", "diagnosis": f"未知聚合策略: {join_strategy}"}


def _validate_skills(chain_def: list, chain_step_skills: dict, chain_owner: str) -> list:
    """验证所有 serial chain step 都有对应的 skills key。返回错误列表，空列表表示无错误。

    跳过 parallel / interactive / loop 类型步骤，它们不需要 skills key。
    """
    errors = []
    for i, step in enumerate(chain_def):
        step_type = _get_step_type(step)
        if step_type != STEP_TYPE_SERIAL:
            continue  # 非 serial 步骤无需 skills key
        key = f"{chain_owner}@{i}"
        if key not in chain_step_skills:
            errors.append(f"缺少 skills key: '{key}' (step {i}: {step['agent']} → {step['goal']})")
    return errors


def _sanitize_task_id(task_id: str) -> str:
    """净化 task_id，防止路径遍历攻击"""
    if not re.match(r'^[a-zA-Z0-9_.-]+$', task_id):
        raise ValueError(f"非法 task_id: {task_id}（仅允许字母、数字、下划线、点、连字符）")
    return task_id


def _state_path(task_id: str) -> str:
    """构建状态文件的完整路径。

    参数:
        task_id: 任务标识符（仅允许字母、数字、下划线、点、连字符）

    返回:
        状态文件的绝对路径

    异常:
        ValueError: task_id 包含非法字符时由 _sanitize_task_id 抛出
    """
    _sanitize_task_id(task_id)
    return os.path.join(STATE_DIR, task_id, "chain-state.json")


def _load_state(task_id: str) -> dict:
    """从磁盘加载指定 task 的状态数据。

    如果状态文件不存在，返回默认初始状态（current_step=0, retry 计数器归零, concerns=[]）。

    参数:
        task_id: 任务标识符

    返回:
        状态字典，包含 current_step、spec_retry、quality_retry、concerns、context 等字段

    异常:
        RuntimeError: 状态文件损坏（JSON 解析失败）或不可读时抛出
    """
    path = _state_path(task_id)
    if not os.path.exists(path):
        return {
            "current_step": 0,
            "spec_retry": 0,
            "quality_retry": 0,
            "concerns": [],
            "context": {},
        }
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"state 文件损坏或不可读: {path} — {e}。"
            f"如需恢复，请检查/删除此文件后重试。"
        )


def _save_state(task_id: str, state: dict):
    """将状态数据原子化写入磁盘。

    使用临时文件 + os.replace 确保写入原子性，避免断电/崩溃导致文件损坏。
    写入前会创建目标目录（如果不存在）。

    参数:
        task_id: 任务标识符
        state: 要持久化的状态字典

    异常:
        OSError: 目录创建或文件写入失败时抛出
    """
    path = _state_path(task_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _build_step_result(step: dict, chain_owner: str, step_idx: int,
                       chain_step_skills: dict, context: dict | None = None,
                       task_id: str = "") -> dict:
    """为单个 step 构建响应。支持 serial / parallel / interactive / loop 四种类型。

    如果 step 有 type 字段且不为 serial，委托给对应的专用构建函数。
    如果 step 有 batch: true，返回 CONTINUE_BATCH 数组。
    """
    step_type = _get_step_type(step)

    if step_type == STEP_TYPE_PARALLEL:
        return _build_parallel_result(step, step_idx)
    elif step_type == STEP_TYPE_INTERACTIVE:
        return _build_interactive_result(step, step_idx)
    elif step_type == STEP_TYPE_LOOP:
        return _build_loop_result(step, step_idx, context or {})

    # ── serial 原有逻辑 ──
    if step.get("batch"):
        # batch 模式：根据 batch 配置展开为多个子任务
        batch_count = step.get("batch_count", 3)  # 默认拆成 3 个
        batch_goal_template = step.get("batch_goal_template", "")
        goals = []
        for i in range(batch_count):
            if batch_goal_template:
                goal = batch_goal_template.replace("{index}", str(i + 1)).replace("{batch_index}", str(i))
            else:
                goal = f"{step['goal']} (Batch {i + 1}/{batch_count})"
            goals.append(goal)

        next_items = []
        step_keywords = step.get("keywords", [])
        for i, goal in enumerate(goals):
            item = {
                "agent": step["agent"],
                "goal": goal,
                "batch_index": i,
            }
            if step_keywords:
                item["keywords"] = step_keywords
            next_items.append(item)

        return {
            "status": "CONTINUE_BATCH",
            "next": next_items,
            "batch_count": batch_count,
            "context": {
                "chain_step": step_idx,
                "step_goal": step["goal"],
                "batch_results": [],
            },
        }
    else:
        key = f"{chain_owner}@{step_idx}"
        skills = chain_step_skills.get(key, [])

        # ── 写 Step Brief 文件 ──
        brief_dir = "/opt/data/.shared/briefs"
        os.makedirs(brief_dir, exist_ok=True)
        brief_path = os.path.join(brief_dir, f"{task_id}-step{step_idx}.md")

        step_goal = step.get("goal", "")
        step_keywords = step.get("keywords", [])
        step_contract = step.get("completion_contract", [])

        brief_lines = [
            f"## Step Brief — {task_id}",
            "",
            f"### Agent",
            step["agent"],
            "",
            f"### Goal",
            step_goal,
            "",
            f"### Skills",
            ", ".join(skills),
            "",
        ]
        if step_keywords:
            brief_lines += ["### Keywords", ", ".join(step_keywords), ""]
        if step_contract:
            brief_lines += ["### Completion Contract", json.dumps(step_contract, indent=2, ensure_ascii=False), ""]

        with open(brief_path, "w") as f:
            f.write("\n".join(brief_lines) + "\n")

        next_item = {
            "agent": step["agent"],
            "goal": f"读取 Brief 文件 {brief_path} 并执行",
            "skills": skills,
            "brief_file": brief_path,
        }
        if step_keywords:
            next_item["keywords"] = step_keywords
        return {
            "status": "CONTINUE",
            "next": next_item,
            "context": {"chain_step": step_idx, "step_goal": step_goal},
        }


def start_chain(task_id: str, chain_def: list, chain_step_skills: dict, chain_owner: str,
                report_only: bool = False, dry_run: bool = False):
    """
    start action：封装首次 advance 调用，自动构造 last_result={"status":"init"}。
    等价于 advance(task_id, chain_def, chain_step_skills, {"status":"init"}, chain_owner, report_only, dry_run=dry_run)。

    当 dry_run=True 时：不创建状态文件，直接解析第一步的合法 status 列表并返回。
    """
    return advance(task_id, chain_def, chain_step_skills, {"status": "init"}, chain_owner, report_only=report_only, dry_run=dry_run)


def run_chain(task_id: str, chain_agent: str, last_result: dict):
    """
    run action：从 index.yaml 读取 chain_def + chain_step_skills，然后调用 advance。
    """
    index_data = load_index()
    if index_data is None:
        return {"status": "ERROR", "diagnosis": f"未找到 index.yaml: {INDEX_YAML_PATH}"}

    agents = index_data.get("agents", {})
    agent_config = agents.get(chain_agent)
    if not agent_config:
        return {"status": "ERROR", "diagnosis": f"index.yaml 中未找到 agent: {chain_agent}"}

    chain_def = agent_config.get("chain")

    chain_step_skills = agent_config.get("chain_step_skills", {})
    report_only = False

    # 如果内联 chain 不存在，尝试从 chain_ref 加载
    if not chain_def:
        chain_ref = agent_config.get("chain_ref")
        if chain_ref:
            chain_data = load_chain(chain_ref)
            if chain_data is None:
                chain_file = os.path.join(SCRIPT_DIR, "..", "route-map", "chains", f"{chain_ref}.yaml")
                return {"status": "ERROR", "diagnosis": f"chain_ref 文件不存在: {chain_file}"}
            chain_def = chain_data.get("steps", [])
            chain_step_skills = chain_data.get("chain_step_skills", {})
            report_only = chain_data.get("report_only", False)

    if not chain_def:
        return {"status": "ERROR", "diagnosis": f"agent '{chain_agent}' 未定义 chain 或 chain_ref"}

    chain_owner = chain_agent

    return advance(task_id, chain_def, chain_step_skills, last_result, chain_owner, report_only=report_only)


def run_verification(step: dict) -> dict:
    """执行 step 的 completion_contract 验证。

    检查 step 中是否有 completion_contract 字段，如果有则逐个执行 verify_command。

    返回:
        {status: "VERIFIED"|"FAILED"|"NO_CONTRACT", results: [...]}
    """
    contract = step.get("completion_contract")
    if not contract:
        return {"status": STATUS_NO_CONTRACT, "results": []}

    results = []
    all_passed = True

    for item in contract:
        cmd = item.get("verify_command", "")
        cmd_type = item.get("type", "unknown")
        description = item.get("description", "")

        if not cmd:
            results.append({
                "type": cmd_type,
                "description": description,
                "exit_code": -1,
                "passed": False,
                "error": "verify_command 为空",
            })
            all_passed = False
            continue

        try:
            # 判断命令是否需要 shell 特性（管道、重定向等）
            needs_shell = any(op in cmd for op in ("|", ">", "<", ";&"))
            if needs_shell:
                # 管道命令: 按管道符分段，每段只检查第一个命令名
                allowed = True
                blocked_cmd = ""
                segments = cmd.split("|")
                for seg in segments:
                    seg = seg.strip()
                    if not seg:
                        continue
                    first_token = seg.split()[0]
                    base_cmd = first_token.split("/")[-1]
                    if base_cmd not in VERIFY_COMMAND_BASENAMES:
                        allowed = False
                        blocked_cmd = base_cmd
                        break
                if not allowed:
                    results.append({
                        "type": cmd_type,
                        "description": description,
                        "exit_code": -1,
                        "passed": False,
                        "error": f"命令 '{blocked_cmd}' 不在白名单 {sorted(VERIFY_COMMAND_BASENAMES)}",
                    })
                    all_passed = False
                    continue
                cp = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    timeout=VERIFICATION_TIMEOUT,
                    text=True,
                )
            else:
                # 简单命令: shlex.split + shell=False
                cmd_parts = shlex.split(cmd)
                cp = subprocess.run(
                    cmd_parts,
                    shell=False,
                    capture_output=True,
                    timeout=VERIFICATION_TIMEOUT,
                    text=True,
                )
            passed = cp.returncode == 0
            results.append({
                "type": cmd_type,
                "description": description,
                "exit_code": cp.returncode,
                "passed": passed,
                "stdout": cp.stdout[:MAX_OUTPUT_LENGTH] if cp.stdout else "",
                "stderr": cp.stderr[:MAX_OUTPUT_LENGTH] if cp.stderr else "",
            })
            if not passed:
                all_passed = False
        except subprocess.TimeoutExpired:
            results.append({
                "type": cmd_type,
                "description": description,
                "exit_code": -1,
                "passed": False,
                "error": f"Command timed out after {VERIFICATION_TIMEOUT}s",
            })
            all_passed = False
        except Exception as e:
            results.append({
                "type": cmd_type,
                "description": description,
                "exit_code": -1,
                "passed": False,
                "error": str(e),
            })
            all_passed = False

    status = STATUS_VERIFIED if all_passed else STATUS_VERIFICATION_FAILED
    return {"status": status, "results": results}


# ── advance() 辅助函数（S08 提取） ──────────────────


def _handle_blocked(step_idx: int, agent: str, step_goal: str,
                    last_result: dict) -> dict:
    """处理 BLOCKED 状态：挂起当前步骤并返回阻塞诊断。

    参数:
        step_idx: 当前步骤索引
        agent: 当前 agent 名称
        step_goal: 当前步骤目标
        last_result: 上一步返回的结果（含 message 诊断信息）

    返回:
        包含 BLOCKED 状态码的决策字典
    """
    return {
        "status": "BLOCKED",
        "step_idx": step_idx,
        "agent": agent,
        "goal": step_goal,
        "diagnosis": last_result.get("message", "阻塞，无诊断信息"),
    }


def _handle_needs_fix(task_id: str, state: dict, step_idx: int,
                      agent: str, step_goal: str, step_type: str,
                      last_result: dict) -> dict:
    """处理 NEEDS_FIX 状态：根据 review 类型增加 retry 计数并返回 RETRY 指令。

    支持 spec-review 和 quality-review 两种 retry 类型。
    retry 次数达到 MAX_RETRY 后返回 BLOCKED 而非 RETRY。

    参数:
        task_id: 任务标识符
        state: 当前状态字典（会在函数内修改并持久化）
        step_idx: 当前步骤索引
        agent: 当前 agent 名称
        step_goal: 当前步骤目标
        step_type: _infer_step_type() 推断的步骤类型
        last_result: 上一步返回的结果

    返回:
        RETRY（重试修复）或 BLOCKED（达到上限挂起）的决策字典
    """
    if step_type == "spec-review":
        state["spec_retry"] += 1
        retry = state["spec_retry"]
        if retry >= MAX_RETRY:
            return {
                "status": "BLOCKED",
                "step_idx": step_idx,
                "agent": agent,
                "goal": step_goal,
                "diagnosis": f"spec 修复已达上限 {MAX_RETRY} 次，挂起",
                "spec_retry_count": retry,
            }
        _save_state(task_id, state)
        return {
            "status": "RETRY",
            "next": {
                "agent": "programmer",
                "goal": f"fix: 根据 spec review 修复 ({retry}/{MAX_RETRY})",
                "skills": ["test-driven-development", "requesting-code-review"],
            },
            "context": {
                "review_findings": last_result.get("findings", ""),
                "original_diff_path": state["context"].get("diff_path", ""),
                "retry_type": "spec",
                "retry_count": retry,
                "target_step_idx": step_idx,
            },
        }
    elif step_type == "quality-review":
        state["quality_retry"] += 1
        retry = state["quality_retry"]
        if retry >= MAX_RETRY:
            return {
                "status": "BLOCKED",
                "step_idx": step_idx,
                "agent": agent,
                "goal": step_goal,
                "diagnosis": f"质量修复已达上限 {MAX_RETRY} 次，挂起",
                "quality_retry_count": retry,
            }
        _save_state(task_id, state)
        return {
            "status": "RETRY",
            "next": {
                "agent": "programmer",
                "goal": f"fix: 根据 quality review 修复 ({retry}/{MAX_RETRY})",
                "skills": ["test-driven-development", "requesting-code-review"],
            },
            "context": {
                "review_findings": last_result.get("findings", ""),
                "original_diff_path": state["context"].get("diff_path", ""),
                "retry_type": "quality",
                "retry_count": retry,
                "target_step_idx": step_idx,
            },
        }
    else:
        return {"status": "ERROR", "diagnosis": f"step[{step_idx}] '{step_goal}' 无法判断 retry 类型"}


def _build_chain_done_result(state: dict, chain_def_length: int) -> dict:
    """构建链完成的返回结果。

    在各分支（batch_complete / branches_complete / loop_complete / DONE）中，
    当 current_step 超过 chain_def 长度时调用此函数。

    参数:
        state: 当前状态字典（含 report_only 标志、context、concerns 等）
        chain_def_length: chain_def 总步数

    返回:
        DONE 或 REPORT_ONLY 的决策字典
    """
    return {
        "status": "REPORT_ONLY" if state.get("report_only") else "DONE",
        "final_output_path": state["context"].get("last_output", ""),
        "concerns": state["concerns"],
        "summary": {
            "total_steps": chain_def_length,
            "spec_retry_count": state["spec_retry"],
            "quality_retry_count": state["quality_retry"],
            "concerns_count": len(state["concerns"]),
        },
    }


def _accumulate_partial_result(task_id: str, state: dict,
                               last_result: dict, agent: str,
                               status: str) -> dict:
    """累加并行分支或 batch 分片的中间结果。

    处理 branch_index 和 batch_index 两种 partial 回报。
    将结果存入 state.context.branch_results 或 state.context.batch_results 数组。
    返回 BRANCH_PROGRESS 或 BATCH_PROGRESS 以指示主 Agent 等待更多结果。

    参数:
        task_id: 任务标识符
        state: 当前状态字典（会在函数内修改并持久化）
        last_result: 上一步返回的结果（含 branch_index 或 batch_index）
        agent: 当前 agent 名称
        status: 当前状态

    返回:
        BRANCH_PROGRESS 或 BATCH_PROGRESS 的决策字典
    """
    if last_result.get("branch_index") is not None:
        branch_index = last_result["branch_index"]
        if "branch_results" not in state["context"]:
            state["context"]["branch_results"] = []
        while len(state["context"]["branch_results"]) <= branch_index:
            state["context"]["branch_results"].append(None)
        state["context"]["branch_results"][branch_index] = {
            "agent": agent,
            "status": status,
            "output_path": last_result.get("output_path", ""),
            "message": last_result.get("message", ""),
            "findings": last_result.get("findings", ""),
            "summary": last_result.get("summary", ""),
        }
        _save_state(task_id, state)
        return {
            "status": "BRANCH_PROGRESS",
            "branch_index": branch_index,
            "branch_count": len(state["context"]["branch_results"]),
            "context": state.get("context", {}),
            "message": f"Branch {branch_index + 1} completed. "
                       f"Awaiting remaining or branches_complete.",
        }

    if last_result.get("batch_index") is not None:
        batch_index = last_result["batch_index"]
        if "batch_results" not in state["context"]:
            state["context"]["batch_results"] = []
        while len(state["context"]["batch_results"]) <= batch_index:
            state["context"]["batch_results"].append(None)
        state["context"]["batch_results"][batch_index] = {
            "agent": agent,
            "status": status,
            "output_path": last_result.get("output_path", ""),
            "message": last_result.get("message", ""),
            "findings": last_result.get("findings", ""),
            "summary": last_result.get("summary", ""),
        }
        _save_state(task_id, state)
        return {
            "status": "BATCH_PROGRESS",
            "batch_index": batch_index,
            "batch_count": len(state["context"]["batch_results"]),
            "context": state.get("context", {}),
            "message": f"Batch item {batch_index + 1} completed. "
                       f"Awaiting remaining or batch_complete.",
        }

    return {"status": "ERROR", "diagnosis": "partial result 缺少 branch_index 或 batch_index"}


def _handle_batch_complete(task_id: str, state: dict, chain_def: list,
                           chain_owner: str, chain_step_skills: dict,
                           last_result: dict, step_idx: int) -> dict:
    """处理 batch_complete：保存 batch 结果，执行 verification，
    推进到下一步或结束链。

    参数:
        task_id: 任务标识符
        state: 当前状态字典
        chain_def: 链定义列表
        chain_owner: 链所属 Agent
        chain_step_skills: step skills 字典
        last_result: 上一步返回的结果（含 batch_results）
        step_idx: 当前步骤索引

    返回:
        下一步决策字典（CONTINUE / DONE / VERIFICATION_FAILED 等）
    """
    batch_results = last_result.get("batch_results", [])
    if batch_results:
        state["context"]["batch_results"] = batch_results
    current_step = chain_def[step_idx]
    verification_result = run_verification(current_step)
    if verification_result["status"] == STATUS_VERIFICATION_FAILED:
        _save_state(task_id, state)
        return {
            "status": STATUS_VERIFICATION_FAILED,
            "step_idx": step_idx,
            "agent": chain_def[step_idx].get("agent", ""),
            "goal": chain_def[step_idx].get("goal", ""),
            "verification_results": verification_result["results"],
            "diagnosis": f"Batch step {step_idx} verification failed",
        }
    _save_state(task_id, state)
    state["current_step"] += 1
    if state["current_step"] >= len(chain_def):
        _save_state(task_id, state)
        return _build_chain_done_result(state, chain_def_length=len(chain_def))
    _save_state(task_id, state)
    step = chain_def[state["current_step"]]
    return _build_step_result(step, chain_owner, state["current_step"],
                               chain_step_skills, state.get("context", {}),
                               task_id=task_id)


def _handle_branch_complete(task_id: str, state: dict, chain_def: list,
                            chain_owner: str, chain_step_skills: dict,
                            last_result: dict, step_idx: int) -> dict:
    """处理 branches_complete：保存并行分支结果，执行 verification，
    推进到下一步或结束链。

    参数:
        task_id: 任务标识符
        state: 当前状态字典
        chain_def: 链定义列表
        chain_owner: 链所属 Agent
        chain_step_skills: step skills 字典
        last_result: 上一步返回的结果（含 branch_results）
        step_idx: 当前步骤索引

    返回:
        下一步决策字典
    """
    branch_results = last_result.get("branch_results", [])
    if branch_results:
        state["context"]["branch_results"] = branch_results
    current_step = chain_def[step_idx]
    verification_result = run_verification(current_step)
    if verification_result["status"] == STATUS_VERIFICATION_FAILED:
        _save_state(task_id, state)
        return {
            "status": STATUS_VERIFICATION_FAILED,
            "step_idx": step_idx,
            "agent": chain_def[step_idx].get("agent", ""),
            "goal": chain_def[step_idx].get("goal", ""),
            "verification_results": verification_result["results"],
            "diagnosis": f"Branches step {step_idx} verification failed",
        }
    _save_state(task_id, state)
    state["current_step"] += 1
    if state["current_step"] >= len(chain_def):
        _save_state(task_id, state)
        return _build_chain_done_result(state, chain_def_length=len(chain_def))
    _save_state(task_id, state)
    step = chain_def[state["current_step"]]
    return _build_step_result(step, chain_owner, state["current_step"],
                               chain_step_skills, state.get("context", {}),
                               task_id=task_id)


def _handle_loop_complete(task_id: str, state: dict, chain_def: list,
                          chain_owner: str, chain_step_skills: dict,
                          last_result: dict, step_idx: int) -> dict:
    """处理 loop_complete：保存循环结果，推进到下一步或结束链。

    参数:
        task_id: 任务标识符
        state: 当前状态字典
        chain_def: 链定义列表
        chain_owner: 链所属 Agent
        chain_step_skills: step skills 字典
        last_result: 上一步返回的结果（含 loop_results）
        step_idx: 当前步骤索引

    返回:
        下一步决策字典
    """
    loop_results = last_result.get("loop_results", [])
    if loop_results:
        state["context"]["loop_results"] = loop_results
    _save_state(task_id, state)
    state["current_step"] += 1
    if state["current_step"] >= len(chain_def):
        _save_state(task_id, state)
        return _build_chain_done_result(state, chain_def_length=len(chain_def))
    _save_state(task_id, state)
    step = chain_def[state["current_step"]]
    return _build_step_result(step, chain_owner, state["current_step"],
                               chain_step_skills, state.get("context", {}),
                               task_id=task_id)


def _handle_serial_step(step: dict, chain_owner: str, step_idx: int,
                        chain_step_skills: dict,
                        context: dict | None = None,
                        task_id: str = "") -> dict:
    """处理 serial 类型的步骤构建（委托给 _build_step_result）。

    参数:
        step: 步骤定义字典
        chain_owner: 链所属 Agent
        step_idx: 步骤索引
        chain_step_skills: step skills 字典
        context: 当前上下文

    返回:
        _build_step_result 的返回结果（CONTINUE 等）
    """
    return _build_step_result(step, chain_owner, step_idx,
                              chain_step_skills, context,
                              task_id=task_id)


def _handle_parallel_step(step: dict, chain_owner: str, step_idx: int,
                          chain_step_skills: dict,
                          context: dict | None = None,
                          task_id: str = "") -> dict:
    """处理 parallel 类型的步骤构建（委托给 _build_step_result）。

    参数:
        step: 步骤定义字典
        chain_owner: 链所属 Agent
        step_idx: 步骤索引
        chain_step_skills: step skills 字典
        context: 当前上下文

    返回:
        _build_step_result 的返回结果（CONTINUE_PARALLEL 等）
    """
    return _build_step_result(step, chain_owner, step_idx,
                              chain_step_skills, context,
                              task_id=task_id)


def advance(task_id: str, chain_def: list, chain_step_skills: dict,
            last_result: dict, chain_owner: str = "", report_only: bool = False,
            dry_run: bool = False):
    """
    last_result: 从上一步委托返回的结果 dict
      必有: agent, status
      可选: output_path, findings, message
    1) 首个调用: last_result={"status":"init"}
    2) batch 场景: last_result 可含 batch_index 或 batch_complete
    chain_owner: 链所属的 Agent（用于构建 skills key: {owner}@{idx}）
    report_only: 链完成后返回 REPORT_ONLY 状态码而非 DONE
    dry_run: 为 True 时不加载状态/不修改状态，直接根据 chain_def
             解析当前 step 的合法 status 列表并返回
    """

    # ── dry-run 模式：不加载/不修改状态，直接返回合法 status 列表 ──
    if dry_run:
        dr_step_idx = 0  # 默认 step 0
        if last_result.get("status") == "init":
            dr_step_idx = 0
            if not chain_def:
                return {"status": "ERROR", "diagnosis": "chain_def 为空数组，无法启动链"}
        else:
            # 非 init 调用：尝试从 last_result 推断 step_idx
            dr_step_idx = last_result.get("step_idx", 0)
            # 如果 last_result 有 target_step_idx（fix 场景），用那个
            dr_step_idx = last_result.get("target_step_idx", dr_step_idx)

        if dr_step_idx >= len(chain_def):
            return {"status": "ERROR", "diagnosis": f"step_idx {dr_step_idx} 超出 chain_def 范围"}
        dr_step = chain_def[dr_step_idx]
        dr_goal = dr_step.get("goal", "")
        dr_type = _infer_step_type(dr_goal) if dr_goal else "tdd"
        dr_valid = STEP_VALID_STATUSES.get(dr_type, ["DONE", "BLOCKED", "NEEDS_FIX", "NEEDS_CONTEXT"])
        return {
            "agent": dr_step.get("agent", ""),
            "valid_statuses": dr_valid,
            "step_idx": dr_step_idx,
        }

    # ── 首次调用 ──
    if last_result.get("status") == "init":
        if not chain_owner:
            return {"status": "ERROR", "diagnosis": "首次调用必须指定 --chain_owner"}
        if not chain_def:
            return {"status": "ERROR", "diagnosis": "chain_def 为空数组，无法启动链"}
        skill_errors = _validate_skills(chain_def, chain_step_skills, chain_owner)
        if skill_errors:
            return {
                "status": "ERROR",
                "diagnosis": "; ".join(skill_errors),
                "defined_keys": list(chain_step_skills.keys()),
            }
        try:
            state = _load_state(task_id)
        except RuntimeError as e:
            return {"status": "ERROR", "diagnosis": str(e)}
        # 重置状态（避免跨 session 污染）
        state.update({
            "current_step": 0, "spec_retry": 0, "quality_retry": 0,
            "concerns": [], "context": {}, "chain_owner": chain_owner,
            "report_only": report_only,
        })
        _save_state(task_id, state)

        step = chain_def[0]
        return _build_step_result(step, chain_owner, 0, chain_step_skills, state.get("context", {}), task_id=task_id)

    try:
        state = _load_state(task_id)
    except RuntimeError as e:
        return {"status": "ERROR", "diagnosis": str(e)}
    # 使用 state 中存储的 chain_owner（首次调用时已保存）
    chain_owner = state.get("chain_owner", chain_owner)
    agent = last_result.get("agent", "")
    status = last_result.get("status", "")
    if "current_step" not in state:
        return {"status": "ERROR", "diagnosis": f"state 文件缺少 current_step，可能已损坏（task_id={task_id}）"}
    step_idx = state["current_step"]
    if step_idx >= len(chain_def):
        return {"status": "ERROR", "diagnosis": f"state.current_step ({step_idx}) >= chain_def 长度 ({len(chain_def)})，状态与链定义不匹配"}

    # ── batch_complete ──
    if last_result.get("batch_complete"):
        return _handle_batch_complete(task_id, state, chain_def, chain_owner,
                                      chain_step_skills, last_result, step_idx)

    # ── branches_complete ──
    if last_result.get("branches_complete"):
        return _handle_branch_complete(task_id, state, chain_def, chain_owner,
                                       chain_step_skills, last_result, step_idx)

    # ── loop_complete ──
    if last_result.get("loop_complete"):
        return _handle_loop_complete(task_id, state, chain_def, chain_owner,
                                     chain_step_skills, last_result, step_idx)

    # ── branch_index / batch_index — 累加 partial 结果 ──
    if last_result.get("branch_index") is not None or last_result.get("batch_index") is not None:
        return _accumulate_partial_result(task_id, state, last_result, agent, status)

    # ── 校验状态合法性 ──
    step_goal = chain_def[step_idx].get("goal", "")
    if not step_goal:
        return {"status": "ERROR", "diagnosis": f"step[{step_idx}] 缺少 goal 字段（可能 parallel/interactive/loop 步骤未经过 completion handler）"}
    step_type = _infer_step_type(step_goal)
    # 空 status → 首次调用，返回当前步骤
    if not status:
        step = chain_def[step_idx]
        return _build_step_result(step, chain_owner, step_idx, chain_step_skills, state.get("context", {}), task_id=task_id)
    valid = STEP_VALID_STATUSES.get(step_type, ["DONE", "BLOCKED", "NEEDS_FIX", "NEEDS_CONTEXT"])
    if status not in valid:
        return {
            "status": "ERROR",
            "diagnosis": f"step[{step_idx}] '{step_goal}' (agent={agent}) 返回非法状态 '{status}'。合法状态: {valid}",
        }

    # ── BLOCKED — 挂起 ──
    if status == "BLOCKED":
        return _handle_blocked(step_idx, agent, step_goal, last_result)

    # ── NEEDS_CONTEXT — 等待用户 ──
    if status == "NEEDS_CONTEXT":
        return {
            "status": "NEEDS_CONTEXT",
            "step_idx": step_idx,
            "agent": agent,
            "goal": step_goal,
            "question": last_result.get("message", "缺少上下文，请补充"),
        }

    # ── DONE_WITH_CONCERNS — 标记，继续 ──
    if status == "DONE_WITH_CONCERNS":
        concern = last_result.get("message", f"step[{step_idx}] 有未解决的担忧")
        state["concerns"].append(concern)
        _save_state(task_id, state)
        status = "DONE"  # 降级为 DONE

    # ── NEEDS_FIX — review 不通过 ──
    if status == "NEEDS_FIX":
        step_type = _infer_step_type(step_goal)
        return _handle_needs_fix(task_id, state, step_idx, agent,
                                 step_goal, step_type, last_result)

    # ── DONE / APPROVE — 推进或回归 ──
    if status in ("DONE", "APPROVE"):
        # 保存产出路径到上下文
        if last_result.get("output_path"):
            state["context"]["diff_path"] = last_result["output_path"]
        state["context"]["last_output"] = last_result.get("output_path", "")

        # 防御检查：RETRY 上下文中缺少 target_step_idx
        if last_result.get("target_step_idx") is None and state["context"].get("retry_type"):
            return {
                "status": "ERROR",
                "diagnosis": f"fix 步骤 DONE 但缺少 target_step_idx（retry_type={state['context'].get('retry_type')}）。主 Agent 应在 fix 完成后回传 target_step_idx 以回到评审步骤。",
            }

        # ── Verification Gate：检查当前 step 的 completion_contract ──
        current_step = chain_def[step_idx]
        verification_result = run_verification(current_step)
        if verification_result["status"] == STATUS_VERIFICATION_FAILED:
            _save_state(task_id, state)
            return {
                "status": STATUS_VERIFICATION_FAILED,
                "step_idx": step_idx,
                "agent": agent,
                "goal": step_goal,
                "verification_results": verification_result["results"],
                "diagnosis": f"Step {step_idx} ('{step_goal}') verification failed",
            }

        # fix 循环后回到 review step
        if last_result.get("target_step_idx") is not None:
            state["current_step"] = last_result["target_step_idx"]
            _save_state(task_id, state)
            step = chain_def[state["current_step"]]
            return _build_step_result(step, chain_owner, state["current_step"], chain_step_skills, state.get("context", {}), task_id=task_id)

        # ── Skip Threshold 检查：下一步是否应跳过 ──
        next_step_idx = step_idx + 1
        if next_step_idx < len(chain_def):
            next_step = chain_def[next_step_idx]
            skip_result = _should_skip_step(next_step, state, next_step_idx)
            if skip_result is not None:
                # 跳过下一步：将 state 的 current_step 推进到跳过步骤之后，
                # 主 Agent 收到 SKIPPED 后再次调用 advance 会从下一步继续。
                state["current_step"] = next_step_idx + 1
                _save_state(task_id, state)
                if state["current_step"] >= len(chain_def):
                    return _build_chain_done_result(state, chain_def_length=len(chain_def))
                return skip_result

        # 正常推进到下一步
        state["current_step"] += 1
        if state["current_step"] >= len(chain_def):
            _save_state(task_id, state)
            return _build_chain_done_result(state, chain_def_length=len(chain_def))

        _save_state(task_id, state)
        step = chain_def[state["current_step"]]
        return _build_step_result(step, chain_owner, state["current_step"], chain_step_skills, state.get("context", {}), task_id=task_id)

    return {"status": "ERROR", "diagnosis": f"未处理的状态: {status}"}


def main():
    """CLI 入口函数。

    支持 action 参数: advance, start, run, verify。
    所有结果以 JSON 格式输出到 stdout（exit code 0）或 stderr（exit code 1）。

    advance: 推进链状态机一步。需要 --chain_def, --chain_step_skills, --last_result。
    start:   启动新链。需要 --chain_def, --chain_step_skills, --chain_owner。
    run:     从 index.yaml 加载 chain 并推进。需要 --chain_agent, 可选 --last_result。
    verify:  执行指定 step 的 verification contract。需要 --chain_def, --step_idx。

    异常:
        SystemExit(1): 参数缺失、JSON 解析失败、task_id 非法或 step_idx 越界时退出
    """
    parser = argparse.ArgumentParser(description="Chain 编排引擎")
    parser.add_argument("action", choices=["advance", "start", "run", "verify"])
    parser.add_argument("--task_id", required=True)
    parser.add_argument("--step_idx", default=None, type=int,
                        help="step 索引（verify action 必填）")
    parser.add_argument("--chain_def", default=None,
                        help="JSON: [{agent, goal}, ...] (advance/start 必填)")
    parser.add_argument("--chain_step_skills", default=None,
                        help="JSON: {owner@idx: [skill, ...]} (advance/start 必填)")
    parser.add_argument("--last_result", default=None,
                        help="JSON: {agent, status, ...} (advance/run 必填, start 自动设为 init)")
    parser.add_argument("--chain_owner", default="",
                        help="链所属 Agent（首次调用必填）")
    parser.add_argument("--chain_agent", default="",
                        help="从 index.yaml 读取 chain 的 Agent 名 (run action 必填)")
    parser.add_argument("--report_only", action="store_true", default=False,
                        help="链完成后返回 REPORT_ONLY 状态码而非 DONE")
    parser.add_argument("--dry-run", action="store_true", default=False, dest="dry_run",
                        help="dry-run 模式：不加载/不修改状态，直接返回当前 step 的合法 status 列表")
    args = parser.parse_args()

    # 在 main 入口净化 task_id
    try:
        _sanitize_task_id(args.task_id)
    except ValueError as e:
        print(json.dumps({"status": "ERROR", "diagnosis": str(e)}, ensure_ascii=False))
        sys.exit(1)

    if args.action == "start":
        # start: --chain_def, --chain_step_skills, --chain_owner 必填；不填 --last_result
        if not args.chain_def:
            print(json.dumps({"status": "ERROR", "diagnosis": "start action 需要 --chain_def"}, ensure_ascii=False))
            sys.exit(1)
        if not args.chain_step_skills:
            print(json.dumps({"status": "ERROR", "diagnosis": "start action 需要 --chain_step_skills"}, ensure_ascii=False))
            sys.exit(1)
        if not args.chain_owner:
            print(json.dumps({"status": "ERROR", "diagnosis": "start action 需要 --chain_owner"}, ensure_ascii=False))
            sys.exit(1)

        try:
            chain_def = json.loads(args.chain_def)
            chain_step_skills = json.loads(args.chain_step_skills)
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "ERROR", "diagnosis": f"JSON 解析失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
        result = start_chain(args.task_id, chain_def, chain_step_skills, args.chain_owner,
                             report_only=args.report_only, dry_run=args.dry_run)

    elif args.action == "run":
        # run: --chain_agent 必填；可选 --last_result（默认 init）
        if not args.chain_agent:
            print(json.dumps({"status": "ERROR", "diagnosis": "run action 需要 --chain_agent"}, ensure_ascii=False))
            sys.exit(1)

        try:
            last_result = json.loads(args.last_result) if args.last_result else {"status": "init"}
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "ERROR", "diagnosis": f"last_result JSON 解析失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
        result = run_chain(args.task_id, args.chain_agent, last_result)

    elif args.action == "verify":
        # verify: --chain_def, --step_idx 必填
        if not args.chain_def:
            print(json.dumps({"status": "ERROR", "diagnosis": "verify action 需要 --chain_def"}, ensure_ascii=False))
            sys.exit(1)
        if args.step_idx is None:
            print(json.dumps({"status": "ERROR", "diagnosis": "verify action 需要 --step_idx"}, ensure_ascii=False))
            sys.exit(1)

        try:
            chain_def = json.loads(args.chain_def)
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "ERROR", "diagnosis": f"chain_def JSON 解析失败: {e}"}, ensure_ascii=False))
            sys.exit(1)

        if args.step_idx < 0 or args.step_idx >= len(chain_def):
            print(json.dumps({
                "status": "ERROR",
                "diagnosis": f"step_idx {args.step_idx} 超出 chain_def 范围 (0..{len(chain_def) - 1})",
            }, ensure_ascii=False))
            sys.exit(1)

        step = chain_def[args.step_idx]
        result = run_verification(step)

    else:
        # advance (原有行为，保持向后兼容)
        if not args.chain_def or not args.chain_step_skills or not args.last_result:
            print(json.dumps({
                "status": "ERROR",
                "diagnosis": "advance action 需要 --chain_def, --chain_step_skills, --last_result 三个参数",
            }, ensure_ascii=False))
            sys.exit(1)

        try:
            chain_def = json.loads(args.chain_def)
            chain_step_skills = json.loads(args.chain_step_skills)
            last_result = json.loads(args.last_result)
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "ERROR", "diagnosis": f"JSON 解析失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
        result = advance(args.task_id, chain_def, chain_step_skills, last_result,
                         chain_owner=args.chain_owner, report_only=args.report_only,
                         dry_run=args.dry_run)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
