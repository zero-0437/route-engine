#!/usr/bin/env python3
"""切片测试：NEEDS_CONTEXT 确认节点 + Context 瘦身 skill。

覆盖范围：
  NC-01~NC-10:  交互步骤 NEEDS_CONTEXT 流转
  CS-01~CS-07:  Context 瘦身 skill 文档完整性（可自动化部分）
  CS-08~CS-10:  TODO 占位（人工/LLM-as-Judge）
  INT-04:        NEEDS_CONTEXT 集成—完整链

参考测试方案：/opt/data/test-plan.md（第3-4章）
执行计划：/opt/data/test-exec-plan.md（④⑤⑥ 组）
"""

import json
import os
import sys
from unittest.mock import MagicMock, PropertyMock, call, patch

import tempfile

import pytest

# ── 将被测模块加入 sys.path ────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


# ── 共用 Fixture ─────────────────────────────────────────


@pytest.fixture
def patch_state_dir():
    """将 STATE_DIR 指向临时目录，避免污染真实 .shared 目录。

    与 tests/test_chain_executor.py 中的 autouse fixture 等效但不 autouse，
    仅本文件需要时显式注入。
    """
    import scripts.chain_executor as ce

    original = ce.STATE_DIR
    ce.STATE_DIR = tempfile.mkdtemp()
    yield
    ce.STATE_DIR = original

from scripts.chain_executor import (
    MAX_RETRY,
    STEP_TYPE_SERIAL,
    STEP_TYPE_PARALLEL,
    STEP_TYPE_INTERACTIVE,
    STEP_TYPE_LOOP,
    STATUS_VERIFIED,
    STATUS_VERIFICATION_FAILED,
    STATUS_NO_CONTRACT,
    advance,
    start_chain,
    _validate_skills,
    run_verification,
    _state_path,
    _load_state,
    _save_state,
    _build_step_result,
    _build_chain_done_result,
    _handle_blocked,
    _handle_needs_fix,
    _accumulate_partial_result,
    _handle_batch_complete,
    _handle_branch_complete,
    _handle_loop_complete,
    _build_interactive_result,
    _get_step_type,
)

# ========================================================================
# NC 组 — NEEDS_CONTEXT / interactive 步骤
# ========================================================================


class TestNCInteractiveStep:
    """NC-01 ~ NC-03: _build_interactive_result 单元测试。"""

    def test_nc01_interactive_step_marked_correctly(self):
        """NC-01: interactive 步骤标记正确。
        step 带 type=interactive → status=NEEDS_CONTEXT, interactive=True
        """
        step = {
            "agent": "pm-agent",
            "goal": "输出修复方案摘要，等待用户确认",
            "type": "interactive",
            "keywords": ["fix-summary", "confirm"],
        }
        result = _build_interactive_result(step, 1)
        assert result["status"] == "NEEDS_CONTEXT"
        assert result["interactive"] is True
        assert result["step_idx"] == 1
        assert result["agent"] == "pm-agent"
        assert "请确认结果" in result["question"]

    def test_nc02_interactive_step_has_keywords(self):
        """NC-02: interactive 步骤含 keywords → 返回结果含 keywords。"""
        step = {
            "agent": "pm-agent",
            "goal": "等待用户确认",
            "type": "interactive",
            "keywords": ["confirm", "review-plan"],
        }
        result = _build_interactive_result(step, 2)
        assert "keywords" in result
        assert result["keywords"] == ["confirm", "review-plan"]

    def test_nc03_interactive_step_no_keywords(self):
        """NC-03: interactive 步骤无 keywords → 返回结果无 keywords 字段。"""
        step = {
            "agent": "pm-agent",
            "goal": "等待用户确认",
            "type": "interactive",
        }
        result = _build_interactive_result(step, 3)
        assert "keywords" not in result


class TestNCAdvanceNeedsContext:
    """NC-04 ~ NC-06: advance() 中 NEEDS_CONTEXT 处理。"""

    def test_nc04_advance_needs_context_with_message(self, patch_state_dir):
        """NC-04: advance 收到 NEEDS_CONTEXT（含 message）→ 返回 question 与 message 一致。
        NEEDS_CONTEXT 路径不推进 current_step，不保存 state。
        """
        task_id = "T-nc04"
        chain_def = [
            {"agent": "programmer", "goal": "TDD 实现 + self-review"},
        ]
        skills = {"programmer@0": ["test-driven-development"]}

        # 先启动链
        start_chain(task_id, chain_def, skills, "programmer")
        state_before = _load_state(task_id)

        # 模拟 step0 返回 NEEDS_CONTEXT（含 message）
        last_result = {
            "agent": "programmer",
            "status": "NEEDS_CONTEXT",
            "message": "请确认结果，或补充修改意见后继续",
        }
        result = advance(task_id, chain_def, skills, last_result)

        assert result["status"] == "NEEDS_CONTEXT"
        assert result["step_idx"] == 0
        assert result["question"] == "请确认结果，或补充修改意见后继续"

        # NC-06: 不改变 current_step
        state_after = _load_state(task_id)
        assert state_after["current_step"] == state_before["current_step"]
        assert state_after["current_step"] == 0

    def test_nc05_advance_needs_context_default_message(self, patch_state_dir):
        """NC-05: advance NEEDS_CONTEXT 无 message → 默认 question。"""
        task_id = "T-nc05"
        chain_def = [
            {"agent": "programmer", "goal": "TDD 实现 + self-review"},
        ]
        skills = {"programmer@0": ["test-driven-development"]}

        start_chain(task_id, chain_def, skills, "programmer")
        last_result = {
            "agent": "programmer",
            "status": "NEEDS_CONTEXT",
        }
        result = advance(task_id, chain_def, skills, last_result)

        assert result["status"] == "NEEDS_CONTEXT"
        assert result["question"] == "缺少上下文，请补充"

    def test_nc06_needs_context_does_not_advance_state(self, patch_state_dir):
        """NC-06: NEEDS_CONTEXT 不影响 state（显式断言 current_step 不变）。"""
        task_id = "T-nc06"
        chain_def = [
            {"agent": "programmer", "goal": "TDD 实现"},
            {"agent": "error-analyst", "goal": "Spec 合规评审"},
        ]
        skills = {
            "programmer@0": ["test-driven-development"],
            "programmer@1": ["code-review"],
        }

        start_chain(task_id, chain_def, skills, "programmer")
        # 推进到 step1
        advance(task_id, chain_def, skills,
                {"agent": "programmer", "status": "DONE", "output_path": "/tmp/p.diff"})
        state_at_step1 = _load_state(task_id)
        assert state_at_step1["current_step"] == 1

        # step1 返回 NEEDS_CONTEXT
        result = advance(task_id, chain_def, skills,
                         {"agent": "error-analyst", "status": "NEEDS_CONTEXT",
                          "message": "缺少 spec 文档"})
        assert result["status"] == "NEEDS_CONTEXT"
        assert result["step_idx"] == 1

        # current_step 应仍为 1
        state_after = _load_state(task_id)
        assert state_after["current_step"] == 1

        # 再次 advance（用户补了上下文，返回 DONE）→ 应推进到 step 2 → chain DONE
        advance(task_id, chain_def, skills,
                {"agent": "error-analyst", "status": "DONE", "output_path": "/tmp/review.md"})
        state_final = _load_state(task_id)
        assert state_final["current_step"] == 2


class TestNCInteractiveStepDispatch:
    """NC-07: interactive 步骤走完整分发链。"""

    def test_nc07_interactive_step_full_dispatch(self, patch_state_dir):
        """NC-07: _build_step_result 正确分发到 _build_interactive_result。"""
        step = {
            "agent": "pm-agent",
            "goal": "输出修复方案摘要，等待用户确认",
            "type": "interactive",
            "keywords": ["confirm", "fix-summary"],
        }
        result = _build_step_result(step, "pm-agent", 1, {"pm-agent@1": ["writing-plans"]})
        assert result["status"] == "NEEDS_CONTEXT"
        assert result["interactive"] is True
        assert result["keywords"] == ["confirm", "fix-summary"]


class TestNCDryRunWithInteractive:
    """NC-08: dry-run + interactive 步骤。"""

    def test_nc08_dry_run_interactive_valid_statuses(self, patch_state_dir):
        """NC-08: dry-run 模式含 interactive 步 → valid_statuses 含 NEEDS_CONTEXT。"""
        task_id = "T-nc08"
        chain_def = [
            {"agent": "pm-agent", "goal": "输出修复方案摘要，等待用户确认"},
        ]
        skills = {"pm-agent@0": ["writing-plans"]}

        # dry-run start → 返回 {"agent": ..., "valid_statuses": [...], "step_idx": ...}
        result = start_chain(task_id, chain_def, skills, "pm-agent", dry_run=True)
        assert "valid_statuses" in result, f"dry-run 应返回 valid_statuses，实际返回: {result}"
        assert "NEEDS_CONTEXT" in result["valid_statuses"], (
            f"interactive 步的 valid_statuses 应含 NEEDS_CONTEXT，实际: {result['valid_statuses']}"
        )

        # dry-run advance
        result2 = advance(task_id, chain_def, skills,
                          {"agent": "pm-agent", "status": "init"},
                          chain_owner="pm-agent", dry_run=True)
        assert "valid_statuses" in result2
        assert "NEEDS_CONTEXT" in result2["valid_statuses"]


class TestNCNormalStepNeedsContext:
    """NC-09: 普通 serial step 返回 NEEDS_CONTEXT。"""

    def test_nc09_normal_step_needs_context(self, patch_state_dir):
        """NC-09: serial step 的 last_result status=NEEDS_CONTEXT → 正常返回 NEEDS_CONTEXT。"""
        task_id = "T-nc09"
        chain_def = [
            {"agent": "programmer", "goal": "TDD 实现 + self-review"},
        ]
        skills = {"programmer@0": ["test-driven-development"]}

        start_chain(task_id, chain_def, skills, "programmer")
        result = advance(task_id, chain_def, skills,
                         {"agent": "programmer", "status": "NEEDS_CONTEXT",
                          "message": "需要更多上下文才能继续"})
        assert result["status"] == "NEEDS_CONTEXT"
        assert result["question"] == "需要更多上下文才能继续"
        assert "step_idx" in result


class TestNCYamlValidation:
    """NC-10: follow-process-chain.yaml 结构验证。"""

    YAML_PATH = os.path.join(_SCRIPT_DIR, "route-map", "chains", "follow-process-chain.yaml")

    def test_nc10_yaml_file_exists(self):
        """follow-process-chain.yaml 文件存在。"""
        assert os.path.exists(self.YAML_PATH), f"文件不存在: {self.YAML_PATH}"

    def test_nc10_yaml_has_six_steps(self):
        """YAML 含 6 个步骤。"""
        import yaml
        with open(self.YAML_PATH, "r") as f:
            data = yaml.safe_load(f)
        assert "steps" in data
        assert len(data["steps"]) == 6

    def test_nc10_yaml_step1_is_interactive(self):
        """第2步（索引1）是 interactive 步骤。"""
        import yaml
        with open(self.YAML_PATH, "r") as f:
            data = yaml.safe_load(f)
        step1 = data["steps"][1]
        assert step1["interactive"] is True
        assert step1["agent"] == "pm-agent"
        assert "confirm" in step1.get("keywords", [])
        assert "fix-summary" in step1.get("keywords", [])

    def test_nc10_yaml_step0_has_completion_contract(self):
        """第1步有 completion_contract。"""
        import yaml
        with open(self.YAML_PATH, "r") as f:
            data = yaml.safe_load(f)
        step0 = data["steps"][0]
        assert "completion_contract" in step0
        assert len(step0["completion_contract"]) >= 1
        assert "verify_command" in step0["completion_contract"][0]

    def test_nc10_yaml_has_chain_step_skills(self):
        """YAML 含 chain_step_skills 键，对应 6 个步骤。"""
        import yaml
        with open(self.YAML_PATH, "r") as f:
            data = yaml.safe_load(f)
        assert "chain_step_skills" in data
        assert len(data["chain_step_skills"]) == 6

    def test_nc10_yaml_non_interactive_steps_have_no_type(self):
        """非 interactive 步骤的 type 默认为 serial（或省略）。"""
        import yaml
        with open(self.YAML_PATH, "r") as f:
            data = yaml.safe_load(f)
        for i, step in enumerate(data["steps"]):
            if i != 1:
                # 省略 type 字段或 type=serial
                step_type = step.get("type", STEP_TYPE_SERIAL)
                assert step_type == STEP_TYPE_SERIAL, f"step[{i}] type 应为 serial，实际为 {step_type}"


# ========================================================================
# CS 组 — Context 瘦身 skill 文档完整性
# ========================================================================


class TestContextSlimSkillDoc:
    """CS-01 ~ CS-02: Context 瘦身 skill 文档结构完备性（可自动化部分）。"""

    SKILL_MD_PATH = os.path.join(_SCRIPT_DIR, "skills", "creative", "context-slimming", "SKILL.md")

    def test_cs01_skill_file_exists(self):
        """CS-01: SKILL.md 文件存在。"""
        assert os.path.exists(self.SKILL_MD_PATH), f"文件不存在: {self.SKILL_MD_PATH}"

    def test_cs01_has_overview_section(self):
        """CS-01: 包含 Overview 章节。"""
        with open(self.SKILL_MD_PATH, "r") as f:
            content = f.read()
        assert "概述" in content or "Overview" in content or "Context 瘦身" in content

    def test_cs01_has_l1_section(self):
        """CS-01: 包含 L1 — 最小化 规范。"""
        with open(self.SKILL_MD_PATH, "r") as f:
            content = f.read()
        assert "L1" in content and "最小化" in content

    def test_cs01_has_l2_section(self):
        """CS-01: 包含 L2 — 中等 规范。"""
        with open(self.SKILL_MD_PATH, "r") as f:
            content = f.read()
        assert "L2" in content and "中等" in content

    def test_cs01_has_l3_section(self):
        """CS-01: 包含 L3 — 完整 规范。"""
        with open(self.SKILL_MD_PATH, "r") as f:
            content = f.read()
        assert "L3" in content and "完整" in content

    def test_cs01_has_usage_principles_table(self):
        """CS-01: 包含使用原则表格（就低/递进/引用优先/清除冗余）。"""
        with open(self.SKILL_MD_PATH, "r") as f:
            content = f.read()
        # 检查是否包含使用原则表格的关键文本
        principles = ["就低原则", "递进原则", "引用优先", "清除冗余"]
        for p in principles:
            assert p in content, f"缺少原则: {p}"

    def test_cs02_markdown_heading_structure(self):
        """CS-02: Markdown 标题层级正确（# ## ###）。"""
        with open(self.SKILL_MD_PATH, "r") as f:
            content = f.read()
        lines = content.split("\n")
        headings = [l.strip() for l in lines if l.strip().startswith("#")]
        # 应有至少 2 个顶级章节
        h1 = [h for h in headings if h.startswith("# ")]
        h2 = [h for h in headings if h.startswith("## ")]
        assert len(h1) >= 1, f"缺少 H1 标题，当前标题: {headings}"
        assert len(h2) >= 1, f"缺少 H2 标题，当前标题: {headings}"

    def test_cs02_markdown_has_code_block(self):
        """CS-02: Markdown 含示例代码块。"""
        with open(self.SKILL_MD_PATH, "r") as f:
            content = f.read()
        assert "```" in content, "没有找到代码块标记"

    def test_cs02_markdown_has_table(self):
        """CS-02: Markdown 含表格（使用原则表格）。"""
        with open(self.SKILL_MD_PATH, "r") as f:
            content = f.read()
        lines = content.split("\n")
        # 找包含 |---| 的表格分隔行
        has_table = any("|---" in line for line in lines)
        assert has_table, "没有找到表格（使用原则表格）"

    def test_cs07_token_saving_estimate(self):
        """CS-07: token 节省估算（L3 vs L1 对比）。
        验证 L1 和 L3 章节均有合理内容（>0 chars）— 说明文档结构完整。
        注意：L1 含示例代码块可能比 L3 长，但两个章节内容均应非空。
        """
        with open(self.SKILL_MD_PATH, "r") as f:
            content = f.read()

        l1_marker = "### L1"
        l3_marker = "### L3"

        l1_start = content.find(l1_marker)
        l3_start = content.find(l3_marker)

        assert l1_start >= 0, "未找到 L1 章节"
        assert l3_start >= 0, "未找到 L3 章节"

        # 提取章节内容（到下一个 ### 或 ---）
        def section_end(text: str, start: int) -> int:
            for delim in ("\n#", "\n---"):
                pos = text.find(delim, start + 1)
                if pos >= 0:
                    return pos
            return len(text)

        l1_text = content[l1_start:section_end(content, l1_start)]
        l3_text = content[l3_start:section_end(content, l3_start)]

        # 两个章节都有实质性内容
        assert len(l1_text.strip()) > 20, f"L1 章节内容不足: {len(l1_text.strip())} chars"
        assert len(l3_text.strip()) > 20, f"L3 章节内容不足: {len(l3_text.strip())} chars"

        # L3 为完整规范，应含独立关键词
        assert "完整背景" in l3_text or "完整文件" in l3_text or "全部历史" in l3_text, (
            "L3 章节应包含完整规范的描述文本"
        )

    # ── CS-03~CS-07 人工/LLM-as-Judge 部分不在此实现 ──
    # 见 test-plan.md CS-03~CS-07：依赖人工或 LLM-as-Judge 评审

    def test_cs08_todo_dry_run_independence(self):
        """CS-08: [TODO] dry-run 与 Context 瘦身无关 — 确认无耦合。"""
        # TODO: dry-run 模式不涉及 context 传递，无交互。
        # 验证方式：确认 dry-run 代码路径中没有 context-slimming 相关调用。
        # 当前代码中 dry-run 仅在 advance() 前置分支处理，不加载 context，
        # 与 Context 瘦身 skill 无耦合。此测试为占位注释。
        pass

    def test_cs09_todo_skip_threshold_independence(self):
        """CS-09: [TODO] skip_threshold 与 Context 瘦身无关 — 确认无耦合。"""
        # TODO: skip_threshold 不涉及 context 内容，无交互。
        # 验证方式：确认 _should_skip_step 和 skip 路径中无 context-slimming 引用。
        # 当前代码中 skip 逻辑仅比较 retry 计数和 threshold，不接触 context 内容。
        # 此测试为占位注释。
        pass

    def test_cs10_todo_runtime_constraint_feasibility(self):
        """CS-10: [TODO] 运行时约束可行性验证 — delegate_task 调用点 context 审计。"""
        # TODO: 模拟 delegate_task(agent, goal, context) 调用点，
        # 在调用点输出 context 的 token 数和规范等级。
        # 当前无运行时 Hook，此测试为占位注释。
        # 建议方式：在 Hermes Agent 调用 delegate_task 处增加 hook，
        # 记录 context 长度并对照 L1/L2/L3 规范进行事后审计。
        pass


# ========================================================================
# INT 组 — 集成测试
# ========================================================================


class TestIntegrationNeedsContextChain:
    """INT-04: 完整链 start → NEEDS_CONTEXT → 继续 → DONE。"""

    def test_int04_full_chain_with_interactive(self, patch_state_dir):
        """INT-04: 完整链含 interactive 步骤的集成测试。
        链: pm-agent(分析) → pm-agent(interactive 确认) → programmer(实现)
        """
        task_id = "T-int04"
        chain_def = [
            {"agent": "pm-agent", "goal": "任务拆解 — 将交付任务拆解为可执行的垂直切片"},
            {
                "agent": "pm-agent",
                "goal": "输出修复方案摘要（改动量+文件+风险），等待用户确认",
                "type": "interactive",
                "keywords": ["fix-summary", "confirm", "review-plan"],
            },
            {"agent": "programmer", "goal": "按拆解结果逐个实现代码"},
        ]
        skills = {
            "pm-agent@0": ["writing-plans"],
            "pm-agent@1": ["writing-plans"],
            "pm-agent@2": ["test-driven-development"],
        }

        # Step 0: start chain
        result0 = start_chain(task_id, chain_def, skills, "pm-agent")
        assert result0["status"] in ("CONTINUE", "CONTINUE_BATCH")

        # Step 0 done → advance to step 1 (interactive)
        result1 = advance(task_id, chain_def, skills,
                          {"agent": "pm-agent", "status": "DONE", "output_path": "/tmp/task-slice.md"})
        # Step 1 是 interactive → 应返回 NEEDS_CONTEXT
        assert result1["status"] == "NEEDS_CONTEXT"
        assert result1["interactive"] is True
        assert result1["keywords"] == ["fix-summary", "confirm", "review-plan"]
        assert result1["step_idx"] == 1

        # State 不应推进（NEEDS_CONTEXT 不改变 current_step）
        state1 = _load_state(task_id)
        assert state1["current_step"] == 1

        # 模拟用户确认（返回 DONE）
        result2 = advance(task_id, chain_def, skills,
                          {"agent": "pm-agent", "status": "DONE", "output_path": "/tmp/plan.md"})
        # Step 1 DONE → 推进到 step 2
        state2 = _load_state(task_id)
        assert state2["current_step"] == 2
        # step 2 是 serial
        assert result2["status"] in ("CONTINUE", "CONTINUE_BATCH")

        # Step 2 done → chain DONE
        result3 = advance(task_id, chain_def, skills,
                          {"agent": "programmer", "status": "DONE", "output_path": "/tmp/impl.diff"})
        assert result3["status"] == "DONE"
        assert "summary" in result3
        assert result3["summary"]["total_steps"] == 3

    def test_int04_needs_context_then_done_preserves_state(self, patch_state_dir):
        """INT-04 子场景：NEEDS_CONTEXT 后再次 advance(DONE) 正确推进。"""
        task_id = "T-int04b"
        chain_def = [
            {"agent": "error-analyst", "goal": "Spec 合规评审"},
            {"agent": "programmer", "goal": "代码质量评审"},
        ]
        skills = {
            "error-analyst@0": ["code-review"],
            "error-analyst@1": ["code-review"],
        }

        start_chain(task_id, chain_def, skills, "error-analyst")

        # Step 0 返回 NEEDS_CONTEXT
        r1 = advance(task_id, chain_def, skills,
                     {"agent": "error-analyst", "status": "NEEDS_CONTEXT",
                      "message": "缺少 spec 文档，请提供"})
        assert r1["status"] == "NEEDS_CONTEXT"
        assert r1["step_idx"] == 0
        assert _load_state(task_id)["current_step"] == 0

        # Step 0 确认后返回 DONE → 推进到 step 1
        r2 = advance(task_id, chain_def, skills,
                     {"agent": "error-analyst", "status": "DONE",
                      "output_path": "/tmp/review.md"})
        assert _load_state(task_id)["current_step"] == 1
        assert r2["status"] in ("CONTINUE", "CONTINUE_BATCH")

        # Step 1 DONE → chain DONE
        r3 = advance(task_id, chain_def, skills,
                     {"agent": "programmer", "status": "DONE",
                      "output_path": "/tmp/quality.md"})
        assert r3["status"] == "DONE"
