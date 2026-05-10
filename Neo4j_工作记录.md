# Neo4j 工作记录

## 1. 工作目标

本次工作的目标是：基于 `zotero_data/` 目录下已经完成 LLM 提取的论文 JSON，构建 Neo4j 中的 **Bronze Layer（物理提及层）**，实现论文原文载体与大模型提取提及之间的基础图谱入库。

当前实现聚焦于：
- `Paper`
- `Section`
- 各类 `*Mention`
- `Paper -> Mention` 证据关系

本次未实现：
- `Block` 层导入
- 跨论文实体消歧
- Silver / Gold 层统一实体图谱

---

## 2. 新增代码

已新增脚本：

- `script/neo4j_bronze_ingestor.py`

该脚本的职责：
- 初始化 Neo4j 约束
- 扫描 `zotero_data/*/*.json`
- 过滤非最终结果文件
- 支持单篇 / 批量导入
- 使用 Neo4j 官方 Python Driver 进行写入
- 保证单篇论文通过 `session.execute_write(...)` 原子导入

---

## 3. 当前脚本实现的导入规则

### 3.1 Paper 节点
- Label: `Paper`
- 主键：`paper_id`
- 属性：
  - `paper_id`
  - `title`
  - `authors`
  - `publication_year`
  - `venue`
  - `DOI`
  - `url`

### 3.2 Section 节点
- Label: `Section`
- 属性：
  - `type`
  - `content`
- 关系：
  - `(Paper)-[:HAS_SECTION]->(Section)`

### 3.3 Mention 节点与关系
当前已实现的实体映射如下：

- `research_tasks`
  - 节点：`TaskMention`
  - 关系：`ADDRESSES_TASK`
- `proposed_methods`
  - 节点：`MethodMention`
  - 关系：`PROPOSES_METHOD`
- `datasets`
  - 节点：`DatasetMention`
  - 关系：`USES_DATASET`
- `evaluation_metrics`
  - 节点：`MetricMention`
  - 关系：`USES_METRIC`
- `baselines`
  - 节点：`BaselineMention`
  - 关系：`COMPARES_WITH_BASELINE`
- `self_admitted_limitations`
  - 节点：`LimitationMention`
  - 关系：`HAS_LIMITATION`
- `addressed_existing_flaws`
  - 节点：`FlawMention`
  - 关系：`ADDRESSES_FLAW`
  - 额外关系属性：`targeted_baseline`

Mention 节点当前写入的属性：
- `name`
- `aliases`
- `semantic_definition`

关系当前写入的属性：
- `evidence_quote`
- `evidence_block_id`
- `targeted_baseline`（仅 `ADDRESSES_FLAW`）

---

## 4. 当前约束设计

脚本会自动初始化以下唯一约束：

- `Paper.paper_id`
- `TaskMention.name`
- `MethodMention.name`
- `DatasetMention.name`
- `MetricMention.name`
- `BaselineMention.name`
- `LimitationMention.name`
- `FlawMention.name`

说明：
- `Section` 当前不做唯一约束
- Mention 当前是按“同一 Label 下 name 唯一”处理的

---

## 5. 文件扫描规则

脚本扫描：
- `zotero_data/*/*.json`

会自动排除：
- `metadata.json`
- `sections.json`
- `layout.json`
- `*_llm_raw.json`

当前优先识别：
- `zotero_data/<cite_key>/<cite_key>.json`

---

## 6. 重复导入策略

当前实现支持重复运行。

对于同一篇论文重复导入时：
- 会保留 `Paper` 节点
- 会删除该论文旧的 `Section`
- 会删除该论文到 Mention 的旧关系
- 会重新创建新的 `Section` 和新的论文到 Mention 的关系
- 若某些 Mention 因失去所有关系而变成孤立节点，会被清理

这样可以避免同一篇论文重复累积 `Section`，并保证关系属性按最新 JSON 刷新。

---

## 7. 环境处理记录

### 7.1 Neo4j Desktop / CLI 相关
已确认本机 Neo4j 安装目录下存在可用 CLI：
- `neo4j.bat`
- `neo4j-admin.bat`
- `cypher-shell.bat`

已确认：
- 系统默认 Java 版本过低（Java 15）
- 当前 Neo4j 2026.03.1 需要更高版本运行时
- Neo4j 安装目录自带可用的 Java 21

可用 Java 路径：
- `F:\neo4j\APP\resources\offline\runtime\zulu21.48.17-ca-jre21.0.10-win_x64\bin\java.exe`

### 7.2 Python 依赖
本机 Python 环境原本缺少 `neo4j` 包，已安装：
- `neo4j==6.2.0`

---

## 8. 本次数据库连接与导入情况

已确认实例可连接：
- URI: `neo4j://127.0.0.1:7687`
- 用户名：`neo4j`
- 密码：`12345678`
- 数据库：`base1`
- Instance ID：`3a28c67d-b964-454d-a12d-777d82132d41`
- DBMS 路径：`F:\neo4j\PROData\Application\Data\dbmss\dbms-3a28c67d-b964-454d-a12d-777d82132d41`

说明：
- 之前用户提到的 `knowledge_base` 并不是实际可连接数据库名
- 经检查，当时在线数据库只有 `neo4j` 和 `system`
- 后续用户重新创建数据库 `base1`，并成功在线
- 以上账号密码为**本地测试数据库**连接信息，按用户授权记录在仓库文档中

**注意：以上密码仅适用于当前本地测试库，不应视为生产或共享环境凭据。**

---

## 9. 实际导入结果

### 9.1 已导入文件
共导入 3 篇论文：

- `zotero_data/campos2021ORBSLAM3Accurate/campos2021ORBSLAM3Accurate.json`
- `zotero_data/pan2025RobustDirect/pan2025RobustDirect.json`
- `zotero_data/yu2025MCVOGeneric/yu2025MCVOGeneric.json`

### 9.2 导入后图谱统计
导入完成后，`base1` 中统计结果为：

- `Paper`: **3**
- `HAS_SECTION`: **18**
- `ADDRESSES_TASK`: **3**
- `PROPOSES_METHOD`: **11**
- `USES_DATASET`: **6**
- `USES_METRIC`: **7**
- `COMPARES_WITH_BASELINE`: **11**
- `ADDRESSES_FLAW`: **8**
- `HAS_LIMITATION`: **6**

### 9.3 样例验证结果
对 `yu2025MCVOGeneric` 做过单篇验证，结果正确：

- `Paper`: 1
- `Section`: 6
- Mention 边数：18

关系分布：
- `ADDRESSES_TASK`: 1
- `PROPOSES_METHOD`: 4
- `USES_DATASET`: 2
- `USES_METRIC`: 3
- `COMPARES_WITH_BASELINE`: 3
- `ADDRESSES_FLAW`: 3
- `HAS_LIMITATION`: 2

---

## 10. 已执行的验证方式

### 10.1 脚本层验证
已验证：
- `python -m py_compile script/neo4j_bronze_ingestor.py`
- `--single ... --dry-run`
- 全量 `--dry-run`
- 单篇真实导入
- 全量真实导入

### 10.2 Neo4j 层验证
已使用 `cypher-shell` 检查：
- 数据库是否在线
- `Paper` 总数
- 样例论文 `Section` 数量
- 样例论文 Mention 关系数量与分布
- 全库关系类型计数

---

## 11. 当前脚本的使用方式

### 11.1 单篇干跑
```bash
python "F:/MyUsefulTool/knowledge_base/script/neo4j_bronze_ingestor.py" \
  --data-dir "F:/MyUsefulTool/knowledge_base/zotero_data" \
  --single yu2025MCVOGeneric \
  --dry-run
```

### 11.2 单篇导入
```bash
python "F:/MyUsefulTool/knowledge_base/script/neo4j_bronze_ingestor.py" \
  --data-dir "F:/MyUsefulTool/knowledge_base/zotero_data" \
  --single yu2025MCVOGeneric \
  --uri "neo4j://127.0.0.1:7687" \
  --username neo4j \
  --password 12345678 \
  --database base1
```

### 11.3 全量导入
```bash
python "F:/MyUsefulTool/knowledge_base/script/neo4j_bronze_ingestor.py" \
  --data-dir "F:/MyUsefulTool/knowledge_base/zotero_data" \
  --uri "neo4j://127.0.0.1:7687" \
  --username neo4j \
  --password 12345678 \
  --database base1
```

---

## 12. 后续可继续做的工作

如果后续继续扩展，建议优先级如下：

### 优先级 1
- 增加 `Block` 层导入
- 利用 `full_before_llm.md` 和 `*_llm_raw.json` 建立更细粒度证据定位

### 优先级 2
- 增加图谱浏览用查询模板
- 输出导入后的统计报告
- 增加更详细的异常日志

### 优先级 3
- 做 Silver Layer：跨论文实体归一化
- 引入别名聚合、语义聚类、统一实体节点

---

## 13. 当前状态结论

截至当前：
- Neo4j Bronze Layer 首版入库脚本已完成
- 本地 Neo4j 实库已成功写入数据
- 真实论文 JSON 已完成导入验证
- 当前仓库已具备继续扩展到更细粒度图谱层的基础
