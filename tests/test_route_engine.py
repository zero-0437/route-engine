"""单元测试 — route_engine.py 路由引擎（unittest 版）

覆盖范围:
  - TestLoadRouteMap: 加载/缓存/异常
  - TestNormalize: 文本归一化
  - TestMatchRule: 规则匹配（regex/phrase/keyword）
  - TestEvaluate: 评分正确性（12 Agent 全量）
  - TestDecide: 决策逻辑（auto/fallback/tiebreak）
  - TestRouteIntegration: 12 Agent 全量端到端
  - TestMisMatchProtection: 误匹配防护（PR≠PROXY, UI≠bUILd, bug≠debug）
  - TestCLI: 命令行入口验收

运行方式:
    cd /opt/data && uv run python -m unittest tests.test_route_engine -v
"""

import io
import sys
import os
import unittest
import contextlib

# ── 路径解析 ──────────────────────────────────────────────────────
# 将 scripts/ 加入 sys.path，使 route_engine 可被导入
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_SCRIPT_DIR, "..", "scripts"))

from route_engine import (
    route,
    load_route_map,
    evaluate,
    decide,
    match_rule,
    _normalize,
    _route_map_cache,
)

# ── 缓存清理辅助 ──────────────────────────────────────────────────
def _clear_cache():
    """安全清空模块级路由缓存"""
    import route_engine as _re
    _re._route_map_cache = None


# ══════════════════════════════════════════════════════════════════
# 1. TestLoadRouteMap — 加载/缓存/异常
# ══════════════════════════════════════════════════════════════════

class TestLoadRouteMap(unittest.TestCase):
    """加载/缓存/异常"""

    def setUp(self):
        _clear_cache()

    def test_imports(self):
        """模块可正常导入，不存在导入错误"""
        self.assertTrue(callable(route))
        self.assertTrue(callable(load_route_map))
        self.assertTrue(callable(evaluate))
        self.assertTrue(callable(decide))

    def test_load_returns_dict_with_all_12_agents(self):
        """load_route_map() 返回非空 dict，包含所有 12 个 Agent"""
        rm = load_route_map()
        expected = {
            "pm-agent", "programmer", "error-analyst", "data-analyst",
            "ui-designer", "document-processor", "file-ops", "synology-helper",
            "memory-agent", "prompt-engineer", "reality-checker", "docs-writer",
        }
        self.assertIsInstance(rm, dict)
        self.assertTrue(expected.issubset(rm["agents"].keys()),
                        f"缺少 Agent: {expected - set(rm['agents'].keys())}")
        # 每个 Agent 应包含 rules 和 priority
        for name in expected:
            self.assertIn("rules", rm["agents"][name], f"{name} 缺少 rules")
            self.assertIn("priority", rm["agents"][name], f"{name} 缺少 priority")

    def test_cache_returns_same_object(self):
        """验证模块级缓存生效——重复调用返回同一对象"""
        _clear_cache()
        rm1 = load_route_map()
        rm2 = load_route_map()
        self.assertIs(rm1, rm2, "缓存未命中，load_route_map 返回了新对象")

    def test_route_map_has_defaults(self):
        """route-map 应包含 defaults 和 overrides 字段"""
        rm = load_route_map()
        self.assertIn("defaults", rm)
        self.assertIn("overrides", rm)
        self.assertGreater(rm["defaults"]["min_confidence"], 0)
        self.assertIsNotNone(rm["defaults"]["fallback_agent"])


# ══════════════════════════════════════════════════════════════════
# 2. TestNormalize — 文本归一化
# ══════════════════════════════════════════════════════════════════

class TestNormalize(unittest.TestCase):
    """文本归一化"""

    def test_lowercase_chinese(self):
        """中文+英文混合转小写"""
        self.assertEqual(_normalize("修复一个 Python Bug"), "修复一个 python bug")

    def test_mixed_case_english(self):
        """全英文大写转小写"""
        self.assertEqual(_normalize("PROXY 连接"), "proxy 连接")

    def test_already_lower(self):
        """已全小写保持不变"""
        self.assertEqual(_normalize("hello world"), "hello world")

    def test_empty_string(self):
        """空字符串返回空字符串"""
        self.assertEqual(_normalize(""), "")

    def test_numbers_and_symbols(self):
        """数字和符号不受影响"""
        self.assertEqual(_normalize("123 !@#"), "123 !@#")


# ══════════════════════════════════════════════════════════════════
# 3. TestMatchRule — 规则匹配（regex/phrase/keyword）
# ══════════════════════════════════════════════════════════════════

class TestMatchRule(unittest.TestCase):
    """规则匹配（regex/phrase/keyword）"""

    def test_regex_match(self):
        """regex 类型：匹配成功"""
        self.assertTrue(match_rule("修复一个 python bug",
                                   {"type": "regex", "pattern": r"\bbug\b"}))

    def test_regex_no_match(self):
        """regex 类型：不匹配"""
        self.assertFalse(match_rule("hello world",
                                    {"type": "regex", "pattern": r"\bbug\b"}))

    def test_regex_word_boundary_protection(self):
        """regex 类型：\\bPR\\b 不应匹配 PROXY"""
        self.assertFalse(match_rule("修复 PROXY",
                                    {"type": "regex", "pattern": r"\bPR\b"}),
                         "\\bPR\\b 不应匹配 PROXY")

    def test_regex_word_boundary_bug_vs_debug(self):
        """regex 类型：\\bbug\\b 不应匹配 debug"""
        self.assertFalse(match_rule("debug the code",
                                    {"type": "regex", "pattern": r"\bbug\b"}),
                         "\\bbug\\b 不应匹配 debug")

    def test_phrase_exact_match(self):
        """phrase 类型：完全匹配"""
        self.assertTrue(match_rule("写一个函数",
                                   {"type": "phrase", "pattern": "写一个函数"}))

    def test_phrase_partial_match(self):
        """phrase 类型：部分包含"""
        self.assertTrue(match_rule("请写一个函数给我",
                                   {"type": "phrase", "pattern": "写一个函数"}))

    def test_phrase_no_match(self):
        """phrase 类型：不包含"""
        self.assertFalse(match_rule("写一个类",
                                    {"type": "phrase", "pattern": "写一个函数"}))

    def test_keyword_match(self):
        """keyword 类型：匹配（归一化后）"""
        kw = _normalize("架构")
        self.assertTrue(match_rule(kw, {"type": "keyword", "pattern": "架构"}))

    def test_keyword_case_insensitive(self):
        """keyword 类型：大小写不敏感（输入需先归一化，与实际调用一致）"""
        normalized = _normalize("ARCHITECTURE")
        self.assertTrue(match_rule(normalized,
                                   {"type": "keyword", "pattern": "architecture"}))

    def test_keyword_no_match(self):
        """keyword 类型：不匹配"""
        self.assertFalse(match_rule("hello",
                                    {"type": "keyword", "pattern": "架构"}))

    def test_unsupported_type_raises(self):
        """未知规则类型抛出 ValueError"""
        with self.assertRaises(ValueError):
            match_rule("test", {"type": "unknown", "pattern": "test"})


# ══════════════════════════════════════════════════════════════════
# 4. TestEvaluate — 评分正确性（12 Agent 全量 + 排序）
# ══════════════════════════════════════════════════════════════════

class TestEvaluate(unittest.TestCase):
    """评分正确性"""

    def setUp(self):
        _clear_cache()

    def _top_agent(self, text: str) -> str:
        """辅助方法：返回评分最高的 Agent 名"""
        rm = load_route_map()
        scores = evaluate(text, rm)
        return scores[0][0] if scores else ""

    # ── pm-agent ──
    def test_pm_agent_architecture(self):
        """pm-agent：设计多 Agent 架构"""
        self.assertEqual(self._top_agent("设计多 Agent 架构"), "pm-agent")

    # ── programmer ──
    def test_programmer_bug(self):
        """programmer：PROXY 连接的 bug"""
        self.assertEqual(self._top_agent("修复 PROXY 连接的 bug"), "programmer")

    # ── error-analyst ──
    def test_error_analyst(self):
        """error-analyst：审查代码安全性"""
        self.assertEqual(self._top_agent("审查代码安全性"), "error-analyst")

    # ── data-analyst ──
    def test_data_analyst_search(self):
        """data-analyst：搜索最新的 AI 论文"""
        self.assertEqual(self._top_agent("搜索最新的 AI 论文"), "data-analyst")

    # ── ui-designer ──
    def test_ui_designer(self):
        """ui-designer：构建新的 UI 界面"""
        self.assertEqual(self._top_agent("构建新的 UI 界面"), "ui-designer")

    # ── document-processor ──
    def test_document_processor(self):
        """document-processor：把 PDF 转换成 Word"""
        self.assertEqual(self._top_agent("把 PDF 转换成 Word"), "document-processor")

    # ── file-ops ──
    def test_file_ops(self):
        """file-ops：处理文件目录结构"""
        self.assertEqual(self._top_agent("处理文件目录结构"), "file-ops")

    # ── synology-helper ──
    def test_synology_helper(self):
        """synology-helper：NAS 备份配置"""
        self.assertEqual(self._top_agent("NAS 备份配置"), "synology-helper")

    # ── memory-agent ──
    def test_memory_agent(self):
        """memory-agent：记忆之前的讨论内容"""
        self.assertEqual(self._top_agent("记忆之前的讨论内容"), "memory-agent")

    # ── prompt-engineer ──
    def test_prompt_engineer(self):
        """prompt-engineer：优化 system prompt"""
        self.assertEqual(self._top_agent("优化 system prompt"), "prompt-engineer")

    # ── reality-checker ──
    def test_reality_checker(self):
        """reality-checker：端到端集成测试验证"""
        self.assertEqual(self._top_agent("端到端集成测试验证"), "reality-checker")

    # ── docs-writer ──
    def test_docs_writer(self):
        """docs-writer：写一份 API 参考文档"""
        self.assertEqual(self._top_agent("写一份 API 参考文档"), "docs-writer")

    # ── 排序检查 ──
    def test_scores_descending(self):
        """验证分数按降序排列"""
        rm = load_route_map()
        scores = evaluate("修复一个 Python bug", rm)
        vals = [s for _, s, _ in scores]
        self.assertEqual(vals, sorted(vals, reverse=True),
                         "分数未按降序排列")

    def test_scores_all_nonnegative(self):
        """所有评分应 ≥ 0（负权重被 clamp 为 0）"""
        rm = load_route_map()
        scores = evaluate("写一份 API 参考文档", rm)
        for name, score, _ in scores:
            self.assertGreaterEqual(score, 0.0,
                                    f"{name} 的评分为负数: {score}")


# ══════════════════════════════════════════════════════════════════
# 5. TestDecide — 决策逻辑（auto/fallback/tiebreak）
# ══════════════════════════════════════════════════════════════════

class TestDecide(unittest.TestCase):
    """决策逻辑（auto/fallback/tiebreak）"""

    def setUp(self):
        _clear_cache()

    def test_high_confidence_auto(self):
        """高置信度 → auto 自动路由"""
        rm = load_route_map()
        scores = [("programmer", 0.85), ("error-analyst", 0.3)]
        result = decide(scores, rm, None)
        self.assertEqual(result["agent"], "programmer")
        self.assertEqual(result["method"], "auto")
        self.assertEqual(result["confidence"], 0.85)

    def test_below_threshold_fallback(self):
        """置信度低于阈值 → llm_fallback"""
        rm = load_route_map()
        scores = [("programmer", 0.2), ("error-analyst", 0.1)]
        result = decide(scores, rm, None)
        self.assertEqual(result["method"], "llm_fallback")
        self.assertEqual(result["agent"], "pm-agent")  # fallback_agent from defaults

    def test_tiebreak_higher_priority(self):
        """平局按 priority 裁决（数值越小优先级越高）"""
        rm = load_route_map()
        # ui-designer priority=5, programmer priority=2 → programmer wins
        scores = [("ui-designer", 0.8), ("programmer", 0.8)]
        result = decide(scores, rm, None)
        self.assertEqual(result["agent"], "programmer")
        self.assertEqual(result["method"], "auto_tiebreak")
        self.assertEqual(result["confidence"], 0.8)

    def test_empty_scores_fallback(self):
        """空评分列表 → llm_fallback"""
        rm = load_route_map()
        result = decide([], rm, None)
        self.assertEqual(result["method"], "llm_fallback")
        self.assertEqual(result["agent"], "pm-agent")

    def test_fallback_reason_contains_threshold_info(self):
        """fallback 原因应包含阈值信息"""
        rm = load_route_map()
        scores = [("programmer", 0.15)]
        result = decide(scores, rm, None)
        self.assertIn("fallback_reason", result["details"])
        self.assertIn("0.15", result["details"]["fallback_reason"])

    def test_tiebreak_reason_contains_tied_agents(self):
        """平局裁决原因应包含所有平局 Agent"""
        rm = load_route_map()
        scores = [("ui-designer", 0.8), ("programmer", 0.8)]
        result = decide(scores, rm, None)
        self.assertIn("programmer", result["details"]["fallback_reason"])
        self.assertIn("ui-designer", result["details"]["fallback_reason"])

    def test_result_structure(self):
        """decide 返回结果包含必要字段"""
        rm = load_route_map()
        result = decide([("programmer", 0.85)], rm, None)
        for key in ("agent", "confidence", "method", "details",
                     "auto_skills", "manual_skills"):
            self.assertIn(key, result, f"缺少字段: {key}")
        for key in ("scores", "matched_rules", "fallback_reason"):
            self.assertIn(key, result["details"], f"details 缺少字段: {key}")


# ══════════════════════════════════════════════════════════════════
# 6. TestRouteIntegration — 12 Agent 全量端到端
# ══════════════════════════════════════════════════════════════════

class TestRouteIntegration(unittest.TestCase):
    """12 Agent 全量端到端"""

    def setUp(self):
        _clear_cache()

    def test_programmer(self):
        """programmer：修复 PROXY 连接的 bug"""
        result = route("修复 PROXY 连接的 bug")
        self.assertEqual(result["agent"], "programmer",
                         f"期望 programmer, 得到 {result['agent']}")

    def test_ui_designer(self):
        """ui-designer：构建新的 UI 界面"""
        result = route("构建新的 UI 界面")
        self.assertEqual(result["agent"], "ui-designer")

    def test_document_processor(self):
        """document-processor：把 PDF 转换成 Word"""
        result = route("把 PDF 转换成 Word")
        self.assertEqual(result["agent"], "document-processor")

    def test_synology_helper(self):
        """synology-helper：NAS 备份配置"""
        result = route("NAS 备份配置")
        self.assertEqual(result["agent"], "synology-helper")

    def test_data_analyst(self):
        """data-analyst：搜索最新的 AI 论文"""
        result = route("搜索最新的 AI 论文")
        self.assertEqual(result["agent"], "data-analyst")

    def test_docs_writer(self):
        """docs-writer：写一份 API 参考文档"""
        result = route("写一份 API 参考文档")
        self.assertEqual(result["agent"], "docs-writer")

    def test_pm_agent_override(self):
        """pm-agent：overrides 中 '架构'→pm-agent，跳过评分直接返回"""
        result = route("设计多 Agent 架构方案")
        self.assertEqual(result["agent"], "pm-agent")
        self.assertEqual(result["method"], "auto")

    def test_error_analyst(self):
        """error-analyst：审查代码安全性"""
        result = route("审查代码安全性")
        self.assertEqual(result["agent"], "error-analyst")

    def test_prompt_engineer(self):
        """prompt-engineer：优化 system prompt"""
        result = route("优化 system prompt")
        self.assertEqual(result["agent"], "prompt-engineer")

    def test_file_ops(self):
        """file-ops：处理文件目录结构"""
        result = route("处理文件目录结构")
        self.assertEqual(result["agent"], "file-ops")

    def test_reality_checker(self):
        """reality-checker：端到端集成测试验证"""
        result = route("端到端集成测试验证")
        self.assertEqual(result["agent"], "reality-checker")

    def test_memory_agent(self):
        """memory-agent：记忆之前的讨论内容"""
        result = route("记忆之前的讨论内容")
        self.assertEqual(result["agent"], "memory-agent")

    def test_result_has_all_keys(self):
        """route 结果包含所有必要字段"""
        result = route("修复一个 bug")
        self.assertIn("agent", result)
        self.assertIn("confidence", result)
        self.assertIn("method", result)
        self.assertIn("details", result)
        self.assertIn("auto_skills", result)
        self.assertIn("manual_skills", result)

    def test_details_has_expected_keys(self):
        """details 包含 scores / matched_rules / fallback_reason"""
        result = route("修复一个 bug")
        d = result["details"]
        self.assertIn("scores", d)
        self.assertIn("matched_rules", d)
        self.assertIn("fallback_reason", d)

    def test_confidence_range(self):
        """置信度在 [0.0, 1.0] 范围内"""
        result = route("修复一个 bug")
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)

    def test_auto_method_has_no_fallback_reason(self):
        """自动路由时 fallback_reason 应为空字符串"""
        result = route("修复 PROXY 连接的 bug")
        if result["method"] == "auto":
            self.assertEqual(result["details"]["fallback_reason"], "")

    def test_low_confidence_input_fallsback(self):
        """模糊输入触发 llm_fallback"""
        result = route("你好今天天气不错")
        self.assertIn(result["method"], ("llm_fallback",))


# ══════════════════════════════════════════════════════════════════
# 7. TestMisMatchProtection — 误匹配防护
# ══════════════════════════════════════════════════════════════════

class TestMisMatchProtection(unittest.TestCase):
    """误匹配防护 — 边界条件测试"""

    def setUp(self):
        _clear_cache()

    def test_pr_not_match_proxy(self):
        """PR≠PROXY：\\bPR\\b 不应匹配 PROXY"""
        result = route("修复 PROXY 的配置")
        self.assertNotEqual(result["agent"], "programmer",
                            "PROXY 不应被误匹配为 programmer（PR≠PROXY）")

    def test_ui_not_match_build(self):
        """UI≠bUILd：'重建缓存 rebuild' 不应匹配 ui-designer"""
        result = route("重建缓存 rebuild")
        self.assertNotEqual(result["agent"], "ui-designer",
                            "rebuild 不应被误匹配为 ui-designer（UI≠bUILd）")

    def test_bug_not_match_debug(self):
        """bug≠debug：\\bbug\\b 不应匹配 debug"""
        result = route("调试这段代码 debug")
        self.assertNotEqual(result["agent"], "programmer",
                            "debug 不应被误匹配为 programmer（bug≠debug）")

    def test_document_not_match_writing(self):
        """文档处理 vs 文档写作：'格式转换' 不应误匹配 docs-writer"""
        result = route("把 PDF 转换成 Word 格式")
        # Should go to document-processor, not docs-writer
        self.assertEqual(result["agent"], "document-processor",
                         f"格式转换应路由到 document-processor, 得到 {result['agent']}")

    def test_search_not_match_coding(self):
        """搜索类请求不应误匹配 programmer"""
        result = route("查一下最近 AI 论文的动态")
        self.assertNotEqual(result["agent"], "programmer",
                            "搜索请求不应被误匹配为 programmer")

    def test_backup_not_match_programmer(self):
        """备份类请求不应误匹配 programmer（负权重防护）"""
        result = route("配置完整备份")
        self.assertNotEqual(result["agent"], "programmer",
                            "备份请求不应被误匹配为 programmer")


# ══════════════════════════════════════════════════════════════════
# 8. TestCLI — 命令行入口验收
# ══════════════════════════════════════════════════════════════════

class TestCLI(unittest.TestCase):
    """命令行入口验收"""

    def setUp(self):
        _clear_cache()

    def _run_main(self, args):
        """辅助方法：运行 main 并捕获 stdout"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            from route_engine import main
            main(args)
        return buf.getvalue()

    def test_cli_programmer(self):
        """CLI：'修复一个 Python bug' → programmer"""
        out = self._run_main(["修复一个 Python bug"])
        self.assertIn("programmer", out)

    def test_cli_synology(self):
        """CLI：'NAS 备份' → synology-helper"""
        out = self._run_main(["NAS 备份"])
        self.assertIn("synology-helper", out)

    def test_cli_docs_writer(self):
        """CLI：'写一份 API 参考文档' → docs-writer"""
        out = self._run_main(["写一份 API 参考文档"])
        self.assertIn("docs-writer", out)

    def test_cli_pm_agent(self):
        """CLI：'架构设计' → pm-agent"""
        out = self._run_main(["架构设计"])
        self.assertIn("pm-agent", out)

    def test_cli_output_is_json(self):
        """CLI 输出应为有效 JSON"""
        out = self._run_main(["修复一个 bug"])
        import json
        try:
            parsed = json.loads(out)
            self.assertIn("agent", parsed)
        except json.JSONDecodeError:
            self.fail("CLI 输出不是有效 JSON")


# ══════════════════════════════════════════════════════════════════
# 启动入口（支持 python test_route_engine.py 直接运行）
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)

# ══════════════════════════════════════════════════════════════════
# 10. TestChainMode — mode 字段（orchestrator / stepwise）
# ══════════════════════════════════════════════════════════════════

class TestChainMode(unittest.TestCase):
    """Test the 'mode' field returned by chain keyword matching (C-方案)"""

    def test_pub_chain_orchestrator_mode(self):
        """pub-chain 应返回 mode=orchestrator"""
        result = route("发布路由引擎")
        self.assertEqual(result.get("mode"), "orchestrator")

    def test_dual_review_stepwise_mode(self):
        """dual-review-chain 无显式 mode，应返回默认 stepwise"""
        result = route("双评审")
        self.assertEqual(result.get("mode"), "stepwise")

    def test_follow_process_stepwise_mode(self):
        """follow-process-chain 无显式 mode，应返回默认 stepwise"""
        result = route("按流程走")
        self.assertEqual(result.get("mode"), "stepwise")

    def test_default_mode_for_unknown_chain(self):
        """不存在的链关键词应返回默认 stepwise"""
        # 走 normal route, 非 chain keyword
        result = route("审查代码安全性")
        # 这个不是 chain 匹配，所以 chain/model 字段可能不存在
        # 我们检查如果有 mode 字段则应为 stepwise
        if "mode" in result:
            self.assertEqual(result["mode"], "stepwise")
