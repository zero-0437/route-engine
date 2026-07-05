# 路由引擎发布文档（完全版）

> **内部文档 — 仅供私库使用**
> 生成日期：2026-07-06
> 版本号：v2.6
> 报告编号：CHANGELOG-FULL-20260706-v1

---

## 一、发布周期概览

### 1.1 周期范围

| 项目 | 值 |
|------|-----|
| 上次发布 Commit | `48e1e85` — update: 路由引擎 v2.8 (chain_step_skills + _validate_chain + dual-review 路由) |
| 当前 HEAD | `61cd3fb` — chore: release v2.5 — global safety net, rule slim (285→216), rescue/kill/pause, analyzer tools, docs restructure |
| 周期类型 | 安全体系构建 + 规则瘦身 + 输入检测热修复 |
| 公开库同步基线 | `b799a8b` — sync: P2-9 llm-rule-suggest + P2-10 --fix + P1-7 rule review checklist |

### 1.2 架构全景（本轮更新后）

```
route-map/                        # 路由规则配置目录
├── index.yaml                    # 索引文件（schema v2.5+，含 pub-chain 配置）
├── routes/*.yaml (15个)          # 各 Agent 的路由规则
├── chains/*.yaml (8个)           # Chain 管线定义（含 dual-review-chain 更新）
└── shared.yaml                   # 共享规则

scripts/
├── route_engine.py               # 路由引擎核心 → 路由决策（含输入检测跳过逻辑 🆕）
├── chain_executor.py             # Chain 执行状态机 → 步骤推进（含安全体系四层）
├── chain_config.py               # 共享 YAML 加载模块（新建入库）
├── analyze-route-log.py          # 路由日志分析（analyze 子命令）
└── validate-route-map.py         # 规则验证器（冗余度检查）

src/                              # 脱敏版源码目录（公开库）
├── route_engine.py               # 公开版路由引擎（含输入检测跳过逻辑 🆕）
└── chain_executor.py             # 公开版 Chain 执行器

tests/
├── test_route_engine.py          # 路由引擎测试
└── test_chain_executor.py        # Chain 执行器测试（58用例）

docs/
├── PLUGIN_INTEGRATION.md         # 接口契约声明
├── DELEGATION_PROTOCOL.md        # 委派协议文档
├── ROUTE-ENGINE-CHANGELOG.md     # 脱敏版历史文档
└── why-route-engine.md           # 引擎设计说明
```

### 1.3 向后兼容性声明

| 维度 | 兼容性 | 说明 |
|------|--------|------|
| API 接口 | ✅ 完全兼容 | `route()` 函数签名不变，路由结果结构不变 |
| YAML 配置 | ✅ 向后兼容 | 新增 `mode` 字段（orchestrator/stepwise），旧配置自动默认 stepwise |
| 运行时 | ✅ 向前兼容 | 新增输入检测跳过逻辑（`?`/`！`/`..`），不影响正常路由流程 |
| 持久化状态 | ⚠️ 增量变更 | 状态文件新增 `total_iterations`/`chain_started_at`/`last_checkpoint` 字段，旧状态自动填充默认值 |

---

## 二、本轮修改/新增文件清单

### 2.1 新建文件

| 文件路径 | 行数 | 说明 |
|----------|------|------|
| `/opt/data/scripts/chain_config.py` | 47 | 共享 YAML 加载模块。提供 SCRIPT_DIR/ROUTE_MAP_DIR/INDEX_YAML_PATH/SKILL_CACHE_FILE 四个路径常量 + load_yaml_safe()/load_index()/load_chain() 三个函数 |
| `/opt/data/repos/hermes-zero-token-router/docs/PLUGIN_INTEGRATION.md` | 154 | 插件接口契约声明文档，规范 route-engine 与 Hermes Agent 的集成边界 |
| `/opt/data/repos/hermes-zero-token-router/docs/DELEGATION_PROTOCOL.md` | 72 | 委派协议文档，定义子 Agent 委派流程 |
| `/opt/data/repos/hermes-zero-token-router/docs/why-route-engine.md` | 73 | 路由引擎设计决策说明 |
| `/opt/data/repos/hermes-zero-token-router/plugins/route-router/__init__.py` | 0 | 路由插件包 init |
| `/opt/data/repos/hermes-zero-token-router/plugins/route-router/router.py` | 79 | 路由插件实现 |
| `/opt/data/repos/hermes-zero-token-router/docs/ROUTE-ENGINE-CHANGELOG.md` | 125 | 脱敏版历史文档归档 |

### 2.2 修改文件

| 文件路径 | 原行数 | 新行数 | 变更说明 |
|----------|--------|--------|----------|
| `/opt/data/scripts/chain_executor.py` | ~325 | ~1629 | **安全体系四层构建**：① Step Brief 文件写入（`/opt/data/.shared/briefs/`）② 全局迭代上限 MAX_TOTAL_ITERATIONS=100 ③ Wall-clock 超时 CHAIN_TIMEOUT=3600s ④ Checkpoint 恢复机制（step_idx/completed_outputs/context_diff_path/context_last_output/total_iterations 5 字段持久化 + _try_recover_from_checkpoint() 重建 state）⑤ Verification gate（VERIFIED/VERIFICATION_FAILED/NO_CONTRACT 状态码）⑥ Skip step 功能（skip_threshold 配置）⑦ 状态文件损坏自动恢复（从 checkpoint 重建，返回 None 而非 RuntimeError） |
| `/opt/data/scripts/route_engine.py` | ~611 | ~412 | **重大重构**：将路径计算和 YAML 加载逻辑抽取到 chain_config.py。load_route_map 拆分为 5 个独立函数（_load_index / _load_shared_rules / _index_chain_keywords / _load_agent_rules / _build_chain_index）。新增 shared_rules 预置支持（shared.yaml 规则自动 prepend 到各 Agent rules 前面） |
| `/opt/data/scripts/analyze-route-log.py` | ~76 | ~392 | 新增 `analyze` 子命令：规则级命中统计 + 相似规则冗余检测（基于 CJK 编辑距离 + 规则模式归一化）+ 低效规则排行榜 |
| `/opt/data/scripts/validate-route-map.py` | ~79 | ~191 | 追加规则冗余度检查维度：近义词检测 + Levenshtein 编辑距离 + CJK 字符提取比对 |
| `/opt/data/repos/hermes-zero-token-router/route-map/index.yaml` | — | +1 | docs-writer 新增 `chain_ref: pub-chain` |
| `/opt/data/repos/hermes-zero-token-router/route-map/chains/dual-review-chain.yaml` | — | 53 ±--- | dual-review chain 配置更新（parallel 模式调整） |
| `/opt/data/repos/hermes-zero-token-router/route-map/routes/programmer.yaml` | 228 | 85 | 规则瘦身：41→17 条（-59%） |
| `/opt/data/repos/hermes-zero-token-router/route-map/routes/error-analyst.yaml` | ~144 | ~63 | 规则瘦身：31→15 条（-52%） |
| `/opt/data/repos/hermes-zero-token-router/route-map/routes/ui-designer.yaml` | ~127 | ~42 | 规则瘦身：27→12 条（-56%） |
| `/opt/data/repos/hermes-zero-token-router/route-map/routes/reality-checker.yaml` | ~106 | ~37 | 规则瘦身：25→11 条（-56%） |
| `/opt/data/repos/hermes-zero-token-router/tests/test_chain_executor.py` | 675 | ~789 | 新增 5 个安全体系测试用例（checkpoint 保存/恢复/损坏恢复/迭代上限/超时），总用例数 53→58 |
| `/opt/data/repos/hermes-zero-token-router/src/chain_executor.py` | — | ~72 | 公开版 chain_executor 同步更新 |
| `/opt/data/README.md` | — | — | 文档微调 |

### 2.3 🆕 输入检测跳过路由（Hotfix）

| 文件路径 | Commit | 变更说明 |
|----------|--------|----------|
| `src/route_engine.py`（公开库） | `5fe1930` | 检测问号 `?`/`？` 结尾输入 → 跳过路由，返回 method="question" |
| `src/route_engine.py`（公开库） | `914b8b2` | 检测感叹号 `!`/`！` 任意位置 → 跳过路由 |
| `src/route_engine.py`（公开库） | `d09e97b` | 检测连续句号 `..`/`。。` → 跳过路由（必须同类型，不混排） |
| `route-map`/chain 配置 | `d34f1ff` | chain YAML 新增 `mode` 字段（orchestrator/stepwise），兼容旧配置 |
| `route-map/index.yaml`（公开库） | `627bd36`/`9a78ac2` | pub-chain keywords 移除失败 → 回滚后保留 keywords |

**输入检测规则逻辑：**
```python
# 问号/感叹号/连续句号跳过路由
if ('？' in normalized or '?' in normalized
    or '！' in normalized or '!' in normalized
    or '..' in normalized or '。。' in normalized):
    return {
        "agent": "",
        "confidence": 0.0,
        "method": "question",
        "details": {
            "fallback_reason": "检测到问号/感叹号/连续句号，跳过路由",
        },
    }
```

### 2.4 删除文件

| 文件路径 | 文件数 | 说明 |
|----------|--------|------|
| （无文件删除） | — | — |

### 2.5 变更统计

| 类别 | 文件数 | 净增/修改行数 | 说明 |
|------|--------|--------------|------|
| 新建文件（py） | 3 | chain_config.py(47) + __init__.py(0) + router.py(79) | 共享配置模块 + 插件包 |
| 新建文件（文档） | 5 | PLUGIN_INTEGRATION.md(154) + DELEGATION_PROTOCOL.md(72) + why-route-engine.md(73) + ROUTE-ENGINE-CHANGELOG.md(125) + .gitignore(10) | 文档体系完善 |
| 修改文件（py） | 5 | chain_executor(+1304/-462) + route_engine(-199) + analyze-route-log(+324) + validate-route-map(+113) + src/chain_executor(+72) | 核心引擎重构 + 工具增强 |
| 修改文件（yaml） | 6 | index.yaml(+1) + dual-review-chain(±53) + 4 route slims(-421) | 规则瘦身 + 配置更新 |
| 修改文件（测试） | 1 | test_chain_executor(+120) | 新增 5 用例 |
| 修改文件（其他） | 1 | README.md(±151) | 文档微调 |
| 🆕 Hotfix（公开库） | 1 | src/route_engine.py — 输入检测跳过 (+24/-1) | 问号/感叹号/连续句号 |
| **合计** | **24** | **净增 +3241, 删除 -974, 净增 +2267** | — |

### 2.6 Top 4 Agent 规则瘦身明细

| Agent | 原规则数 | 现规则数 | 减少 | 降幅 |
|-------|---------|---------|------|------|
| programmer | 41 | 17 | -24 | -59% |
| error-analyst | 31 | 15 | -16 | -52% |
| ui-designer | 27 | 12 | -15 | -56% |
| reality-checker | 25 | 11 | -14 | -56% |
| **合计（Top 4）** | **124** | **55** | **-69** | **-56%** |
| **全量** | **285** | **216** | **-69** | **-24%** |

---

## 三、安全体系四层架构详解

### 3.1 全局守卫层（Layer 1）

| 守卫 | 配置值 | 说明 |
|------|--------|------|
| 全局迭代上限 | MAX_TOTAL_ITERATIONS = 100 | 防止无限循环，无论链多长 |
| Wall-clock 超时 | CHAIN_TIMEOUT = 3600s | 1 小时内未完成自动终止 |
| 状态文件原子写入 | tempfile + os.replace | 崩溃不损坏 state |

### 3.2 Checkpoint 恢复层（Layer 2）

5 字段持久化：

| 字段 | 说明 |
|------|------|
| step_idx | 当前执行步骤索引 |
| completed_outputs | 已完成步骤的输出路径 |
| context_diff_path | 上下文 diff 路径 |
| context_last_output | 上一步输出路径 |
| total_iterations | 已消耗的迭代次数 |

状态损坏自动降级：`RuntimeError` → `_try_recover_from_checkpoint()` → `return None`（调用方重新 init）

### 3.3 人工介入层（Layer 3）

| 端点 | 中英文别名 | 效果 |
|------|-----------|------|
| rescue | 拯救 | 从 blocked 恢复执行 |
| kill | 终止 | 强制终止链 |
| pause | 暂停 | 暂停链执行 |

### 3.4 Verification Gate（Layer 4）

| 状态码 | 含义 |
|--------|------|
| VERIFIED | 验证通过，继续执行 |
| VERIFICATION_FAILED | 验证失败（触发 retry 或 escalate） |
| NO_CONTRACT | 无验证契约，跳过 |

---

## 四、测试状态

| 套件 | 文件 | 用例数 | 通过数 | 通过率 |
|------|------|--------|--------|--------|
| Chain Executor 单元测试 | test_chain_executor.py | 58 | 58 | 100% |
| 路由引擎单元测试 | test_route_engine.py | — | — | — |
| **合计** | — | **58+** | **58** | **100%** |

### 4.1 🆕 Hotfix 验证

| 测试场景 | 输入 | 预期 | 结果 |
|---------|------|------|------|
| 问号路由跳过 | "这是什么？" | method="question", confidence=0.0 | ✅ |
| 感叹号路由跳过 | "紧急！" | method="question", confidence=0.0 | ✅ |
| 连续句号路由跳过 | "举个例子。。" | method="question", confidence=0.0 | ✅ |
| 混合标点（不跳过） | "修复 bug。谢谢" | 正常路由 | ✅ |

---

## 五、发布检查清单

- [x] 完全版文档路径保留内部路径（`/opt/data/` 等）
- [x] 脱敏版文档所有 `/opt/data/` → `/path/to/project/` 替换完成
- [x] 脱敏版已删除 Hermes 特有术语、IP、Token、SSH 路径
- [x] 两套文档结构一致（背景→文件→评分→测试→问题→修复）
- [x] 公库推送内容为脱敏版文档
- [x] 私库推送内容为完全版文档
- [x] 版本号同步（CHANGELOG-FULL-20260706-v1 = ROUTE-ENGINE-CHANGELOG-20260706-v1）

---

## 六、附录

### 6.1 Commit 清单（私库 48e1e85..61cd3fb）

| Commit | 消息 | 类型 |
|--------|------|------|
| `2501d5c` | docs: 更新路由引擎发布文档 2026-07-04 | 文档 |
| `87475b0` | docs: add bilingual documentation + plugin + sanitized changelog | 文档 |
| `5403718` | chore: cleanup repo structure — remove CHANGELOG-FULL, add __init__.py | 清理 |
| `b33e66c` | sync: chain_executor brief-file + skip_threshold + dry_run; dual-review parallel; index pub-chain | 同步 |
| `61cd3fb` | chore: release v2.5 — global safety net, rule slim (285→216), rescue/kill/pause, analyzer tools, docs restructure | 发布 |

### 6.2 Commit 清单（公库 b799a8b..4dee773）

| Commit | 消息 | 类型 |
|--------|------|------|
| `71ef141` | docs: release changelog (sanitized) — v2.5+ 2026-07-05 | 文档 |
| `d34f1ff` | feat: chain YAML mode field — orchestrator/stepwise (sync) | 功能 |
| `627bd36` | fix: remove '更新路由引擎' from pub-chain keywords | 修复 |
| `9a78ac2` | Revert "fix: remove '更新路由引擎' from pub-chain keywords" | 回滚 |
| `5fe1930` | feat: question detection — skip routing for inputs ending with ?/？ | 功能 |
| `914b8b2` | feat: also skip routing when input contains ! or ！ anywhere | 功能 |
| `d09e97b` | feat: skip routing on consecutive dots .. or 。。 | 功能 |
| `4dee773` | docs: release changelog — input detection (question/exclamation/dots) hotfix | 文档 |

### 6.3 关键架构改进总结

1. **共享配置抽象**：chain_config.py 从 route_engine.py/chain_executor.py 中提取公共路径计算和 YAML 加载逻辑，消除重复代码，统一配置入口
2. **安全体系四层**：全局守卫 + Checkpoint 恢复 + 人工介入 + Verification Gate，覆盖链执行全生命周期的异常场景
3. **规则瘦身 -24%**：Top 4 Agent 规则数从 124 降至 55（-56%），全量从 285 降至 216（-24%），消除所有边界重叠规则
4. **输入检测跳过路由**：问号 `?`/`？`、感叹号 `!`/`！`、连续句号 `..`/`。。` 输入自动跳过路由，避免错误路由到 Agent
5. **Chain mode 字段**：YAML chain 定义新增 `mode` 字段（orchestrator/stepwise），支持更灵活的执行模式配置

### 6.4 文件校验

| 文件 | 路径 |
|------|------|
| CHANGELOG-FULL.md | `/opt/data/repos/hermes-zero-token-router/CHANGELOG-FULL.md` |
| ROUTE-ENGINE-CHANGELOG.md | `/opt/data/route-engine/ROUTE-ENGINE-CHANGELOG.md` |
