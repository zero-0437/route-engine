#!/usr/bin/env python3
"""切片测试 — _should_skip_step() + skip_threshold。

覆盖范围：
    SK-01~SK-12 (12 个 _should_skip_step 测试用例)
    INT-02, INT-03, INT-06 (3 个集成测试)

说明：
    测试基于实际代码行为：
    - skip_threshold <= 0 (含 0、负数、无字段)  → 不跳过
    - skip_threshold > 0 且 max_retry >= threshold → SKIPPED
    - state 缺省 spec_retry/quality_retry → .get(..., 0) 降级为 0
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── 将被测模块加入 sys.path ──
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import scripts.chain_executor as ce

from scripts.chain_executor import (
    advance,
    start_chain,
    _should_skip_step,
    _build_chain_done_result,
    _save_state,
    _load_state,
)


# ══════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def patch_state_dir(tmp_path):
    """将 STATE_DIR 指向临时目录，避免污染真实 .shared 目录。"""
    original = ce.STATE_DIR
    ce.STATE_DIR = str(tmp_path / ".shared")
    yield
    ce.STATE_DIR = original


@pytest.fixture
def sample_skills():
    return {
        "prog@0": ["tdd"],
        "prog@1": ["simplify-code"],
    }


def _set_state_retry(task_id, spec_retry=0, quality_retry=0):
    """Helper: 修改指定 task 的 state 中的 retry 计数。"""
    state = _load_state(task_id)
    state["spec_retry"] = spec_retry
    state["quality_retry"] = quality_retry
    _save_state(task_id, state)


# ══════════════════════════════════════════════════════════════════
# 单元测试：_should_skip_step() 纯函数
# ══════════════════════════════════════════════════════════════════


class TestSkipThresholdUnit:
    """SK-01 ~ SK-08：纯函数测试"""

    # ── SK-01: 无 skip_threshold 字段 → 不跳过 ──
    def test_no_skip_threshold_field(self):
        """step 字典没有 skip_threshold 键 → .get() 返回 0 → 不跳过。"""
        step = {"agent": "prog", "goal": "代码质量评审"}
        state = {"spec_retry": 100, "quality_retry": 100}
        result = _should_skip_step(step, state, step_idx=2)
        assert result is None, "无 skip_threshold 字段不应跳过"

    # ── SK-02: skip_threshold=0（显式）→ 不跳过 ──
    def test_threshold_zero(self):
        """skip_threshold=0 显式设置 → skip_threshold <= 0 → 不跳过。"""
        step = {"agent": "prog", "goal": "代码质量评审", "skip_threshold": 0}
        state = {"spec_retry": 999, "quality_retry": 999}
        result = _should_skip_step(step, state, step_idx=2)
        assert result is None, "skip_threshold=0 不应跳过"

    # ── SK-03: skip_threshold=5 且 retry < threshold → 不跳过 ──
    @pytest.mark.parametrize("spec,quality", [
        (0, 0),    # 无 retry
        (1, 0),    # spec_retry 较小
        (2, 1),    # 混合，max=2 < 5
        (4, 0),    # max=4 < 5
        (0, 3),    # quality_retry=3 < 5
        (4, 4),    # max=4 < 5
    ])
    def test_threshold_five_retry_below(self, spec, quality):
        """skip_threshold=5, max_retry < 5 → 不跳过。"""
        step = {"agent": "prog", "goal": "代码质量评审", "skip_threshold": 5}
        state = {"spec_retry": spec, "quality_retry": quality}
        result = _should_skip_step(step, state, step_idx=1)
        assert result is None, f"max_retry={max(spec, quality)} < 5 不应跳过"

    # ── SK-04: skip_threshold=5 且 retry == threshold → 跳过 ──
    def test_threshold_five_retry_equal(self):
        """skip_threshold=5, max_retry=5 → SKIPPED。"""
        step = {"agent": "prog", "goal": "代码质量评审", "skip_threshold": 5}
        state = {"spec_retry": 5, "quality_retry": 0}
        result = _should_skip_step(step, state, step_idx=1)
        assert result is not None
        assert result["status"] == "SKIPPED"
        assert result["step_idx"] == 1
        assert "5" in result.get("diagnosis", "")

    # ── SK-05: skip_threshold=5 且 retry > threshold → 跳过 ──
    def test_threshold_five_retry_above(self):
        """skip_threshold=5, max_retry=7 > 5 → SKIPPED。"""
        step = {"agent": "prog", "goal": "代码质量评审", "skip_threshold": 5}
        state = {"spec_retry": 7, "quality_retry": 0}
        result = _should_skip_step(step, state, step_idx=2)
        assert result is not None
        assert result["status"] == "SKIPPED"
        assert result["step_idx"] == 2
        assert "5" in result.get("diagnosis", "")

    # ── SK-06: quality_retry 触发跳过 ──
    def test_quality_retry_triggers_skip(self):
        """quality_retry (而非 spec_retry) 达到 threshold → SKIPPED。"""
        step = {"agent": "prog", "goal": "代码质量评审", "skip_threshold": 3}
        state = {"spec_retry": 1, "quality_retry": 3}
        result = _should_skip_step(step, state, step_idx=3)
        assert result is not None
        assert result["status"] == "SKIPPED"
        assert result["step_idx"] == 3
        assert "3" in result.get("diagnosis", "")

    # ── SK-07: state 无 retry 计数 → 降级为 0 ──
    def test_state_missing_retry_keys(self):
        """state 中无 spec_retry/quality_retry → .get(..., 0) 降级 → 不跳过。"""
        step = {"agent": "prog", "goal": "代码质量评审", "skip_threshold": 5}
        state = {"current_step": 0}  # 无 retry 键
        result = _should_skip_step(step, state, step_idx=1)
        # max_retry = max(0, 0) = 0 < 5 → 不跳过
        assert result is None, "state 缺省 retry 键应降级为 0"

    # ── SK-08: 负数 threshold → 视同 0 → 不跳过 ──
    @pytest.mark.parametrize("neg_val", [-1, -5, -100])
    def test_negative_threshold(self, neg_val):
        """skip_threshold 为负数 → skip_threshold <= 0 → 不跳过。"""
        step = {"agent": "prog", "goal": "代码质量评审", "skip_threshold": neg_val}
        state = {"spec_retry": 999, "quality_retry": 999}
        result = _should_skip_step(step, state, step_idx=1)
        assert result is None, f"skip_threshold={neg_val} 不应跳过"


# ══════════════════════════════════════════════════════════════════
# 集成测试：skip_threshold + advance()
# ══════════════════════════════════════════════════════════════════


class TestSkipThresholdIntegration:
    """SK-09 ~ SK-12 + INT-02, INT-03, INT-06：advance 集成测试"""

    # ── SK-09: 跳过下一步后正常推进 ──
    def test_skip_next_step_then_advance(self, sample_skills):
        """3-step chain: step0 DONE → 跳过 step1 (threshold=2, retry≥2) → step2 正常进行。"""
        chain_def = [
            {"agent": "prog", "goal": "TDD 实现"},
            {"agent": "prog", "goal": "代码质量评审", "skip_threshold": 2},
            {"agent": "prog", "goal": "最终验证"},
        ]
        task_id = "T-SK09"
        start_chain(task_id, chain_def, sample_skills, "prog")
        # 修改 state 中的 retry 计数，使 step1 的 threshold 被触发
        _set_state_retry(task_id, spec_retry=3, quality_retry=0)

        # step0 DONE → 应跳过 step1（threshold=2, retry=3）
        result = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/p.diff"},
        )
        assert result["status"] == "SKIPPED", "应跳过 step1"
        assert result["step_idx"] == 1, "被跳过的 step 索引为 1"

        # 再次 advance → state.current_step 已指向 step2，执行 step2
        result2 = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/p2.diff"},
        )
        # step2 是最后一步，DONE 后应返回 chain DONE
        assert result2["status"] == "DONE"
        assert "summary" in result2

    # ── SK-10: 跳过最后一步 → 链完成 ──
    def test_skip_last_step_chain_done(self, sample_skills):
        """2-step chain: step0 DONE → step1 有 threshold=1, retry≥1 → 跳过最后一步 → chain DONE。"""
        chain_def = [
            {"agent": "prog", "goal": "TDD 实现"},
            {"agent": "prog", "goal": "代码质量评审", "skip_threshold": 1},
        ]
        task_id = "T-SK10"
        start_chain(task_id, chain_def, sample_skills, "prog")
        _set_state_retry(task_id, spec_retry=2, quality_retry=0)

        # step0 DONE → 应跳过 step1 → current_step=2 ≥ len(chain)=2 → chain DONE
        result = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/p.diff"},
        )
        # 跳过最后一步后调用 _build_chain_done_result → 返回 DONE
        assert result["status"] == "DONE", "跳过最后一步应返回 DONE"
        assert "summary" in result

    # ── SK-11: 单步链 → 跳过逻辑不触发 ──
    def test_single_step_chain(self, sample_skills):
        """1-step chain: 无下一步可跳过，直接正常推进到 DONE。"""
        chain_def = [
            {"agent": "prog", "goal": "TDD 实现", "skip_threshold": 5},
        ]
        task_id = "T-SK11"
        start_chain(task_id, chain_def, sample_skills, "prog")
        _set_state_retry(task_id, spec_retry=999, quality_retry=999)

        # step0 DONE → next_step_idx=1 >= len(chain)=1 → skip 逻辑不执行 → DONE
        result = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/p.diff"},
        )
        assert result["status"] == "DONE", "单步链完成应返回 DONE"

    # ── SK-12: 多次跳过验证 ──
    def test_multiple_skips_tracking(self, sample_skills):
        """跳过步后 state.current_step 正确推进，被跳过步不会再次执行。"""
        chain_def = [
            {"agent": "prog", "goal": "步骤 A"},
            {"agent": "prog", "goal": "步骤 B", "skip_threshold": 1},
            {"agent": "prog", "goal": "步骤 C"},
        ]
        task_id = "T-SK12"
        start_chain(task_id, chain_def, sample_skills, "prog")
        _set_state_retry(task_id, spec_retry=2, quality_retry=0)

        # step0 DONE → 跳过 step1
        result = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/a.diff"},
        )
        assert result["status"] == "SKIPPED", f"应跳过 step1，实际={result['status']}"
        assert result["step_idx"] == 1

        # 再次 advance → 应执行 step2（而非再次执行 step1）
        result2 = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/b.diff"},
        )
        # step2 DONE → chain 完成
        assert result2["status"] == "DONE"
        assert "summary" in result2

    # ── INT-03: 完整链 start → DONE → skip → DONE → DONE ──
    def test_full_chain_with_skip(self, sample_skills):
        """3-step chain: step0 DONE → skip step1 → step2 DONE → chain DONE。"""
        chain_def = [
            {"agent": "prog", "goal": "步骤 0"},
            {"agent": "prog", "goal": "步骤 1（将被跳过）", "skip_threshold": 1},
            {"agent": "prog", "goal": "步骤 2"},
        ]
        task_id = "T-INT03"
        start_chain(task_id, chain_def, sample_skills, "prog")
        _set_state_retry(task_id, spec_retry=2, quality_retry=0)

        # step0 DONE → 跳过 step1
        r1 = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/0.diff"},
        )
        assert r1["status"] == "SKIPPED", f"应跳过 step1，实际={r1['status']}"
        assert r1["step_idx"] == 1

        # step2 DONE → chain 完成
        r2 = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/2.diff"},
        )
        assert r2["status"] == "DONE"
        assert "summary" in r2
        assert r2["summary"]["total_steps"] == 3

    # ── INT-06: skip_threshold 最后一步 ──
    def test_skip_threshold_last_step(self, sample_skills):
        """2-step chain: step0 DONE → step1 (最后一步) 有 threshold → 跳过 → chain DONE。"""
        chain_def = [
            {"agent": "prog", "goal": "步骤 A"},
            {"agent": "prog", "goal": "步骤 B", "skip_threshold": 1},
        ]
        task_id = "T-INT06"
        start_chain(task_id, chain_def, sample_skills, "prog")
        _set_state_retry(task_id, spec_retry=3, quality_retry=0)

        # step0 DONE → 跳过 step1（最后一步）→ chain DONE
        result = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/a.diff"},
        )
        assert result["status"] == "DONE", f"跳过最后一步应 DONE，实际={result['status']}"
        assert "summary" in result

    # ── INT-02: skip_threshold + interactive 步 ──
    def test_skip_interactive_step(self, sample_skills):
        """被跳过的步骤是 interactive 步 → skip 逻辑与步骤类型无关。"""
        chain_def = [
            {"agent": "prog", "goal": "TDD 实现"},
            {
                "agent": "pm-agent", "goal": "用户确认",
                "interactive": True, "skip_threshold": 2,
            },
            {"agent": "prog", "goal": "最终验证"},
        ]
        task_id = "T-INT02"
        start_chain(task_id, chain_def, sample_skills, "prog")
        _set_state_retry(task_id, spec_retry=3, quality_retry=1)

        # step0 DONE → 应跳过 interactive step1
        result = advance(
            task_id, chain_def, sample_skills,
            {"agent": "prog", "status": "DONE", "output_path": "/tmp/t.diff"},
        )
        assert result["status"] == "SKIPPED", "interactive 步也可被跳过"
        assert result["step_idx"] == 1
