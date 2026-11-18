# 第四阶段设计说明：EntityResolver（实体存在性验证与消歧）

> 2026-11-18。影子运行阶段：实体验证与旧链路并行，不驱动业务。
> 第三阶段基线：commit `80337bd`（五层候选识别瀑布，已冻结）。

## 1. 目标与边界

第三阶段回答的是「用户说了什么、文本里有哪些候选」；第四阶段回答
「**这些候选究竟是什么、是否真实存在、属于哪个城市、能否进入行程**」。

- **目标**：对 CandidateExtractor 产出的每个 POI 候选做独立、只读的
  存在性验证与消歧，产出四档分流结果。
- **本阶段不做**（验收红线，逐条对应）：
  - 不让 LLM 决定实体是否存在（LLM 兜底是第五阶段）
  - 未验证候选**绝不写入 .kb.db**——本模块对 KB 只读，也绝不调用
    `autofetch.ensure_spot`（它会写库）
  - 在线接口失败 → `probe_status="unavailable"`，**绝不解释成
    not_found**（网络故障 ≠ 用户输入错误）
  - 名称匹配成功 ≠ 直接加入行程——本阶段仍是影子运行，结果只记录
  - 不修改 Composer、路线优化、预算计算
  - 不顺手修 DEP-001（行程依赖一致性引擎，见 tests/BACKLOG_DEPENDENCY.md）

## 2. 处理流程

```
CandidateExtractor（Phase 3，五层瀑布，产出候选+Span+类型+角色）
   │
EntityResolver（Phase 4，逐候选独立验证）
   │  ① KB 精确匹配     静态种子 + KB 数据库，名称全等优先
   │  ② 部分匹配        双向包含（短名 ≥2 字），置信降档并如实标注
   │  ③ 城市归属验证    候选命中城市 vs 当前目的地/上下文城市
   │  ④ 类型验证        候选 entity_type vs KB category（餐厅≠景点）
   │  ⑤ 分店消歧        同名多分行/跨城多记录 → 列清单，绝不静默合并
   │  ⑥ 在线只读探测    KB 未命中时查高德（find_spot / search_amap_keyword）
   │  ⑦ 置信度          名称匹配方式 × 城市一致性 × 类型一致性
   ↓
四档分流（对齐验收流程图）
   confirmed   高置信度：唯一匹配、城市类型一致 → 允许进入后续流程
   ambiguous   中置信度：多候选/跨城/类型不符 → 要求用户消歧
   rejected    低置信度：在线探测核实不存在 → 不进行程，保留原始表达
   unverified  未验证：探测不可用/未执行 → 记录候选，等待后续验证
```

## 3. 关键契约

### 3.1 probe_status 与 status 严格正交

`probe_status` 记录「探测这件事做到哪一步」，`status` 是业务分流结论：

| probe_status | 含义 | 对 status 的影响 |
|---|---|---|
| not_needed / kb_hit | KB 已命中 | 不探测 |
| online_hit | 在线命中 | 可 confirmed |
| not_found | 探测**成功执行**且确认没有 | rejected（保留原始表达） |
| unavailable | 网络/Key/熔断/无坐标，**探测没做成** | 一律 unverified |
| skipped | 本轮关闭在线探测（影子默认 KB-only） | unverified |

铁律：`unavailable ≠ not_found`。任何异常（urllib 报错、无 Key、
熔断中、解析不出城市坐标）都落 unavailable。

### 3.2 matched 语义延续

第三阶段的 `matched`（词典命中）继续区分于「实体真实有效」：
- matched=True 只说明词典里有，不保证属于当前城市、类型正确或就是
  用户想要的那个——这正是本阶段 ③④⑤ 要验证的。
- matched=False 不等于不存在——本阶段 ⑥ 用只读探测补证。

### 3.3 只读

- KB 读取复用第三阶段同一套源（`real_tools.POIS_DB` 静态种子 +
  `kb.get_kb()`），支持注入 `kb_provider` 供测试/未来扩展。
- 在线探测只调 `poi_sources.find_spot`（景点）/ `search_amap_keyword`
  （餐饮/住宿），二者均为纯查询；结果只进 ResolveResult，不入库。
- 探测函数可整体注入（`probe_fn`），测试离线可复现。

### 3.4 分店消歧

同名多记录（同城不同 area 的分店、跨城同名）→ status=ambiguous，
matches 全量列出（city/name/category/area/source），绝不静默合并、
绝不取第一个。跨城命中且与上下文城市不符 → 如实注明
「记在南京不在上海」（延续第二阶段宝船问题的口径）。

## 4. 置信度（规则，无 LLM）

| 信号 | 分值 |
|---|---|
| KB 全名相等 | 0.92 |
| KB 双向包含（部分匹配） | 0.82 |
| 在线探测全名/包含命中 | 0.80 / 0.72 |
| 无上下文城市可比对 | −0.12 |
| 类型不符 | −0.25 且强制 ambiguous |
| 部分匹配 | 含在上表基线内 |

confirmed 阈值：0.75 且唯一匹配且城市一致（或上下文无城市可比对）。
被扣到阈值之下或出现多候选 → ambiguous。

## 5. 影子接入

- `parser.parse_utterance` 的影子块在 CandidateExtractor 之后追加
  EntityResolver，结果挂 `ParseResult.shadow_resolved`（list[dict]），
  与 `shadow_candidates` 同批只记录不驱动业务，全程 try/except 包裹。
- 影子默认 **KB-only**（`allow_online=False`）：不产生额外网络请求、
  不拖慢主链路；打开环境变量 `TRAVEL_RESOLVER_ONLINE=1` 后影子也做
  在线探测（复用 poi_sources 的缓存与熔断）。直接调
  `resolve(...)` 时默认跟随同一环境变量。
- 对比口径：旧链路（spot_mention / ensure_spot 补库通道）产生的实体
  行为 vs 新链路四档分流，差异记入语料回归报告（corpus v4）。

## 6. 角色与分流的关系

- `negated` / `remove` 候选照常验证（验证结果供后续一致性引擎用），
  但本阶段不因角色改变 status——要不要进行程是下游的事。
- `category` / `city` 候选不是 POI，status=skipped，只记录不验证。

## 7. 交付物

- `src/travel_agent/entity_resolver.py`：ResolvableCandidate /
  ResolvedCandidate / ResolveResult + resolve()
- `parser.py`：ParseResult.shadow_resolved + 影子接线（约 10 行）
- `tests/corpus_v4.tsv`：14 例（KB 唯一/跨城/分店/部分/类型不符/
  在线命中/在线多候选/在线核实不存在/探测不可用/离线跳过/品类跳过/
  无城市坐标/否定角色照常验证/跨城加点 C12 的验证版）
- `tests/test_entity_resolver.py`：单测（含 unavailable≠not_found 专项）
- `tests/test_corpus_v4.py`：语料断言 + 新旧链路差异记录

## 8. 明确不做（留给第五阶段及以后）

- LLM 兜底判断（第五阶段）
- 置信度三档分流接管业务决策（先影子积累数据）
- probe-then-store 在线补库（ensure_spot 通道仍是旧链路职责）
- 行程依赖一致性引擎 DEP-001
