# 论文知识库工作手册

> 本文档旨在让一个无上下文的 Agent 从零理解和操作论文知识库的全部流程。
> 最后更新：2026-05-15

---

## 1. 项目总览

### 1.1 目标

从 Zotero 文献库出发，经过 PDF 解析 → Markdown 清洗 → LLM 语义提取 → 零幻觉校验 → 结构化 JSON，最终导入 Neo4j 图数据库，构建论文知识图谱的 **Bronze Layer（物理提及层）**。

### 1.2 数据流全景

```
Zotero PDF ──①──> full.md ──②──> full_clear.md ──③──> {cite_key}.json ──④──> Neo4j
```

| 步骤 | 脚本 | 输入 | 输出 | 说明 |
|------|------|------|------|------|
| ① PDF 解析 | `mineru_zotero_parser.py` | Zotero 中的 PDF | `full.md`, `metadata.json` | 调用 MinerU API |
| ② Markdown 清洗 | `new_paper_pipeline.py`（Phase 0） | `full.md` | `full_clear_table.md` → `full_clear.md` | 提取 HTML 表格、删除 `<details>` 块 |
| ③ LLM 管线 | `new_paper_pipeline.py`（Phase 1-4） | `full_clear.md` | `{cite_key}.json`, `{cite_key}_llm_raw.json` | 切块→LLM→校验→拼装 |
| ④ Neo4j 入库 | `neo4j_bronze_ingestor.py` | `{cite_key}.json` | Neo4j 图数据 | Bronze Layer |

---

## 2. 目录结构

```
D:\tools\knowledge_base\
├── script\                          # 所有脚本
│   ├── config.json                  # 全局配置（Zotero/MinerU/LLM 密钥）
│   ├── mineru_zotero_parser.py      # 步骤①：PDF 解析
│   ├── new_paper_pipeline.py        # 步骤②③：清洗 + LLM 管线
│   ├── neo4j_bronze_ingestor.py     # 步骤④：Neo4j 入库
│   └── pipeline\                    # 管线模块
│       ├── chunker.py               # Phase 1：物理切块
│       ├── extractor.py             # Phase 2：LLM 语义提取（含 SYSTEM_PROMPT）
│       ├── verifier.py              # Phase 3：零幻觉校验
│       ├── assembler.py             # Phase 4：结果拼装
│       ├── clear_table.py           # Phase 0a：HTML 表格提取
│       ├── clear_details.py         # Phase 0b：details 块清理
│       └── utils.py                 # 共享工具函数
├── zotero_data\                     # 论文数据目录
│   └── {cite_key}\                  # 每篇论文一个目录
│       ├── metadata.json            # Zotero 元数据（由步骤①生成）
│       ├── full.md                  # MinerU 解析原始 Markdown（由步骤①生成）
│       ├── full_clear_table.md      # 表格清理后（步骤②中间产物）
│       ├── full_clear.md            # 最终清洗后（步骤②产物，步骤③输入）
│       ├── full_before_llm.md       # LLM 前的切块拼接文本
│       ├── {cite_key}.json          # 最终结构化结果（步骤③产物，步骤④输入）
│       ├── {cite_key}_llm_raw.json  # LLM 原始输出
│       ├── images\                  # 论文图片
│       └── table\                   # 提取的 HTML 表格
└── Neo4j_工作记录_新机.md           # 本文档
```

---

## 3. 环境依赖

### 3.1 软件环境

| 组件 | 路径/版本 | 说明 |
|------|-----------|------|
| Python | 系统 Python（3.12+） | 脚本运行环境 |
| Neo4j Desktop | `D:\neo4j` | Enterprise 2026.03.1 |
| Neo4j 数据库 | DBMS `dbms-2632f9e4-dd1f-4f23-8088-9ac29f6c75ce` | 路径：`D:\neo4j\Data\Application\Data\dbmss\dbms-2632f9e4-dd1f-4f23-8088-9ac29f6c75ce` |
| Zotero | 本地运行 | 提供本地 API `http://localhost:23119` |

### 3.2 Python 依赖

```
openai          # DeepSeek API 调用
neo4j==6.2.0    # Neo4j Python Driver
thefuzz         # 模糊匹配（verifier 用）
rich            # 并行模式进度条
requests        # Zotero / MinerU API
```

### 3.3 config.json

位置：`D:\tools\knowledge_base\script\config.json`

关键字段：
- `llm_key`：DeepSeek API 密钥（LLM 管线使用）
- `zotero_api_base`：Zotero 本地 API 地址
- `zotero_data_dir`：Zotero 存储目录
- `mineru_token_file`：MinerU API Token 文件路径
- `output_dir`：论文数据输出目录

### 3.4 Neo4j 连接信息

| 参数 | 值 |
|------|----|
| URI | `neo4j://127.0.0.1:7687` |
| 用户名 | `neo4j` |
| 密码 | `12345678` |
| 数据库 | `neo4j`（默认库） |

**注意**：`cypher-shell.bat` 无法直接使用，因为系统无 Java。如需 CLI 操作，设置 `JAVA_HOME=D:\neo4j\App\resources\offline\runtime\zulu21.48.17-ca-jre21.0.10-win_x64`。推荐直接用 Python driver。

---

## 4. 操作指南

### 4.1 步骤① — PDF 解析（从 Zotero 导入新论文）

前置条件：Zotero 已运行，MinerU Token 有效。

```bash
cd D:/tools/knowledge_base/script

# 预览将要解析的论文
python mineru_zotero_parser.py --dry-run

# 解析前 5 篇
python mineru_zotero_parser.py --limit 5

# 全量解析
python mineru_zotero_parser.py

# 重试之前失败的
python mineru_zotero_parser.py --retry-failed

# 补全元数据（不重新解析 PDF）
python mineru_zotero_parser.py --update-metadata

# 强制重写所有 metadata.json
python mineru_zotero_parser.py --update-metadata --force
```

产出：每篇论文目录下生成 `full.md` 和 `metadata.json`。

### 4.2 步骤②③ — LLM 数据管线

前置条件：`full.md` 已存在（由步骤①生成）。

```bash
cd D:/tools/knowledge_base/script

# 串行处理所有未完成的论文
python new_paper_pipeline.py

# 单篇处理
python new_paper_pipeline.py --single pan2025RobustDirect

# 10 篇并行，限制处理 20 篇
python new_paper_pipeline.py --workers 10 --limit 20

# 全量并行（推荐 workers=10）
python new_paper_pipeline.py --workers 10

# 强制重跑所有论文（忽略已完成的）
python new_paper_pipeline.py --force --workers 10

# 强制重跑单篇
python new_paper_pipeline.py --single bhat2023ZoeDepthZeroshot --force
```

管线内部流程（`new_paper_pipeline.py`）：
1. **Phase 0 — 预处理**：`full.md` → 提取 HTML 表格 → 删除 `<details>` 块 → `full_clear.md`
2. **Phase 1 — 物理切块**：按行切分为带 ID 的文本块数组
3. **Phase 2 — LLM 语义提取**：调用 DeepSeek API，输出 section_mapping + extracted_entities
4. **Phase 3 — 零幻觉校验**：基于 evidence_block_id 校验 evidence_quote（精确→Token重叠→模糊匹配）
5. **Phase 4 — 结果拼装**：组合为最终 JSON

完成判定：`{cite_key}.json` 存在即为已完成，默认跳过。`--force` 忽略此检查。

### 4.3 步骤④ — Neo4j 入库

前置条件：`{cite_key}.json` 已存在（由步骤③生成），Neo4j 服务已启动。

```bash
cd D:/tools/knowledge_base/script

# 预览将要导入的文件
python neo4j_bronze_ingestor.py --dry-run

# 单篇导入
python neo4j_bronze_ingestor.py \
  --single pan2025RobustDirect \
  --uri "neo4j://127.0.0.1:7687" \
  --username neo4j \
  --password 12345678 \
  --database neo4j

# 全量导入
python neo4j_bronze_ingestor.py \
  --uri "neo4j://127.0.0.1:7687" \
  --username neo4j \
  --password 12345678 \
  --database neo4j

# 只初始化约束
python neo4j_bronze_ingestor.py \
  --init-schema-only \
  --uri "neo4j://127.0.0.1:7687" \
  --username neo4j \
  --password 12345678 \
  --database neo4j
```

支持重复运行：同一篇论文重复导入会删除旧 Section 和旧关系后重建。

---

## 5. 数据格式详述

### 5.1 `{cite_key}.json` — 最终结果

```json
{
  "paper_id": "pan2025RobustDirect",
  "metadata": {
    "title": "...",
    "authors": ["Author1", "Author2"],
    "publication_year": 2025,
    "venue": "...",
    "DOI": "...",
    "url": "...",
    "abstractNote": "...",
    "language": "en",
    ...
  },
  "sections": [
    {"type": "ABSTRACT", "content": "..."},
    {"type": "METHODOLOGY", "content": "..."},
    ...
  ],
  "extracted_entities": {
    "research_tasks": [...],
    "proposed_methods": [...],
    "datasets": [...],
    "evaluation_metrics": [...],
    "baselines": [...],
    "addressed_existing_flaws": [...],
    "self_admitted_limitations": [...]
  }
}
```

### 5.2 Section 类型枚举

`ABSTRACT` | `MOTIVATION_AND_BACKGROUND` | `RELATED_WORK` | `METHODOLOGY` | `EXPERIMENT_SETUP_AND_RESULTS` | `DISCUSSION_AND_CONCLUSION` | `APPENDIX_AND_SUPPLEMENTARY` | `OTHER`

### 5.3 实体格式

**通用实体**（research_tasks, proposed_methods, evaluation_metrics, baselines, self_admitted_limitations）：
```json
{
  "name_in_paper": "VINS-Fusion",
  "aliases": [],
  "semantic_definition": "A visual-inertial odometry system...",
  "evidence_quote": "A comparison is conducted with...",
  "evidence_block_id": 119
}
```

**DatasetEntity**（额外字段）：
```json
{
  "name_in_paper": "KITTI Odometry",
  "aliases": ["KITTI"],
  "semantic_definition": "...",
  "evidence_quote": "...",
  "evidence_block_id": 103,
  "usage_role": "evaluation"  // "pre-training" | "fine-tuning" | "evaluation" | "other"
}
```

**FlawEntity**（额外字段）：
```json
{
  "name_in_paper": "feature-based methods fail in weakly textured scenes",
  "aliases": [],
  "semantic_definition": "...",
  "evidence_quote": "...",
  "evidence_block_id": 44,
  "targeted_baseline": "feature-based SLAM methods"
}
```

---

## 6. Neo4j 图谱 Schema

### 6.1 节点类型

| Label | 主键 | 属性 |
|-------|------|------|
| `Paper` | `paper_id` | paper_id, title, authors, publication_year, venue, DOI, url |
| `Section` | 无唯一约束 | type, content |
| `TaskMention` | `name` | name, aliases, semantic_definition |
| `MethodMention` | `name` | name, aliases, semantic_definition |
| `DatasetMention` | `name` | name, aliases, semantic_definition |
| `MetricMention` | `name` | name, aliases, semantic_definition |
| `BaselineMention` | `name` | name, aliases, semantic_definition |
| `LimitationMention` | `name` | name, aliases, semantic_definition |
| `FlawMention` | `name` | name, aliases, semantic_definition |

### 6.2 关系类型

| 关系 | 起点 → 终点 | 关系属性 |
|------|-------------|----------|
| `HAS_SECTION` | Paper → Section | 无 |
| `ADDRESSES_TASK` | Paper → TaskMention | evidence_quote, evidence_block_id |
| `PROPOSES_METHOD` | Paper → MethodMention | evidence_quote, evidence_block_id |
| `USES_DATASET` | Paper → DatasetMention | evidence_quote, evidence_block_id |
| `USES_METRIC` | Paper → MetricMention | evidence_quote, evidence_block_id |
| `COMPARES_WITH_BASELINE` | Paper → BaselineMention | evidence_quote, evidence_block_id |
| `HAS_LIMITATION` | Paper → LimitationMention | evidence_quote, evidence_block_id |
| `ADDRESSES_FLAW` | Paper → FlawMention | evidence_quote, evidence_block_id, targeted_baseline |

### 6.3 约束

- `Paper.paper_id` UNIQUE
- 每个 `*Mention.name` UNIQUE（同一 Label 下）

### 6.4 重复导入策略

同一论文重复导入时：
1. 保留 `Paper` 节点（MERGE 更新属性）
2. 删除该论文旧的 `Section` 及其 `HAS_SECTION` 关系
3. 删除该论文到 `*Mention` 的旧关系
4. 重建新 Section 和新关系
5. 清理因失去所有关系而变成孤立的 Mention 节点

### 6.5 当前图谱统计（2026-05-14 删库重建后）

| 指标 | 数量 |
|------|------|
| Paper | 177 |
| Section | 1,197 |
| TaskMention | 174 |
| MethodMention | 431 |
| DatasetMention | 309 |
| MetricMention | 342 |
| BaselineMention | 408 |
| FlawMention | 340 |
| LimitationMention | 205 |
| **总节点** | **3,583** |
| **总关系** | **3,883** |

关系分布：

| 关系类型 | 数量 |
|----------|------|
| HAS_SECTION | 1,197 |
| COMPARES_WITH_BASELINE | 593 |
| USES_METRIC | 480 |
| USES_DATASET | 450 |
| PROPOSES_METHOD | 440 |
| ADDRESSES_FLAW | 342 |
| HAS_LIMITATION | 205 |
| ADDRESSES_TASK | 176 |

---

## 7. 常用 Neo4j 查询

用 Python driver 执行（替代 cypher-shell）：

```python
from neo4j import GraphDatabase
driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "12345678"))

with driver.session(database="neo4j") as session:
    # 查看所有数据库
    for r in session.run("SHOW DATABASES"):
        print(r)

    # 论文总数
    print(session.run("MATCH (p:Paper) RETURN count(p)").single()[0])

    # 查看某篇论文的所有关系
    for r in session.run("""
        MATCH (p:Paper {paper_id: $pid})-[r]->(m)
        RETURN type(r) as rel, labels(m)[0] as label, m.name as name
    """, pid="pan2025RobustDirect"):
        print(r)

    # 关系类型分布
    for r in session.run("""
        MATCH ()-[r]->() RETURN type(r) as t, count(r) as c ORDER BY c DESC
    """):
        print(r)

    # 查看被最多论文引用的 Baseline
    for r in session.run("""
        MATCH (p:Paper)-[:COMPARES_WITH_BASELINE]->(b:BaselineMention)
        RETURN b.name, count(p) as cnt ORDER BY cnt DESC LIMIT 10
    """):
        print(r)

driver.close()
```

---

## 8. LLM 管线详细参数

### 8.1 使用的 LLM

- **模型**：DeepSeek（`deepseek-v4-flash`）
- **API 地址**：`https://api.deepseek.com`
- **密钥来源**：`config.json` 的 `llm_key` 字段

### 8.2 提取参数

- `max_tokens`：32768
- `reasoning_effort`：max
- `thinking`：enabled（流式推理）
- `response_format`：json_object

### 8.3 重试策略

- 每篇论文最多 3 次 LLM 调用
- 重试触发条件：LLM 返回空 / schema 校验失败 / block 覆盖率不全（missing 或 duplicate IDs）

### 8.4 校验层级

1. **Schema 校验**（`validate_llm_output`）：严格检查 section_mapping 和 extracted_entities 的结构、字段、类型
2. **覆盖率检查**（`check_coverage`）：确保所有 block ID 被分配且无重复
3. **证据校验**（`verify_entities`）：三级匹配验证 evidence_quote
   - Tier 1：精确子串匹配（大小写不敏感）
   - Tier 2：Token 重叠 ≥ 75%（解决 LaTeX 编码差异）
   - Tier 3：模糊匹配 `partial_ratio` ≥ 80%（`thefuzz` 库）

---

## 9. 故障排查

| 问题 | 排查方式 |
|------|----------|
| Neo4j 连不上 | 确认 Neo4j Desktop 中 DBMS 已启动，数据库状态为 online |
| LLM 管线报 `llm_key` 缺失 | 检查 `script/config.json` 中是否有 `llm_key` 字段 |
| 某篇论文入库失败 | 查看 `{cite_key}.json` 是否存在、结构是否完整 |
| neo4j_bronze_ingestor 报错 | 先 `--dry-run` 确认文件列表，再 `--single` 单篇测试 |
| 想重跑某篇论文 | `--single {cite_key} --force`（管线）；重复导入 Neo4j 会自动覆盖 |
| cypher-shell 报 java 找不到 | 设置 `JAVA_HOME` 或改用 Python driver 查询 |

---

## 10. 后续可做的工作

### 优先级 1
- 增加 `Block` 层导入，利用 `full_before_llm.md` 建立细粒度证据定位
- `usage_role` 写入 DatasetMention 节点属性

### 优先级 2
- 图谱浏览查询模板
- 导入后自动统计报告
- 更详细的异常日志

### 优先级 3
- Silver Layer：跨论文实体归一化
- 别名聚合、语义聚类、统一实体节点

---

## 11. Silver/Gold Layer 构建（2026-05-12）

### 11.1 三层架构总览

```
Bronze Layer（铜层）  → 论文原始 Mention（已完成）
    ↓ silver_builder.py（确定性规则归并）
Silver Layer（银层）  → SAME_AS 关系连接等价 Mention
    ↓ bronze_export.py + gold_ontology_builder.py + gold_importer.py
Gold Layer（金层）  → CanonicalEntity + IS_A 层级本体
```

### 11.2 Silver Layer

**脚本**：`script/silver_builder.py`

**规则**：
- Rule 1：大小写/标点/空格归一化后完全相同 → 合并
- Rule 2：别名交叉匹配（过滤泛称别名和长度 < 3 的别名） → 合并

**归并结果**：

| Label | 原始 | 归并后 | 减少量 | SAME_AS 关系 |
|-------|------|--------|--------|-------------|
| TaskMention | 174 | 172 | 2 | 2 |
| MethodMention | 431 | 424 | 7 | 10 |
| DatasetMention | 309 | 235 | 74 | 106 |
| MetricMention | 342 | 252 | 90 | 126 |
| BaselineMention | 408 | 354 | 54 | 90 |
| FlawMention | 340 | 337 | 3 | 6 |
| LimitationMention | 205 | 203 | 2 | 2 |

**操作命令**：
```bash
# 预览
python silver_builder.py --dry-run

# 执行 + 导出
python silver_builder.py --export D:/tools/knowledge_base/silver_data.json
```

### 11.3 Gold Layer

#### 11.3.1 导出

**脚本**：`script/bronze_export.py`

将 Silver 归并后的 Mention 按 SAME_AS 连通分量分组，每组取一个代表，输出为 LLM 友好的 JSON。

```bash
python bronze_export.py --output silver_input.json
```

产出：`silver_input.json`（约 134k tokens），按 7 个类别分别组织。

#### 11.3.2 LLM 本体推演

**脚本**：`script/gold_ontology_builder.py`

- 模型：DeepSeek v4 Flash（1M 上下文）
- 每批最多 120 个实体（避免输出截断和连接超时）
- 自动分批、重试（2 次）
- 校验完整性（所有 mention_id 必须出现在输出中）
- 同时输出可读的树状文本（`*_tree.txt`）

```bash
# 全量构建
python gold_ontology_builder.py

# 只处理某类
python gold_ontology_builder.py --label BaselineMention
```

**LLM 构建结果**：

| 类别 | 实体数 | Canonical 节点 | 校验 |
|------|--------|---------------|------|
| TaskMention | 172 | 219 | ✓ |
| MethodMention | 424 | 475 | ✓ |
| DatasetMention | 235 | 245 | ✓ |
| MetricMention | 252 | 100 | ✗ 部分缺失 |
| BaselineMention | 354 | 429 | ✓ |
| FlawMention | 337 | 346 | ✗ 1 个丢失 |
| LimitationMention | 203 | 131 | ✓ |

输出目录：`D:\tools\knowledge_base\gold_output\`
- `{Label}_ontology.json`：LLM 输出的本体 JSON（待人工审查）
- `{Label}_tree.txt`：可读的缩进树状视图

**人工审查**：审查上述 JSON 文件，修正 LLM 的错误归并或层级后，再执行导入。

#### 11.3.3 导入 Neo4j

**脚本**：`script/gold_importer.py`

解析本体 JSON，创建：
- `Canonical{Type}` 节点（`canonical_id`, `canonical_name`, `canonical_type`, `mention_count`）
- `(CanonicalEntity)-[:IS_A]->(CanonicalEntity)` 父子关系
- `(Mention)-[:RESOLVES_TO]->(CanonicalEntity)` 映射关系

```bash
# 预览
python gold_importer.py --dry-run

# 执行导入
python gold_importer.py

# 只导入某类
python gold_importer.py --label BaselineMention
```

### 11.4 Gold Layer Neo4j Schema 扩展

#### 新增节点类型

| Label | 主键 | 属性 |
|-------|------|------|
| `CanonicalTask` | `canonical_id` | canonical_id, canonical_name, canonical_type, mention_count |
| `CanonicalMethod` | `canonical_id` | canonical_id, canonical_name, canonical_type, mention_count |
| `CanonicalDataset` | `canonical_id` | canonical_id, canonical_name, canonical_type, mention_count |
| `CanonicalMetric` | `canonical_id` | canonical_id, canonical_name, canonical_type, mention_count |
| `CanonicalBaseline` | `canonical_id` | canonical_id, canonical_name, canonical_type, mention_count |
| `CanonicalFlaw` | `canonical_id` | canonical_id, canonical_name, canonical_type, mention_count |
| `CanonicalLimitation` | `canonical_id` | canonical_id, canonical_name, canonical_type, mention_count |

#### 新增关系类型

| 关系 | 起点 → 终点 | 说明 |
|------|-------------|------|
| `IS_A` | CanonicalEntity → CanonicalEntity | 层级本体（子类 → 父类） |
| `RESOLVES_TO` | Mention → CanonicalEntity | Bronze → Gold 映射 |
| `SAME_AS` | Mention → Mention | Silver 层等价关系 |

#### canonical_type 枚举

`Paradigm` | `Algorithm Family` | `Specific Algorithm` | `Metric Family` | `Specific Metric` | `Dataset Family` | `Specific Dataset` | `Task Category` | `Specific Task`

### 11.5 当前图谱统计（Bronze + Silver + Gold）

| 指标 | 数量 |
|------|------|
| **总节点** | **5,528** |
| **总关系** | **7,435** |

节点分布：

| 节点类型 | 数量 |
|----------|------|
| Paper | 177 |
| Section | 1,197 |
| *Mention（Bronze） | 2,209 |
| Canonical*（Gold） | 1,945 |

关系分布：

| 关系类型 | 数量 |
|----------|------|
| HAS_SECTION | 1,197 |
| COMPARES_WITH_BASELINE | 593 |
| USES_METRIC | 480 |
| USES_DATASET | 450 |
| PROPOSES_METHOD | 440 |
| RESOLVES_TO | 1,853 |
| IS_A | 1,235 |
| ADDRESSES_FLAW | 342 |
| HAS_LIMITATION | 205 |
| ADDRESSES_TASK | 176 |
| SAME_AS | 332 |

### 11.6 三层查询示例

```python
from neo4j import GraphDatabase
driver = GraphDatabase.driver("neo4j://127.0.0.1:7687", auth=("neo4j", "12345678"))
with driver.session(database="neo4j") as session:
    # 完整链路：论文 → Mention → 规范实体 → 父类
    for r in session.run("""
        MATCH (p:Paper)-[:COMPARES_WITH_BASELINE]->(m:BaselineMention)
              -[:RESOLVES_TO]->(c:CanonicalBaseline)
        WHERE p.paper_id = 'pan2025RobustDirect'
        RETURN m.name AS mention, c.canonical_name AS canonical, c.canonical_type AS type
    """):
        print(f"  {r['mention']} → {r['canonical']} [{r['type']}]")

    # 查看某规范实体的层级路径
    for r in session.run("""
        MATCH path = (c:CanonicalBaseline {canonical_name: 'ORB-SLAM3'})-[:IS_A*]->(parent)
        RETURN [n IN nodes(path) | n.canonical_name] AS hierarchy
    """):
        print("  层级:", " → ".join(r['hierarchy']))

    # 哪些论文使用了某个数据集（通过规范实体）
    for r in session.run("""
        MATCH (p:Paper)-[:USES_DATASET]->(m:DatasetMention)
              -[:RESOLVES_TO]->(c:CanonicalDataset {canonical_name: 'KITTI Odometry'})
        RETURN p.paper_id AS pid
    """):
        print(f"  使用 KITTI 的论文: {r['pid']}")
driver.close()
```
