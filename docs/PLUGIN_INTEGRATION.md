# 插件集成：route-router

> 将 core route_engine.py 嵌入 Hermes Agent 的 plugin 层，实现零 token 预路由

## 架构概览

```
用户消息
    │
    ▼
┌──────────────────────────────────────────────────┐
│  Hermes Agent 消息管道                            │
│                                                  │
│  ┌──────────────────────────────────────────┐    │
│  │  pre_llm_call 钩子                        │    │
│  │  (route-router 插件)                      │    │
│  │                                           │    │
│  │  router.run_route_engine(msg) ────────────│──────→ route_engine.py
│  │        │                                  │    │     (缓存预热)
│  │        ▼                                  │    │
│  │  注入上下文:                               │    │
│  │  [路由引擎预判]                            │    │
│  │  目标 Agent: programmer                   │    │
│  │  置信度: 0.8                              │    │
│  │  匹配规则: ['实现']                        │    │
│  │  Chain 摘要: ...                          │    │
│  └──────────────────────────────────────────┘    │
│                                                  │
│  ┌──────────────────────────────────────────┐    │
│  │  LLM 收到注入后的上下文                     │    │
│  │  → 按注入结果 delegate_task               │    │
│  └──────────────────────────────────────────┘    │
│                                                  │
└──────────────────────────────────────────────────┘
```

## 插件结构

```
plugins/route-router/
├── __init__.py      # pre_llm_call 钩子注册 + 上下文注入
└── router.py        # 惰性导入 + 预热 route_engine.py
```

### __init__.py — 钩子注册

```python
def register(ctx):
    """Hermes 插件入口：注册 pre_llm_call 钩子"""
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
```

`_on_pre_llm_call` 在每轮 LLM 调用前触发，接收当前用户消息，返回注入上下文。

### router.py — 惰性加载

```python
_route_engine = None   # 惰性导入
_warmup_ok = False     # 预热状态

def _ensure_route_engine():
    """首次调用时导入 route_engine + 预热缓存"""
    if _route_engine is not None:
        return True
    try:
        import route_engine as _re
        _route_engine = _re
        _warmup_route_engine()
        return True
    except ImportError as exc:
        logger.error("route_engine 导入失败: %s", exc)
        return False
```

关键设计点：
- **惰性导入**：模块级不直接 `import route_engine`，首次调用时才导入
- **预热标志**：`_warmup_ok` 记录预热状态，失败时每次调用自动重试
- **异常隔离**：导入异常不阻断插件加载，仅禁用路由功能

## 上下文注入格式

### 普通路由（置信度 < 2.0）

```
[路由引擎预判]
目标 Agent: programmer
置信度: 0.8
匹配规则: ['实现', '编码']
Chain 摘要: programmer→TDD 实现; error-analyst→spec 合规评审; ...
推荐技能: [test-driven-development, receiving-code-review]
```

### 强制路由（置信度 ≥ 2.0）

```
[路由引擎预判 — 强制路由]
目标 Agent: ui-designer
置信度: 2.2
匹配规则: ['界面', '视觉']

【指令】立即执行 delegate_task，不要分析、不要质疑、不要判断路由结果。
```

### 无匹配（unrouted）

```
[路由引擎] 当前消息无匹配路由 (method=unrouted)
```

### Chain JSON 传递

插件除了注入文本上下文，还会额外传递 `chain_json` 字段（当路由结果附带 chain 时）：

```json
{
  "context": "[路由引擎预判]\n目标 Agent: programmer\n...",
  "chain_json": "[{\"agent\":\"programmer\",\"goal\":\"TDD 实现 + self-review\"},...]"
}
```

主 Agent 上下文中能同时读到可读的 `Chain 摘要:` 行和结构化的 `chain_json` 字段。

## 强制路由机制

当 route_engine 返回的置信度 ≥ `FORCE_ROUTE_THRESHOLD`（2.0）时：

1. 注入标题变为 `[路由引擎预判 — 强制路由]`
2. 追加一行 `【指令】立即执行 delegate_task...`
3. 主 Agent 的 SOUL.md 中对应指令要求**立即执行，无例外**

这种机制是**软强制**而非硬拦截：不阻止 LLM 生成其他内容，但通过明确的指令措辞让主 Agent 直接遵从。

## 性能

| 操作 | 耗时 |
|------|------|
| 首次调用（导入 + 预热） | ~150ms |
| 后续每次路由 | ~0.4ms |
| 子进程开销（旧版） | ~50ms |
| 注入上下文大小 | ~200-500 bytes |

比旧版 subprocess 方案快 100+ 倍（0.4ms vs 50ms），且零 token 消耗。

## SOUL.md 路由处理指令

主 Agent 的 SOUL.md 中对应指令：

```
直接按注入结果委派：
- 目标 Agent + 置信度 → 立即 delegate_task，不分析不质疑
- [路由引擎预判 — 强制路由]（置信度 ≥ 2.0）→ 立即执行，无例外
- unrouted（无匹配）→ 自行判断路由或请求用户明确意图
- 无注入（插件异常）→ 手动判定兜底
```
