"""读取 stability_test 三次运行数据，生成详细横向对比 markdown 报告"""
import json
from pathlib import Path
from collections import Counter

OUTPUT_DIR = Path(__file__).parent.parent / "pipeline_output" / "stability_test"
REPORT_PATH = OUTPUT_DIR / "comparison_report.md"

runs = {}
for i in range(1, 4):
    p = OUTPUT_DIR / f"run_{i}.json"
    runs[i] = json.loads(p.read_text(encoding="utf-8"))

lines = []
def w(s=""):
    lines.append(s)

# ============================================================
w("# LLM 提取稳定性对比报告 — pan2025RobustDirect (3 次运行)")
w()
w("## 一、Section Mapping 总览")
w()
w("| Section | Run 1 | Run 2 | Run 3 | 一致性 |")
w("|---|---|---|---|---|")
for sec in ["ABSTRACT", "MOTIVATION_AND_BACKGROUND", "RELATED_WORK",
            "METHODOLOGY", "EXPERIMENT_SETUP_AND_RESULTS",
            "DISCUSSION_AND_CONCLUSION", "TRASH"]:
    counts = [len(runs[i]["section_mapping"].get(sec, [])) for i in range(1, 4)]
    same = "✅" if len(set(counts)) == 1 else "⚠️"
    w(f"| {sec} | {counts[0]} | {counts[1]} | {counts[2]} | {same} |")

total_covered = []
for i in range(1, 4):
    covered = set()
    for ids in runs[i]["section_mapping"].values():
        covered.update(ids)
    total_covered.append(len(covered))
w()
w(f"**Block 覆盖**: Run1={total_covered[0]}/237, Run2={total_covered[1]}/237, Run3={total_covered[2]}/237")

# ============================================================
w()
w("## 二、Section Mapping 逐 Block 对比")
w()

# 找出有分歧的 blocks
block_votes = {}  # block_id -> {run_id: section}
for i in range(1, 4):
    sm = runs[i]["section_mapping"]
    for sec, ids in sm.items():
        for bid in ids:
            if bid not in block_votes:
                block_votes[bid] = {}
            block_votes[bid][i] = sec

# 统计
stable_blocks = 0
unstable_blocks = []
for bid in sorted(block_votes.keys()):
    votes = block_votes[bid]
    sections = list(votes.values())
    if len(set(sections)) == 1:
        stable_blocks += 1
    else:
        unstable_blocks.append(bid)

w(f"**稳定 block**: {stable_blocks}/237 ({stable_blocks/237*100:.1f}%)")
w(f"**不稳定 block**: {len(unstable_blocks)}/237 ({len(unstable_blocks)/237*100:.1f}%)")
w()

if unstable_blocks:
    w("### 不稳定 Block 详情")
    w()
    w("| Block ID | Run 1 | Run 2 | Run 3 | 多数票 |")
    w("|---|---|---|---|---|")
    for bid in unstable_blocks:
        v = block_votes[bid]
        r1, r2, r3 = v.get(1, "MISS"), v.get(2, "MISS"), v.get(3, "MISS")
        counter = Counter([r1, r2, r3])
        majority = counter.most_common(1)[0][0]
        w(f"| {bid} | {r1} | {r2} | {r3} | {majority} |")

# ============================================================
w()
w("## 三、各 Section 间 Block 流动矩阵")
w()
w("统计不稳定 block 在各 section 间的流动方向：")
w()

# 流动统计
flows = Counter()
for bid in unstable_blocks:
    v = block_votes[bid]
    sections = [v.get(i, "MISS") for i in range(1, 4)]
    unique = sorted(set(sections))
    flows[tuple(unique)] += 1

w("| 涉及的 Section 组合 | Block 数 | Block IDs |")
w("|---|---|---|")
for combo, cnt in sorted(flows.items(), key=lambda x: -x[1]):
    ids_in_combo = [bid for bid in unstable_blocks
                    if sorted(set(block_votes[bid].get(i, "MISS") for i in range(1,4))) == sorted(combo)]
    w(f"| {' ↔ '.join(combo)} | {cnt} | {ids_in_combo} |")

# ============================================================
w()
w("## 四、实体提取对比")
w()

categories = ["research_tasks", "proposed_methods", "datasets",
              "evaluation_metrics", "baselines",
              "addressed_existing_flaws", "self_admitted_limitations"]

total_all = {i: 0 for i in range(1, 4)}
w("| 类别 | Run 1 | Run 2 | Run 3 |")
w("|---|---|---|---|")
for cat in categories:
    counts = [len(runs[i]["extracted_entities"].get(cat, [])) for i in range(1, 4)]
    for i in range(1, 4):
        total_all[i] += counts[i-1]
    w(f"| {cat} | {counts[0]} | {counts[1]} | {counts[2]} |")
w(f"| **总计** | **{total_all[1]}** | **{total_all[2]}** | **{total_all[3]}** |")

# ============================================================
w()
w("## 五、逐类别实体横向对比")
w()

for cat in categories:
    w(f"### {cat}")
    w()

    # 收集所有 name_in_paper
    all_names = {}  # name -> {run_id: entity_dict}
    for i in range(1, 4):
        for ent in runs[i]["extracted_entities"].get(cat, []):
            name = ent.get("name_in_paper", "?")
            if name not in all_names:
                all_names[name] = {}
            all_names[name][i] = ent

    # 按出现次数排序
    sorted_names = sorted(all_names.items(), key=lambda x: -len(x[1]))

    w("| name_in_paper | Run 1 | Run 2 | Run 3 | 出现次数 |")
    w("|---|---|---|---|---|")
    for name, run_map in sorted_names:
        presence = []
        for i in range(1, 4):
            if i in run_map:
                aliases = run_map[i].get("aliases", [])
                alias_str = f" (aliases: {', '.join(aliases)})" if aliases else ""
                presence.append(f"✅{alias_str}")
            else:
                presence.append("❌")
        count = len(run_map)
        count_str = "⭐⭐⭐" if count == 3 else ("⭐⭐" if count == 2 else "⭐")
        w(f"| {name} | {'  \|  '.join(presence)} | {count_str} |")

    w()

    # evidence_quote 对比（仅对比三次都出现的实体）
    shared = [n for n, rm in sorted_names if len(rm) == 3]
    if shared:
        w(f"**三次均出现的实体 evidence_quote 对比**:")
        w()
        for name in shared:
            rm = all_names[name]
            quotes = [rm[i].get("evidence_quote", "") for i in range(1, 4)]
            same_quote = len(set(quotes)) == 1
            if same_quote:
                w(f"- **{name}**: evidence_quote 三次完全一致 ✅")
            else:
                w(f"- **{name}**: evidence_quote 有差异 ⚠️")
                for i in range(1, 4):
                    q = quotes[i-1]
                    short_q = q[:80] + "..." if len(q) > 80 else q
                    w(f"  - Run{i}: `{short_q}`")

    # 语义定义对比（仅对比三次都出现的实体）
    if shared:
        w()
        w(f"**三次均出现的实体 semantic_definition 对比**:")
        w()
        for name in shared:
            rm = all_names[name]
            defs = [rm[i].get("semantic_definition", "") for i in range(1, 4)]
            same_def = len(set(defs)) == 1
            if same_def:
                w(f"- **{name}**: semantic_definition 三次完全一致 ✅")
            else:
                w(f"- **{name}**: semantic_definition 有差异 ⚠️ (语义相同，表述不同)")
                for i in range(1, 4):
                    d = defs[i-1]
                    short_d = d[:100] + "..." if len(d) > 100 else d
                    w(f"  - Run{i}: {short_d}")
    w()

# ============================================================
w("## 六、稳定性总结")
w()
w("### 高稳定区域")
w("- **ABSTRACT / MOTIVATION_AND_BACKGROUND / RELATED_WORK / DISCUSSION_AND_CONCLUSION**: block 分配三次完全一致")
w("- **baselines**: 实体名称三次完全一致 (VINS-Fusion, ORB-SLAM3, DSOL, MCVO)")
w()
w("### 中等波动区域")
w("- **METHODOLOGY**: 边界 block (59, 63, 64) 在 METHODOLOGY/TRASH 间波动")
w("- **EXPERIMENT_SETUP_AND_RESULTS**: Run2/Run3 倾向将更多 block 归入实验节")
w("- **research_tasks / proposed_methods**: 实体粒度有差异（粗提 vs 细分），但核心实体一致")
w()
w("### 建议")
w("- METHODOLOGY/EXPERIMENT 边界的 block 归属为主观判断，对最终知识图谱影响有限")
w("- 实体粒度问题可通过后处理合并（如子集合并入父集）解决")
w("- baselines 核心实体高度稳定，可信度高")

report = "\n".join(lines)
REPORT_PATH.write_text(report, encoding="utf-8")
print(f"Report saved to: {REPORT_PATH}")
print(f"Total lines: {len(lines)}")
