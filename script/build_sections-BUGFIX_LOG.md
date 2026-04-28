# build_sections.py Bug 修复记录

## Bug 1: 容器标题被静默丢弃

### 现象
- Eckenhoff2019 论文本应有 14 个 section，实际只生成了 13 个
- "IV. EXPERIENTIAL RESULTS"（实验章节的父标题）丢失
- 其子 section "A. Duo-Camera Configuration" 和 "B. Three-Camera Configuration" 继承到了错误祖先的类型（method），而非 experiment_or_evaluation

### 根因
在 `build_sections.py` 的两处位置使用了条件：
```python
if current_title_text and current_text_blocks:
```
当两个标题块连续出现（如 "IV. EXPERIENTIAL RESULTS" 紧接 "A. Duo-Camera Configuration"），第一个标题的 `current_text_blocks` 为空列表，导致该 section 被跳过。同时还毒化了 `current_parent_type`，子 section 错误地继承到更早祖先的类型。

### 修复
将两个 `if` 条件从：
```python
if current_title_text and current_text_blocks:
```
改为：
```python
if current_title_text:
```
允许创建正文为空的 section（容器标题），正确记录其类型并传递给子 section。

### 影响范围
- `build_sections.py` 第 253 行（主循环内的 section 保存逻辑）
- `build_sections.py` 第 295 行（最后一个 section 的保存逻辑）

### 修复后效果
- Eckenhoff2019: 13 → 14 sections
- Hug2022: 14 → 15 sections
- 子 section 现在继承直接父 section 的类型，而非远祖类型

---

## Bug 2: OCR 错字导致关键词匹配失败

### 现象
"IV. EXPERIENTIAL RESULTS" 被归类为 `result_or_analysis`（仅匹配到 "results"），而非正确的 `experiment_or_evaluation`。

### 根因
MinerU PDF 提取将 "EXPERIMENTAL" 错误识别为 "EXPERIENTIAL"（遗漏字母 'm'）。正则 `\bexperiment` 无法匹配 "experiential" — 因为 "experiential" = "experi" + "ential"（源自 "experience"），不是 "experi" + "mental"（源自 "experiment"）。

验证：
```python
re.search(r'\bexperiment', 'iv. experiential results')  # 返回 None
```

### 修复
在 `TYPE_RULES` 中新增一条规则，捕获此 OCR 错字：
```python
(r'\bexperiential\b', 'experiment_or_evaluation'),  # OCR typo for "experimental"
```
该规则放在原有 `r'\bexperiment'` 规则之后。由于 TYPE_RULES 按顺序匹配（先匹配有效），正常的 "experiment" 仍会被第一条规则捕获。

### 修复后效果
- Eckenhoff2019 S10 "EXPERIENTIAL RESULTS" → `experiment_or_evaluation`（之前为 `result_or_analysis`）
- 其子 section S11、S12 正确继承为 `experiment_or_evaluation`

---

## 最终验证

| 论文 | Section 数 | 分类正确性 |
|------|-----------|-----------|
| Eckenhoff2019 | 14 | ✅ |
| Eckenhoff2021 | 29 | ✅ |
| Hug2022 | 15 | ✅ |
| Li2025 | 27 | ✅ |
| Lv2023 | 27 | ✅ |
| Qi2025 | 14 | ✅ |
| Yang2021 | 17 | ✅ |
