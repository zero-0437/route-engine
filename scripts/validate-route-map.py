#!/usr/bin/env python3
"""validate-route-map.py — route-map 目录结构验证器（12 维审计）
与 validate-skill-map.py 对齐：退出码 0=OK, 1=WARN, 2=ERR

支持 --fix 半自动规则去重合并（dry-run 默认，--apply 实际写入）
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from collections import defaultdict

import yaml

ROUTE_MAP_DIR = os.path.join(os.path.dirname(__file__), "..", "route-map")
SKILL_MAP = os.path.join(os.path.dirname(__file__), "..", "skill-map.yaml")
INDEX_FILE = os.path.join(ROUTE_MAP_DIR, "index.yaml")
ROUTES_DIR = os.path.join(ROUTE_MAP_DIR, "routes")

# ── 近义词检测辅助函数（与 analyze-route-log.py 共享逻辑） ──
_CJK_RE = re.compile(r'[\u4e00-\u9fff]+')


def _extract_cjk_chars(text: str) -> str:
    return "".join(_CJK_RE.findall(text))


def _levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[-1]


def _is_typo_variant(a: str, b: str) -> bool:
    a_cjk = _extract_cjk_chars(a)
    b_cjk = _extract_cjk_chars(b)
    if not a_cjk or not b_cjk:
        return False
    if abs(len(a_cjk) - len(b_cjk)) > 1:
        return False
    if len(a_cjk) < 2 or len(b_cjk) < 2:
        return _levenshtein(a_cjk, b_cjk) <= 1
    overlap = len(set(a_cjk) & set(b_cjk))
    min_len = min(len(set(a_cjk)), len(set(b_cjk)))
    if min_len == 0:
        return False
    return _levenshtein(a_cjk, b_cjk) <= 2 and overlap / min_len >= 0.5


def _is_near_synonym(a: str, b: str) -> bool:
    a_cjk = _extract_cjk_chars(a)
    b_cjk = _extract_cjk_chars(b)
    if not a_cjk or not b_cjk:
        return False

    # 短文本硬约束：CJK ≤ 3 字符时不判定为近义词
    if len(a_cjk) <= 3 or len(b_cjk) <= 3:
        return False

    if _is_typo_variant(a, b):
        return True
    if a_cjk in b_cjk or b_cjk in a_cjk:
        if a_cjk != b_cjk:
            return True
    set_a = set(a_cjk)
    set_b = set(b_cjk)
    if not set_a or not set_b:
        return False
    jaccard = len(set_a & set_b) / len(set_a | set_b)
    len_ratio = min(len(a_cjk), len(b_cjk)) / max(len(a_cjk), len(b_cjk))
    return jaccard >= 0.5 and len_ratio >= 0.5 and len(a_cjk) >= 2 and len(b_cjk) >= 2


def _find_redundant_pairs(route_files: dict) -> list:
    """扫描所有 route 文件，返回冗余规则对列表，已确定 keep/drop。

    每项: (fname, keep_idx, drop_idx, keep_pattern, drop_pattern, weight, skills)
    keep_idx: 应保留规则的下标（longer/more-specific pattern）
    drop_idx: 应删除规则的下标（shorter/less-specific pattern）
    """
    pairs = []
    for fname, data in route_files.items():
        if fname == "shared.yaml":
            continue
        rules = data.get("rules", [])
        # 按 (weight, skills_tuple) 分组
        groups = {}
        for i, rule in enumerate(rules):
            w = rule.get("weight", 0)
            sk = tuple(sorted(rule.get("skills", []) or []))
            key = (w, sk)
            groups.setdefault(key, []).append((i, rule))
        for key, group in groups.items():
            if len(group) < 2:
                continue
            for a_idx, a_rule in group:
                for b_idx, b_rule in group:
                    if a_idx >= b_idx:
                        continue
                    pa = a_rule.get("pattern", "")
                    pb = b_rule.get("pattern", "")
                    if pa == pb:
                        continue
                    if _is_near_synonym(pa, pb):
                        # 保留 longer/more-specific pattern
                        # 长度相同保留索引更靠前的
                        if len(pa) > len(pb) or (len(pa) == len(pb) and a_idx < b_idx):
                            keep_idx, drop_idx = a_idx, b_idx
                            keep_pattern, drop_pattern = pa, pb
                        else:
                            keep_idx, drop_idx = b_idx, a_idx
                            keep_pattern, drop_pattern = pb, pa
                        pairs.append((
                            fname, keep_idx, drop_idx,
                            keep_pattern, drop_pattern,
                            key[0], list(key[1]),
                        ))
    return pairs


def _dedup_and_generate_patch(route_files: dict, redundant_pairs: list) -> list:
    """对冗余规则对做去重合并，生成 patch 条目。

    返回 list[dict]，每项:
    {"file": str, "action": "drop_rule", "index": int, "rule": dict}
    """
    patches = []
    by_file = defaultdict(list)
    for pair in redundant_pairs:
        fname = pair[0]
        by_file[fname].append(pair)

    for fname, file_pairs in by_file.items():
        data = route_files.get(fname)
        if data is None:
            continue
        rules = data.get("rules", [])
        drop_indices = set()
        for pair in file_pairs:
            drop_indices.add(pair[2])  # drop_idx
        for idx in sorted(drop_indices, reverse=True):
            patches.append({
                "file": fname,
                "action": "drop_rule",
                "index": idx,
                "rule": copy.deepcopy(rules[idx]),
            })
    return patches


def _apply_patches(route_files: dict, patches: list) -> dict:
    """将 patches 应用到 route_files（原地修改）。"""
    by_file = defaultdict(list)
    for p in patches:
        by_file[p["file"]].append(p)

    for fname, file_patches in by_file.items():
        data = route_files.get(fname)
        if data is None:
            continue
        rules = data.get("rules", [])
        indices = sorted(set(p["index"] for p in file_patches), reverse=True)
        for idx in indices:
            if 0 <= idx < len(rules):
                del rules[idx]
        data["rules"] = rules

    return route_files


def err(msg, field=""):
    return {"field": field, "message": msg}


def warn(msg, field=""):
    return {"field": field, "message": msg}


def info(msg, field=""):
    return {"field": field, "message": msg}


def validate():
    errors = []
    warnings = []
    infos = []

    # 1. index.yaml YAML 合法性
    if not os.path.exists(INDEX_FILE):
        errors.append(err("index.yaml 不存在", "index.yaml"))
    else:
        try:
            with open(INDEX_FILE) as f:
                index = yaml.safe_load(f)
            if not isinstance(index, dict):
                errors.append(err("index.yaml 不是有效 dict", "index.yaml"))
        except yaml.YAMLError as e:
            errors.append(err(f"index.yaml YAML 语法错误: {e}", "index.yaml"))

    # 2. 每个 routes/*.yaml YAML 合法性
    agents_map = index.get("agents", {})
    route_files = {}
    for fname in os.listdir(ROUTES_DIR):
        if fname.endswith(".yaml"):
            fpath = os.path.join(ROUTES_DIR, fname)
            try:
                with open(fpath) as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    errors.append(err(f"{fname} 不是有效 dict", fname))
                route_files[fname] = data
            except yaml.YAMLError as e:
                errors.append(err(f"{fname} YAML 语法错误: {e}", fname))

    # 3. agents 映射完整性
    expected_agents = {"pm-agent", "programmer", "error-analyst", "data-analyst",
                       "ui-designer", "document-processor", "file-ops",
                       "synology-helper", "memory-agent", "prompt-engineer",
                       "reality-checker", "docs-writer"}
    indexed_agents = set(agents_map.keys())
    missing = expected_agents - indexed_agents
    extra = indexed_agents - expected_agents
    if missing:
        errors.append(err(f"index.yaml 缺少 Agent: {missing}", "agents"))
    if extra:
        warnings.append(warn(f"index.yaml 有未预期的 Agent: {extra}", "agents"))

    # 4. 交叉验证：index.yaml 引用的 file 路径必须存在
    for name, info in agents_map.items():
        fpath = os.path.join(ROUTES_DIR, info.get("file", "").replace("routes/", ""))
        if not os.path.exists(fpath):
            errors.append(err(f"Agent '{name}' 引用的文件不存在: {info.get('file')}", name))
        elif "file" not in info:
            errors.append(err(f"Agent '{name}' 缺少 file 字段", name))

    # 5. 逆向检查：routes/ 下每个 .yaml 必须在 index.yaml agents 中有对应
    indexed_files = {info.get("file", "").replace("routes/", "") for info in agents_map.values()}
    for fname in route_files:
        if fname not in indexed_files and fname != "shared.yaml":
            warnings.append(warn(f"routes/{fname} 在 index.yaml 中无对应 agent", fname))

    # 6. schema_version 一致性
    try:
        with open(SKILL_MAP) as f:
            skill_map = yaml.safe_load(f)
        sv_index = index.get("schema_version")
        sv_skill = skill_map.get("schema_version")
        if sv_index and sv_skill and sv_index != sv_skill:
            errors.append(err(
                f"schema_version 不一致: index.yaml={sv_index}, skill-map.yaml={sv_skill}",
                "schema_version"))
    except (yaml.YAMLError, FileNotFoundError):
        warnings.append(warn("无法读取 skill-map.yaml，跳过 schema_version 检查"))

    # 7. rules[] 结构完整性
    for fname, data in route_files.items():
        if fname == "shared.yaml":
            continue
        rules = data.get("rules", [])
        if not isinstance(rules, list):
            errors.append(err(f"{fname}: rules 不是数组", fname))
            continue
        for i, rule in enumerate(rules):
            for field in ("type", "pattern", "weight"):
                if field not in rule:
                    errors.append(err(f"{fname} rule[{i}]: 缺少 '{field}'", fname))

    # 8. regex pattern 语法检查
    for fname, data in route_files.items():
        if fname == "shared.yaml":
            continue
        for i, rule in enumerate(data.get("rules", [])):
            if rule.get("type") == "regex":
                try:
                    re.compile(rule["pattern"])
                except re.error as e:
                    errors.append(err(f"{fname} rule[{i}] regex 语法错误: {e}", fname))

    # 9. 每个 Agent 至少 2 条正权重规则
    for fname, data in route_files.items():
        if fname == "shared.yaml":
            continue
        agent_name = data.get("agent", fname.replace(".yaml", ""))
        pos_rules = [r for r in data.get("rules", []) if r.get("weight", 0) > 0]
        if len(pos_rules) < 2:
            errors.append(err(f"Agent '{agent_name}' 正权重规则不足 ({len(pos_rules)} < 2)", agent_name))

    # 10. weight 范围检查（允许负权重用于误匹配防护，须在 -2.0~2.0 之间）
    for fname, data in route_files.items():
        if fname == "shared.yaml":
            continue
        for i, rule in enumerate(data.get("rules", [])):
            w = rule.get("weight", 0)
            if not isinstance(w, (int, float)):
                errors.append(err(f"{fname} rule[{i}] weight 不是数字: {w}", fname))
            elif w < -2.0 or w > 2.0:
                errors.append(err(f"{fname} rule[{i}] weight 越界: {w} (允许 -2.0~2.0)", fname))

    # 11. agent 名一致性
    for fname, data in route_files.items():
        if fname == "shared.yaml":
            continue
        expected_name = fname.replace(".yaml", "")
        actual_name = data.get("agent", "")
        if actual_name and actual_name != expected_name:
            errors.append(err(
                f"{fname}: agent 名 '{actual_name}' 与文件名 '{expected_name}' 不一致",
                fname))

    # 12. 重复规则检测
    for fname, data in route_files.items():
        if fname == "shared.yaml":
            continue
        seen = {}
        for i, rule in enumerate(data.get("rules", [])):
            key = (rule.get("type", ""), rule.get("pattern", ""))
            if key in seen:
                warnings.append(warn(
                    f"{fname}: rule[{i}] 与 rule[{seen[key]}] 重复 (type={key[0]}, pattern={key[1]})",
                    fname))
            seen[key] = i

    # 13. 规则冗余度检查（同 weight + 同 skills + 近义词）
    _redundant_pairs = _find_redundant_pairs(route_files)

    if _redundant_pairs:
        redund_count = len(_redundant_pairs)
        total_rule_pairs = sum(
            len(data.get("rules", [])) * (len(data.get("rules", [])) - 1) // 2
            for fname, data in route_files.items()
            if fname != "shared.yaml"
        )
        ratio = redund_count / max(total_rule_pairs, 1) * 100
        warnings.append(warn(
            f"规则冗余度检查: 发现 {redund_count} 对冗余规则（同weight+同skills+近义词），"
            f"占规则对总数 {total_rule_pairs} 的 {ratio:.1f}%",
            "redundancy"))
        for pair in _redundant_pairs:
            fname, keep_idx, drop_idx, keep_pattern, drop_pattern, w, skills = pair
            warnings.append(warn(
                f"  {fname}: rule[{drop_idx}] (\"{drop_pattern}\" weight:{w}) → 合并至 "
                f"rule[{keep_idx}] (\"{keep_pattern}\" weight:{w}) 同skills={skills}",
                fname))

    # 判定状态
    if errors:
        status = "ERR"
    elif warnings:
        status = "WARN"
    else:
        status = "OK"

    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "info": infos,
        "_redundant_pairs": _redundant_pairs,
        "_route_files": route_files,
    }


def _output_fix_suggestions(result: dict, apply_changes: bool):
    """输出 --fix 去重合并建议，可选 --apply 实际写入文件。"""
    pairs = result.get("_redundant_pairs", [])
    route_files = result.get("_route_files", {})

    if not pairs:
        print("# No redundant pairs found -- nothing to merge.")
        return

    mode = "applying changes" if apply_changes else "dry-run (no files modified)"
    print(f"# Semi-Auto Dedup Merge -- {mode}")

    if not apply_changes:
        print("# To apply: re-run with --fix --apply")

    print(f"\nTotal redundant pairs found: {len(pairs)}")

    patches = _dedup_and_generate_patch(route_files, pairs)

    if not patches:
        print("\nNo patches generated.")
        return

    total_actions = len(pairs)
    print(f"\n## Suggested merges ({total_actions} actions):\n")

    for action_num, pair in enumerate(pairs, 1):
        fname, keep_idx, drop_idx, keep_pattern, drop_pattern, w, skills = pair
        print(f"### {action_num}. [{fname}] rule[{keep_idx}] <- rule[{drop_idx}]")
        print(f"- Keep: \"{keep_pattern}\" (rule[{keep_idx}], weight: {w}, skills: {skills})")
        print(f"- Drop: \"{drop_pattern}\" (rule[{drop_idx}], weight: {w}, skills: {skills})")
        print("- Reason: Near-synonym, same weight+skills")

    # 同一 drop 索引可能被多对冗余指向，实际唯一操作数可能更少
    if total_actions != len(patches):
        print(f"# Note: {len(patches)} unique rule drops needed ({total_actions - len(patches)} pairs share the same drop target)")

    if apply_changes:
        _apply_patches(route_files, patches)
        by_file = defaultdict(list)
        for p in patches:
            by_file[p["file"]].append(p)
        written = 0
        for fname, data in route_files.items():
            if fname == "shared.yaml":
                continue
            file_patches = [p for p in patches if p["file"] == fname]
            if not file_patches:
                continue
            fpath = os.path.join(ROUTES_DIR, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False,
                          sort_keys=False, width=120, indent=2)
            written += 1

        print(f"\n## Applied: {len(patches)} rules dropped across {written} files\n")
        for fname, file_patches in by_file.items():
            dropped_indices = sorted(p["index"] for p in file_patches)
            print(f"  [{fname}] dropped rule indices: {dropped_indices}")

        print("\nWARNING: Changes written to disk. Re-run without --fix to re-validate.")
    else:
        print(f"\n## To apply: pipe this output to a merge script or apply manually")
        print(f"   re-run with: --fix --apply")


def main():
    parser = argparse.ArgumentParser(
        description="route-map directory structure validator (12-dim audit)")
    parser.add_argument("--fix", action="store_true",
                        help="semi-auto rule dedup merge: detect redundant rules and output merge suggestions (dry-run by default, no files modified)")
    parser.add_argument("--apply", action="store_true",
                        help="apply --fix suggestions to actually modify files (only effective with --fix)")
    args = parser.parse_args()

    result = validate()

    if args.fix:
        _output_fix_suggestions(result, apply_changes=args.apply)
        print("\n---\n")

    output = {k: v for k, v in result.items() if not k.startswith("_")}
    print(json.dumps(output, ensure_ascii=False, indent=2))

    if result["status"] == "ERR":
        return 2
    elif result["status"] == "WARN":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
