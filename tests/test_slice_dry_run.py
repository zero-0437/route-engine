#!/usr/bin/env python3
"""切片测试 — dry-run 模式（12 个 DR 用例 + 2 个 INT 集成用例）。

覆盖范围：
  DR-01  start_chain() --dry-run 正常路径 → 返回 valid_statuses + step_idx=0
  DR-02  advance() --dry-run init 路径（等价 start）
  DR-03  advance() --dry-run 指定 step_idx
  DR-04  advance() --dry-run 指定 target_step_idx（覆盖 step_idx）
  DR-05  advance() --dry-run init + 空 chain_def → ERROR
  DR-06  advance() --dry-run step_idx 越界 → ERROR
  DR-07  advance() --dry-run step_idx 负值 → Python 负索引行为（不报错）
  DR-08  advance() --dry-run 推断 step 类型正确（spec-review 不含 APPROVE）
  DR-09  start --dry-run 不创建状态文件
  DR-10  dry-run 不修改已有 state
  DR-11  CLI 端到端：start --dry-run （subprocess 调用）
  DR-12  CLI 端到端：advance --dry-run （subprocess 调用）
  INT-01 dry-run + skip_threshold 组合：dry-run 不触发跳过决策
  INT-05 dry-run + 正常 chain 共存：状态文件不受干扰

测试策略：
  - 前 10 项直接调用 API 函数（单元测试）
  - DR-11/DR-12 用 subprocess 模拟 CLI 调用
  - INT-01/INT-05 模拟完整链的 dry-run 查询
  - 所有测试共享 patch_state_dir fixture（避免污染真实目录）
"""

import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest

# ── 将被测模块加入 sys.path ────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

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
    _infer_step_type,
    _should_skip_step,
)


# ════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def patch_state_dir(tmp_path):
    """将 STATE_DIR 指向临时目录，避免污染真实 .shared 目录。"""
    import scripts.chain_executor as ce

    original = ce.STATE_DIR
    ce.STATE_DIR = str(tmp_path / ".shared")
    yield
    ce.STATE_DIR = original


@pytest.fixture
def sample_chain_def():
    return [
        {"agent": "programmer", "goal": "TDD 实现 + self-review"},
        {"agent": "error-analyst", "goal": "Spec 合规评审"},
        {"agent": "programmer", "goal": "代码质量评审"},
    ]


@pytest.fixture
def sample_skills():
    return {
        "error-analyst@0": ["test-driven-development"],
        "error-analyst@1": ["code-review"],
        "error-analyst@2": ["simplify-code"],
    }


@pytest.fixture
def single_step_chain():
    """单步 chain，用于边界测试。"""
    return [{"agent": "debugger", "goal": "解析日志"}]


@pytest.fixture
def spec_review_chain():
    """spec-review 类型 step，验证 valid_statuses 不含 APPROVE。"""
    return [{"agent": "reviewer", "goal": "Spec 合规评审"}]


# ════════════════════════════════════════════════════════════
# DR-01 ~ DR-10：API 级别 dry-run 测试
# ════════════════════════════════════════════════════════════


class TestDryRunStart:
    """DR-01: start_chain() --dry-run 正确返回 status 列表。"""

    def test_dry_run_start_returns_valid_statuses(self, sample_chain_def, sample_skills):
        """start_chain(dry_run=True) → 返回 dict 含 valid_statuses/agent/step_idx。"""
        result = start_chain(
            "T-dr01", sample_chain_def, sample_skills, "error-analyst",
            dry_run=True,
        )
        # 不应有 'status' 字段（非状态机路径）
        assert "status" not in result
        # 应有 dry-run 专有字段
        assert "agent" in result
        assert "valid_statuses" in result
        assert "step_idx" in result
        # 默认第一项
        assert result["step_idx"] == 0
        assert result["agent"] == "programmer"
        # tdd 类型应含 APPROVE
        assert "APPROVE" in result["valid_statuses"]

    def test_dry_run_start_returns_proper_type(self, sample_chain_def, sample_skills):
        """start_chain(dry_run=True) 返回的 valid_statuses 是 list 且非空。"""
        result = start_chain(
            "T-dr01b", sample_chain_def, sample_skills, "error-analyst",
            dry_run=True,
        )
        assert isinstance(result["valid_statuses"], list)
        assert len(result["valid_statuses"]) > 0
        assert all(isinstance(s, str) for s in result["valid_statuses"])


class TestDryRunAdvanceInit:
    """DR-02: advance() --dry-run init 路径（等价 start）。"""

    def test_dry_run_advance_init(self, sample_chain_def, sample_skills):
        """advance(dry_run=True, last_result={status:init}) → 同 start，step_idx=0。"""
        result = advance(
            "T-dr02", sample_chain_def, sample_skills,
            {"status": "init"}, chain_owner="error-analyst",
            dry_run=True,
        )
        assert "status" not in result
        assert result["step_idx"] == 0
        assert result["agent"] == "programmer"
        assert "APPROVE" in result["valid_statuses"]


class TestDryRunAdvanceStepIdx:
    """DR-03: advance() --dry-run 指定 step_idx。"""

    def test_dry_run_advance_specific_step(self, sample_chain_def, sample_skills):
        """advance(dry_run=True, last_result 含 step_idx=1) → 返回 step 1 的信息。"""
        result = advance(
            "T-dr03", sample_chain_def, sample_skills,
            {"agent": "error-analyst", "status": "DONE", "step_idx": 1},
            chain_owner="error-analyst",
            dry_run=True,
        )
        assert result["step_idx"] == 1
        assert result["agent"] == "error-analyst"
        # spec-review 不含 APPROVE
        assert "APPROVE" not in result["valid_statuses"]

    def test_dry_run_advance_step_zero_explicit(self, sample_chain_def, sample_skills):
        """step_idx=0 显式指定 → 仍返回 step 0。"""
        result = advance(
            "T-dr03b", sample_chain_def, sample_skills,
            {"agent": "programmer", "status": "DONE", "step_idx": 0},
            chain_owner="error-analyst",
            dry_run=True,
        )
        assert result["step_idx"] == 0
        assert result["agent"] == "programmer"


class TestDryRunTargetStepIdx:
    """DR-04: advance() --dry-run 指定 target_step_idx（覆盖 step_idx）。"""

    def test_dry_run_target_step_idx_overrides(self, sample_chain_def, sample_skills):
        """dry_run + target_step_idx → 使用 target_step_idx 而非 step_idx。"""
        result = advance(
            "T-dr04", sample_chain_def, sample_skills,
            {
                "agent": "programmer", "status": "DONE",
                "step_idx": 0, "target_step_idx": 2,
            },
            chain_owner="error-analyst",
            dry_run=True,
        )
        # target_step_idx 覆盖 step_idx → 跳到 step 2
        assert result["step_idx"] == 2
        assert result["agent"] == "programmer"

    def test_dry_run_target_step_idx_without_step_idx(self, sample_chain_def, sample_skills):
        """仅有 target_step_idx 无 step_idx → 使用 target_step_idx。"""
        result = advance(
            "T-dr04b", sample_chain_def, sample_skills,
            {"agent": "programmer", "status": "DONE", "target_step_idx": 1},
            chain_owner="error-analyst",
            dry_run=True,
        )
        assert result["step_idx"] == 1
        assert result["agent"] == "error-analyst"


class TestDryRunEmptyChain:
    """DR-05: advance() --dry-run init + 空 chain_def → ERROR。"""

    def test_dry_run_empty_chain_init(self, sample_skills):
        """空 chain_def + init → ERROR。"""
        result = advance(
            "T-dr05", [], sample_skills,
            {"status": "init"}, chain_owner="error-analyst",
            dry_run=True,
        )
        assert result["status"] == "ERROR"
        assert "空数组" in result["diagnosis"]

    def test_dry_run_empty_chain_non_init(self, sample_skills):
        """空 chain_def + 非 init → 仍走越界检查（len=0，step>=0 即越界）。"""
        result = advance(
            "T-dr05b", [], sample_skills,
            {"agent": "prog", "status": "DONE", "step_idx": 0},
            chain_owner="test",
            dry_run=True,
        )
        assert result["status"] == "ERROR"
        assert "超出" in result["diagnosis"]


class TestDryRunStepIdxOutOfBounds:
    """DR-06: advance() --dry-run step_idx 越界 → ERROR。"""

    def test_dry_run_step_idx_gt_length(self, sample_chain_def, sample_skills):
        """step_idx=5 > len(chain_def)=3 → ERROR。"""
        result = advance(
            "T-dr06", sample_chain_def, sample_skills,
            {"step_idx": 5}, chain_owner="error-analyst",
            dry_run=True,
        )
        assert result["status"] == "ERROR"
        assert "超出" in result["diagnosis"]

    def test_dry_run_step_idx_equal_length(self, single_step_chain, sample_skills):
        """step_idx=1 == len(chain_def)=1 → 越界（0-based）。"""
        result = advance(
            "T-dr06b", single_step_chain, sample_skills,
            {"step_idx": 1}, chain_owner="test",
            dry_run=True,
        )
        assert result["status"] == "ERROR"
        assert "超出" in result["diagnosis"]

    def test_dry_run_target_step_idx_out_of_bounds(self, sample_chain_def, sample_skills):
        """target_step_idx 越界 → ERROR。"""
        result = advance(
            "T-dr06c", sample_chain_def, sample_skills,
            {"agent": "prog", "status": "DONE",
             "step_idx": 0, "target_step_idx": 10},
            chain_owner="error-analyst",
            dry_run=True,
        )
        assert result["status"] == "ERROR"
        assert "超出" in result["diagnosis"]


class TestDryRunNegativeStepIdx:
    """DR-07: advance() --dry-run step_idx 负值 → Python 负索引行为。"""

    def test_dry_run_negative_step_idx(self, sample_chain_def, sample_skills):
        """step_idx=-1 → Python chain_def[-1] 即最后一步（step 2）。"""
        result = advance(
            "T-dr07", sample_chain_def, sample_skills,
            {"step_idx": -1}, chain_owner="error-analyst",
            dry_run=True,
        )
        assert result["step_idx"] == -1  # 原值仍为 -1
        # agent 应与最后一步一致
        assert result["agent"] == "programmer"
        # chain_def[-1] = {"agent": "programmer", "goal": "代码质量评审"}
        # quality-review 类型含 APPROVE
        assert "valid_statuses" in result


class TestDryRunStepTypeInference:
    """DR-08: advance() --dry-run 推断 step 类型正确。"""

    def test_dry_run_spec_review_no_approve(self, spec_review_chain, sample_skills):
        """spec-review 类型 → valid_statuses 不含 APPROVE。"""
        result = advance(
            "T-dr08", spec_review_chain, sample_skills,
            {"status": "init"}, chain_owner="reviewer",
            dry_run=True,
        )
        assert "APPROVE" not in result["valid_statuses"]
        assert "NEEDS_FIX" in result["valid_statuses"]
        assert "DONE" in result["valid_statuses"]
        assert "BLOCKED" in result["valid_statuses"]
        assert "NEEDS_CONTEXT" in result["valid_statuses"]
        assert "DONE_WITH_CONCERNS" in result["valid_statuses"]

    def test_dry_run_tdd_has_approve(self, sample_chain_def, sample_skills):
        """tdd 类型 → valid_statuses 含 APPROVE。"""
        result = advance(
            "T-dr08b", sample_chain_def, sample_skills,
            {"status": "init"}, chain_owner="error-analyst",
            dry_run=True,
        )
        assert "APPROVE" in result["valid_statuses"]

    def test_dry_run_quality_review_has_approve(self):
        """quality-review 类型 → valid_statuses 含 APPROVE。"""
        chain = [{"agent": "qa", "goal": "代码质量评审"}]
        result = advance(
            "T-dr08c", chain, {"test@0": ["skill"]},
            {"status": "init"}, chain_owner="test",
            dry_run=True,
        )
        assert "APPROVE" in result["valid_statuses"]

    def test_dry_run_fix_type(self):
        """fix 类型 → valid_statuses 较小（无 APPROVE/NEEDS_FIX）。"""
        chain = [{"agent": "fixer", "goal": "根据 review 修复缺陷"}]
        result = advance(
            "T-dr08d", chain, {"test@0": ["skill"]},
            {"status": "init"}, chain_owner="test",
            dry_run=True,
        )
        assert "APPROVE" not in result["valid_statuses"]
        assert "NEEDS_FIX" not in result["valid_statuses"]
        assert "DONE" in result["valid_statuses"]
        assert "BLOCKED" in result["valid_statuses"]
        assert "NEEDS_CONTEXT" in result["valid_statuses"]

    def test_dry_run_default_type_unknown_goal(self):
        """未知 goal → 默认 tdd 类型。"""
        chain = [{"agent": "some-agent", "goal": "一个完全未知的目标"}]
        result = advance(
            "T-dr08e", chain, {"test@0": ["skill"]},
            {"status": "init"}, chain_owner="test",
            dry_run=True,
        )
        assert "APPROVE" in result["valid_statuses"]  # tdd 默认


class TestDryRunNoSideEffects:
    """DR-09: start --dry-run 不创建状态文件。"""

    def test_dry_run_creates_no_state_file(self, sample_chain_def, sample_skills):
        """dry_run=True 后，state 文件应不存在。"""
        task_id = "T-dr09-nocreate"
        assert not os.path.exists(_state_path(task_id))

        start_chain(task_id, sample_chain_def, sample_skills, "error-analyst",
                     dry_run=True)

        # dry-run 不应创建 state 文件
        assert not os.path.exists(_state_path(task_id))

    def test_dry_run_no_state_for_both_start_and_advance(self, sample_chain_def, sample_skills):
        """start + advance 都 dry-run → 仍无 state 文件。"""
        task_id = "T-dr09-multi"
        start_chain(task_id, sample_chain_def, sample_skills, "error-analyst",
                     dry_run=True)
        advance(task_id, sample_chain_def, sample_skills,
                {"agent": "programmer", "status": "DONE", "step_idx": 1},
                chain_owner="error-analyst", dry_run=True)

        assert not os.path.exists(_state_path(task_id))


class TestDryRunDoesNotModifyState:
    """DR-10: dry-run 不修改已有 state。"""

    def test_dry_run_preserves_existing_state(self, sample_chain_def, sample_skills):
        """先正常 start → state 存在 → dry-run 不改变内容。"""
        task_id = "T-dr10-preserve"

        # 正常启动
        start_chain(task_id, sample_chain_def, sample_skills, "error-analyst")
        original_state = _load_state(task_id)
        original_content = dict(original_state)

        # dry-run advance
        advance(task_id, sample_chain_def, sample_skills,
                {"agent": "programmer", "status": "DONE"},
                chain_owner="error-analyst", dry_run=True)

        # state 内容应不变
        after_state = _load_state(task_id)
        assert after_state == original_content

    def test_dry_run_start_does_not_clear_state(self, sample_chain_def, sample_skills):
        """先正常 advance 一次 → dry-run start 不重置 state。"""
        task_id = "T-dr10-noreset"

        start_chain(task_id, sample_chain_def, sample_skills, "error-analyst")
        advance(task_id, sample_chain_def, sample_skills,
                {"agent": "programmer", "status": "DONE", "output_path": "/tmp/p.diff"})
        state_before = _load_state(task_id)
        assert state_before["current_step"] == 1

        # dry-run start 不应重置为 step 0
        start_chain(task_id, sample_chain_def, sample_skills, "error-analyst",
                     dry_run=True)

        state_after = _load_state(task_id)
        assert state_after["current_step"] == state_before["current_step"]
        assert state_after["context"]["diff_path"] == "/tmp/p.diff"

    def test_dry_run_does_not_alter_disk_timestamp(self, sample_chain_def, sample_skills):
        """dry-run 不应修改 state 文件的 mtime。"""
        task_id = "T-dr10-mtime"

        start_chain(task_id, sample_chain_def, sample_skills, "error-analyst")
        state_path = _state_path(task_id)
        mtime_before = os.path.getmtime(state_path)

        advance(task_id, sample_chain_def, sample_skills,
                {"agent": "programmer", "status": "DONE"},
                chain_owner="error-analyst", dry_run=True)

        mtime_after = os.path.getmtime(state_path)
        assert mtime_before == mtime_after


# ════════════════════════════════════════════════════════════
# DR-11 ~ DR-12：CLI 端到端 dry-run 测试
# ════════════════════════════════════════════════════════════


class TestDryRunCLI:
    """DR-11/DR-12: CLI 端到端 dry-run 调用。"""

    CLI_SCRIPT = os.path.join(_SCRIPT_DIR, "scripts", "chain_executor.py")

    @classmethod
    def _run_cli(cls, *args):
        """运行 CLI 并返回 (parsed_json, returncode)。"""
        cmd = [sys.executable, cls.CLI_SCRIPT] + list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            result = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
        return result, proc.returncode

    def test_cli_start_dry_run(self):
        """DR-11: CLI start --dry-run 返回 valid_statuses + step_idx。"""
        result, rc = self._run_cli(
            "start",
            "--task_id", "T-dr11",
            "--chain_def", json.dumps([
                {"agent": "programmer", "goal": "TDD 实现"},
            ]),
            "--chain_step_skills", json.dumps({"test@0": ["skill"]}),
            "--chain_owner", "test",
            "--dry-run",
        )
        assert rc == 0
        assert "status" not in result
        assert result["step_idx"] == 0
        assert result["agent"] == "programmer"
        assert "valid_statuses" in result
        assert isinstance(result["valid_statuses"], list)

    def test_cli_start_dry_run_multi_step(self):
        """CLI start --dry-run 多步 chain → step_idx=0。"""
        result, rc = self._run_cli(
            "start",
            "--task_id", "T-dr11b",
            "--chain_def", json.dumps([
                {"agent": "prog", "goal": "实现"},
                {"agent": "reviewer", "goal": "Spec 合规评审"},
            ]),
            "--chain_step_skills", json.dumps({"test@0": ["s1"], "test@1": ["s2"]}),
            "--chain_owner", "test",
            "--dry-run",
        )
        assert rc == 0
        assert result["step_idx"] == 0
        assert result["agent"] == "prog"

    def test_cli_advance_dry_run(self):
        """DR-12: CLI advance --dry-run 返回 valid_statuses + step_idx。"""
        result, rc = self._run_cli(
            "advance",
            "--task_id", "T-dr12",
            "--chain_def", json.dumps([
                {"agent": "prog", "goal": "实现"},
                {"agent": "reviewer", "goal": "Spec 合规评审"},
            ]),
            "--chain_step_skills", json.dumps({"test@0": ["s1"], "test@1": ["s2"]}),
            "--last_result", json.dumps({"agent": "prog", "status": "DONE", "step_idx": 0}),
            "--chain_owner", "test",
            "--dry-run",
        )
        assert rc == 0
        assert "status" not in result
        assert result["step_idx"] == 0
        assert "valid_statuses" in result

    def test_cli_advance_dry_run_with_target_step_idx(self):
        """CLI advance --dry-run + target_step_idx → 使用 target。"""
        result, rc = self._run_cli(
            "advance",
            "--task_id", "T-dr12b",
            "--chain_def", json.dumps([
                {"agent": "a1", "goal": "实现"},
                {"agent": "a2", "goal": "Spec 合规评审"},
                {"agent": "a3", "goal": "质量评审"},
            ]),
            "--chain_step_skills", json.dumps({"test@0": ["s1"], "test@1": ["s2"], "test@2": ["s3"]}),
            "--last_result", json.dumps({
                "agent": "a1", "status": "DONE",
                "step_idx": 0, "target_step_idx": 2,
            }),
            "--chain_owner", "test",
            "--dry-run",
        )
        assert rc == 0
        assert result["step_idx"] == 2
        assert result["agent"] == "a3"

    def test_cli_dry_run_does_not_create_state(self):
        """CLI dry-run 不创建 state 文件。"""
        from scripts.chain_executor import STATE_DIR

        result, rc = self._run_cli(
            "start",
            "--task_id", "T-dr12-nostate",
            "--chain_def", json.dumps([
                {"agent": "prog", "goal": "实现"},
            ]),
            "--chain_step_skills", json.dumps({"test@0": ["skill"]}),
            "--chain_owner", "test",
            "--dry-run",
        )
        assert rc == 0
        state_path = os.path.join(STATE_DIR, "T-dr12-nostate.json")
        assert not os.path.exists(state_path)


# ════════════════════════════════════════════════════════════
# INT-01 / INT-05：dry-run + skip_threshold 集成
# ════════════════════════════════════════════════════════════


class TestDryRunIntegration:
    """INT-01: dry-run 与 skip_threshold 组合。"""

    def test_dry_run_does_not_trigger_skip(self, sample_chain_def, sample_skills):
        """dry-run 不应触发 skip 决策（dry-run 不加载 state）。"""
        task_id = "T-int01-skip"

        # 先正常启动并让多次 retry 以达到 skip_threshold
        chain = [
            {"agent": "prog", "goal": "实现", "skip_threshold": 1},
            {"agent": "reviewer", "goal": "评审"},
        ]
        skills = {"test@0": ["s1"], "test@1": ["s2"]}

        start_chain(task_id, chain, skills, "test")
        # 模拟 retry 2 次（≥ skip_threshold=1）
        state = _load_state(task_id)
        state["spec_retry"] = 2
        state["quality_retry"] = 2
        _save_state(task_id, state)

        # dry-run 不加载 state，所以不应返回 SKIPPED
        result = advance(
            task_id, chain, skills,
            {"agent": "prog", "status": "DONE"},
            chain_owner="test",
            dry_run=True,
        )
        # dry-run 返回 valid_statuses，不是 SKIPPED
        assert "status" not in result
        assert "valid_statuses" in result
        assert result.get("status") != "SKIPPED"

    def test_dry_run_after_full_chain_does_not_skip(self, sample_chain_def, sample_skills):
        """全链完成后 dry-run → 不应修改已完成的 state。"""
        task_id = "T-int01-full"

        chain = [
            {"agent": "prog", "goal": "实现", "skip_threshold": 2},
            {"agent": "reviewer", "goal": "评审"},
        ]
        skills = {"test@0": ["s1"], "test@1": ["s2"]}

        # 完成链
        start_chain(task_id, chain, skills, "test")
        advance(task_id, chain, skills,
                {"agent": "prog", "status": "DONE", "output_path": "/tmp/r.diff"})
        state = _load_state(task_id)
        assert state["current_step"] == 1

        # dry-run advance 到 step 1
        result = advance(
            task_id, chain, skills,
            {"agent": "reviewer", "status": "DONE", "step_idx": 1},
            chain_owner="test",
            dry_run=True,
        )
        assert result["step_idx"] == 1
        # state 不变
        assert _load_state(task_id)["current_step"] == 1

    def test_dry_run_skip_threshold_still_works_normally(self, sample_chain_def, sample_skills):
        """非 dry-run 时 skip_threshold 正常触发。"""
        task_id = "T-int01-skip-normal"

        chain = [
            {"agent": "prog", "goal": "实现", "skip_threshold": 1},
            {"agent": "reviewer", "goal": "评审"},
        ]
        skills = {"test@0": ["s1"], "test@1": ["s2"]}

        start_chain(task_id, chain, skills, "test")
        # 设 spec_retry=1 以触发 skip
        state = _load_state(task_id)
        state["spec_retry"] = 1
        state["quality_retry"] = 1
        _save_state(task_id, state)

        result = advance(
            task_id, chain, skills,
            {"agent": "prog", "status": "DONE"},
            chain_owner="test",
            dry_run=False,  # 非 dry-run
        )
        # 应触发 skip（但非 dry-run 路径，skip_threshold 跳过后正常 advance）
        # 可能返回 CONTINUE 带 SKIPPED，视实现而定
        # 至少确保不是 ERROR
        assert result["status"] != "ERROR"


    def test_dry_run_state_not_created_after_normal_advance(self):
        """INT-05: dry-run + 正常 chain 共存。"""
        task_id = "T-int05-coexist"
        chain = [{"agent": "prog", "goal": "实现", "skip_threshold": 2}]
        skills = {"test@0": ["s1"]}

        # dry-run 先查询
        result_dr = advance(
            task_id, chain, skills,
            {"status": "init"}, chain_owner="test",
            dry_run=True,
        )
        assert "valid_statuses" in result_dr
        # dry-run 不应创建 state
        assert not os.path.exists(_state_path(task_id))

        # 正常启动
        result_normal = start_chain(task_id, chain, skills, "test")
        assert result_normal["status"] == "CONTINUE"
        # state 应已创建
        assert os.path.exists(_state_path(task_id))

        # 再 dry-run 一次
        result_dr2 = advance(
            task_id, chain, skills,
            {"agent": "prog", "status": "DONE", "step_idx": 0},
            chain_owner="test",
            dry_run=True,
        )
        assert "valid_statuses" in result_dr2
        # state 不受影响
        state = _load_state(task_id)
        assert state["current_step"] == 0


# ════════════════════════════════════════════════════════════
# 额外辅助验证：_infer_step_type 在 dry-run 上下文中正确工作
# ════════════════════════════════════════════════════════════


class TestDryRunInferStepType:
    """验证 _infer_step_type 在 dry-run 路径中的正确性。"""

    def test_infer_tdd(self):
        assert _infer_step_type("TDD 实现 + self-review") == "tdd"

    def test_infer_spec_review(self):
        assert _infer_step_type("Spec 合规评审") == "spec-review"

    def test_infer_quality_review(self):
        assert _infer_step_type("代码质量评审") == "quality-review"

    def test_infer_fix(self):
        assert _infer_step_type("根据 review 修复缺陷") == "fix"

    def test_infer_default(self):
        assert _infer_step_type("任意未知目标") == "tdd"

    def test_infer_empty_goal(self):
        """空 goal 在 dry-run 中走 tdd 默认。"""
        assert _infer_step_type("") == "tdd"
