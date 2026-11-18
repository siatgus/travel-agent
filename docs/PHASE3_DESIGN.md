# 第三阶段设计说明：CandidateExtractor（五层候选识别瀑布）

> 2026-11-18。影子运行阶段：新候选识别与旧剥词链路并行，不驱动业务。

## 1. 目标与边界

- **目标**：把分散的减法剥词（spot_mention / extract_slots / food_keyword
  各剥各的）替换为从**原始句子**生成实体候选的统一机制，保留候选的
  Span、实体类型与上下文角色。
- **本阶段不做**（明确边界）：
  - EntityResolver / probe-then-store 存在性校验（第四阶段）
  - 三档置信分流（第四阶段）
  - LLM 兜底（第五阶段）
  - 在线写库、修改行程——候选 `matched=False` 只表示词典未命中，
    绝不代表实体不存在，也绝不进入 KB 或行程。

## 2. Span 契约

- `span_start`：含起始字符；`span_end`：不含结束字符（Python 切片语义）。
- 一律基于**原始用户输入**（Unicode 码点下标），不是剥词后的字符串。
- 契约测试：每条候选 `text[s:e] == text` round-trip（corpus v3 逐条检查）。
- 归一化：当前 L1 直接用原文子串匹配，无归一化层；若未来引入
  （全半角、繁简），必须同时维护原文↔归一文的下标映射，本阶段未引入。

## 3. 五层识别流程（识别顺序，非五个独立解析器）

```
原始输入 text + context
   │
L1 KB 全名/别名匹配      词典 = 静态种子 + KB 数据库 + 历史行程实体
   │  最长匹配优先、贪心不重叠；entity_type 取 KB category；
   │  matched=True，match_type=kb_full / context
L2 实体类型线索未知候选   类型后缀词表（酒店 > 餐饮 > 景点，按长度优先）
   │  + 分店右扩展（（豫园店）/ 郑和南路店）
   │  + 城市前缀左并入（排[上海]贝皇大排档）
   │  + 品类词表（火锅/游乐园…）→ category 候选
   │  + 动词直接挂名（安排[小笼包]）→ unknown/intent_only 候选
   │  matched=False
L3 角色判定             remove > negated > nearby_anchor > must_visit > query
   │  前窗（动词）锚定行尾、后窗（疑问）不锚定
L4 城市识别             只用未被上层占用的 Span
   │  路线框架 从A到B → A=departure_city，B=destination；
   │  「到」后无城市 → 留空并注明，绝不硬截
   │  规划句 → destination；查询句 → query_city
L5 冲突消歧             Span 重叠：层优先（L1>L2）> 长匹配 > 起点；
   │  不因最长匹配误删语义独立的多个实体（T12 双景点）
   ↓
ExtractResult(poi_candidates, city_candidates, notes)
```

## 4. 关键规则与陷阱（实测教训固化）

| 规则 | 反例（未加规则时） |
| --- | --- |
| 城市前缀并入候选实体名，不另发城市候选 | 「排上海贝皇大排档」把上海当换城市 |
| 剥词前先试所有剥法，优先"剥完落在城市名"的剥法 | 「排上」两读：剥成「海贝皇」丢上海 |
| 品类 Span 与类型后缀 Span 重叠时后缀跳过 | 「海贝皇海鲜\|大排档」品类把店名切碎 |
| 后缀与 L1 实体紧邻时跳过 | 「中山陵\|景区」二次扩展出碎片 |
| 泛指后缀（什么/哪些/哪家 + 类型词）不生成实体 | 「附近有什么餐厅」生成「什么餐厅」 |
| 动词挂名词仅在无重叠时生成 unknown 候选 | 「大**排**档」的排字被当动词 |
| 长全名 L1 整体命中，禁止碎片 | 「国民政府中央广播电台\|旧址」 |
| 「到」后非城市 → 目的地留空 + notes 注明 | 「从大理到玩5天」把"玩5天"当 POI/城市 |

## 5. 影子运行机制

- `parser.parse_utterance()` 末尾并行调用 `extract()`，结果写入
  `ParseResult.shadow_candidates / shadow_notes`。
- 旧链路（spot_mention / extract_slots / plan_handler）**继续决定业务**。
- 影子通道任何异常都只追加 note，不影响业务（try/except 全包）。
- 差异报告：`tests/test_corpus_v3.py` 逐条对比新旧候选，分类：
  - `recall-gain`：新瀑布识别出旧剥词漏掉的候选（13 条）
  - `dropped`：旧剥词有、新瀑布无（6 条——全是旧误报清除：
    「从到丽江」「的火锅店」「海贝皇大排档」缺城市前缀等）
  - `role-diff`：双方都有但角色不同（6 条——新瀑布按 L3 规则判定）
- 切换消费方：影子稳定后按 依赖方逐个切换 + 每切换一个补回归 的
  节奏进行（第四阶段起）。

## 6. 词典管理

| 词典 | 来源 | 更新方式 |
| --- | --- | --- |
| KB 实体 | POIS_DB 静态种子 + kb.db（只读）+ 历史行程实体 | 种子随代码评审；KB 随数据层 |
| 城市 | `real_tools.CITY_GEO`（351 城） | 随地理数据 |
| 品类 | `_CATEGORY_WORDS`（本模块，18 词） | 词表变更需测试佐证 |
| 类型后缀 | `_TYPE_SUFFIXES`（酒店>餐饮>景点，优先级即冲突规则） | 不允许把任意尾部词加进来 |
| 停用词/边界 | `_STOP_PREFIX` / `_TRAILING_STOP_RE` / `_BRANCH_RE` | 同上 |

## 7. 本阶段改动文件

- 新增 `src/travel_agent/candidate_extractor.py`（五层瀑布）
- 新增 `tests/test_candidate_extractor.py`（36 断言）
- 新增 `tests/corpus_v3.tsv`（25 病例）、`tests/test_corpus_v3.py`、
  `tests/corpus_v3_report.txt`
- 修改 `src/travel_agent/parser.py`（影子字段 + 影子调用，业务零改动）
- 修改 `src/travel_agent/real_tools.py`（补南京/成都/苍山静态种子——
  供 L1 词典用例；KB 数据文件零改动）
- 新增 `tests/BACKLOG_DEPENDENCY.md`（C01 登记为 DEP-001）
