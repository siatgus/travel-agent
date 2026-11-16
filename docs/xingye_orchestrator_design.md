# 行野旅行助手：Orchestrator 架构设计

> 本文整理自此前关于「行野」旅行助手调用链、现有 Agent 架构评估及
> Orchestrator
> 设计的讨论。重点是**在保留现有规则规划主链路的前提下，解耦对话调度、意图路由和业务处理**。
>
> 说明：文中的部分接口和代码是架构示意，不代表已经与现有源码签名完全一致。落地前必须以当前项目真实函数签名为准。

## 1. 现有系统背景

### 1.1 技术栈

-   Python 3.10+（本机 Python 3.13）
-   `http.server.ThreadingHTTPServer`：主服务 8787、管理后台
    8788，手写路由
-   `urllib`：直连 OpenAI 兼容接口；自研
    `llm.py`，包含重试、超时、`MeteredClient`、JSON 任务
-   `sqlite3`：
    -   `.app.db`：账号、积分、对话存档
    -   `.kb.db`：城市、景点、餐厅、酒店知识库
    -   `.llm_settings.json`：模型配置
-   原生 HTML/CSS/JS，`render_html.py` 内嵌模板
-   地图：优先高德 JS API 2.0，其次天地图 4.0，再次纯 SVG
-   外部数据：天地图 POI/地理编码、高德 REST v3
    驾车路线、Open-Meteo、Nominatim、火车票查询、百度图片
-   `autofetch.py`：缓存、熔断、节流
-   自建测试框架（13 个文件、900+ 断言），不是 pytest
-   setuptools、`pyproject.toml`

### 1.2 当前工具

  ------------------------------------------------------------------------------------------------------------
  工具                                主要职责
  ----------------------------------- ------------------------------------------------------------------------
  `get_weather`                       Open-Meteo，提供 today+15 天预报；超出范围走历史同期档案

  `search_transport`                  城际班次，知识库优先，再查内置班次库

  `search_hotel`                      酒店在售起价、星级、片区、距核心区公里数；知识库优先，再查静态种子数据

  `search_poi`                        景点门票、开放时间、坐标、品类、周边筛选；知识库优先，再查静态种子数据

  `calc_budget`                       预算分项、10% 备用金、人均、预算目标对比

  `save_preference`                   工具说明提到内存 `MEMORY_STORE`；源码确认目前只是内存字典，并非持久化
  ------------------------------------------------------------------------------------------------------------

### 1.3 当前核心设计原则

1.  真实班次、价格、天气、地理距离等事实由规则和工具负责。
2.  规划由规则流水线执行，模型不直接重排真实行程。
3.  模型主要用于分类兜底和最终自然语言表达。
4.  规划请求宁可进入规则规划管道继续识别，也不让 LLM
    自由生成一份未经校验的行程。
5.  不编造数据；参考价格、班次和营业时间要明确口径。
6.  稳定偏好可以保存，单次约束不应误存为长期偏好。

## 2. 当前真实调用链

一轮用户消息从进入到返回，主要在 `dialogue.Session._turn_impl`
中按优先级分层处理。

``` text
/api/chat
   │
   ▼
账号 Session
   │
   ▼
Session.turn()
   │
   ▼
_turn_impl()
   ├─ 写入 history（最近 6 轮历史提供给说话层）
   ├─ _plan_spot_gate：已有行程节点问答前置拦截
   ├─ 意图分类：规则优先，必要时 LLM 兜底
   ├─ 非规划问答：_answer(kind)
   ├─ 方案修改守卫
   ├─ 槽位抽取与合并
   ├─ find_missing：必要信息是否齐全
   ├─ plan_trip：规则规划流水线
   ├─ self_check
   ├─ compose_reply：模型只负责最终表达润色
   └─ 返回 Reply，服务端存档，前端按 mode 更新界面/地图
```

### 2.1 已有行程节点问答 gate

`_plan_spot_gate`
位于意图分类之前。它会识别当前方案中已经存在的景点或店铺，也包括早市、午休、就近自选等填充节点。

命中后会按节点身份回答，不再进入一般意图分类。

**需要注意：** 这个 gate
不能抢先吞掉明确的周边餐饮、路线等独立咨询。例如：

-   "宽窄巷子附近有什么好吃的？"
-   "宽窄巷子到春熙路怎么走？"

这类问题应该先识别为周边餐厅查询或路线查询，而不是简单回答行程节点信息。

### 2.2 意图分类

当前规则分类优先；规则无法识别（`other`）且文本像提问时，再由 LLM 兜底。

目前已有标签：

-   `weather`
-   `poi`
-   `transport`
-   `plan_qa`
-   `smalltalk`
-   `general`

即使 LLM 判断为
`plan`，也不在意图分类处直接执行规划。规划类请求仍须回到规则槽位管道。

### 2.3 非规划直答

分类命中问答类时，进入 `_answer(kind)` 并直接返回：

  意图          当前处理
  ------------- ----------------------------------------
  `weather`     调用 `get_weather`
  `poi`         景点按具体名称、品类、吃、住等路径分流
  `transport`   调用 `search_transport`
  `smalltalk`   调用 `llm.chat_reply`，结合人设和历史
  `general`     联网检索

### 2.4 方案修改守卫

在槽位抽取之前，当前系统有几类明确守卫：

-   修改班次时刻：拒绝直接改写，因为时刻必须来自真实数据。
-   分钟级锚点：通过 `composer.apply_anchor` 对整体行程进行平移。
-   "删掉可去可不去的"：通过 `_trim_overcap`
    就地删减，不做无要求的整体重排。
-   "改第 N 天"：先验证天数是否在当前行程范围内。

这些守卫有明确优先级和业务语义。重构时应保留其顺序与行为，不应简单合并成一个过于宽泛的
`check_plan_modification()`。

### 2.5 规划管道

1.  `extract_slots`：规则优先抽取槽位；抽取失败且像新需求时才由 LLM
    兜底。
2.  `merge_slots`：与已有会话槽位合并，显式字段可以覆盖旧值。
3.  `find_missing`：检查目的地、天数、日期等必要信息。
4.  缺少必要信息：生成追问。
5.  信息齐全：执行 `plan_trip`。
6.  `plan_trip`：天气、班次、酒店、POI 数据获取；缺数据时触发
    `autofetch`；通过 OSRM 获取路网耗时；由 `composer` 分天编排。
7.  `self_check`：对计划进行校验。
8.  `compose_reply`：模型只润色最终表达；数字保真校验失败则回退模板。
9.  返回 `Reply(mode, render, plan, slots)`；服务端存档，`mode=plan`
    时前端刷新地图。

## 3. Agent 框架是否需要更换

### 结论

**现阶段不需要更换 Agent 框架，但有必要升级自研架构。**

不建议为了"使用 Agent 框架"而直接引入 LangChain、LangGraph 或
CrewAI。当前系统已经有：

-   规则优先的意图识别；
-   明确的业务工具；
-   槽位抽取与合并；
-   规则规划流水线；
-   行程编排和自检；
-   数据缓存与补全机制；
-   自建测试体系。

当前的主要问题不是缺少一个第三方 Agent 框架，而是 `dialogue.py`
承担了过多职责：前置拦截、意图分类、业务分流、规划守卫、槽位管理、工具调用和最终回答都集中在同一轮处理函数中。

更适合的方向是：

**自研 Orchestrator + 业务 Handler + 规则规划引擎。**

Handler 是职责清晰的业务模块，不是多个可以自由发挥、互相交接的 LLM
Agent。

## 4. 目标 Orchestrator 架构

### 4.1 总体结构

``` text
Session
  │
  ▼
Orchestrator
  │
  ├── Guard Pipeline
  │     ├── 明确咨询预判
  │     ├── 行程节点问答 gate
  │     └── 方案修改守卫
  │
  ├── Intent Router
  │     ├── 规则分类
  │     └── LLM 分类兜底（受限）
  │
  ├── QA Handlers
  │     ├── WeatherHandler
  │     ├── AttractionHandler
  │     ├── FoodHandler
  │     ├── RestaurantHandler
  │     ├── HotelHandler
  │     ├── TransportHandler
  │     ├── RouteHandler
  │     └── GeneralHandler
  │
  ├── PlanHandler
  │     ├── 槽位抽取/合并
  │     ├── 缺失信息追问
  │     └── plan_trip
  │
  ├── Tool/Data Services
  │     ├── ToolService
  │     └── DataService / autofetch
  │
  └── Reply Builder
        ├── 结果格式化
        ├── 事实保真校验
        └── Reply
```

### 4.2 建议目录结构

``` text
project/
├── dialogue.py
├── orchestrator/
│   ├── __init__.py
│   ├── orchestrator.py
│   ├── context.py
│   ├── intent_router.py
│   ├── guard.py
│   ├── result.py
│   └── handlers/
│       ├── qa_handler.py
│       ├── food_handler.py
│       ├── restaurant_handler.py
│       ├── hotel_handler.py
│       ├── weather_handler.py
│       ├── transport_handler.py
│       ├── poi_handler.py
│       └── plan_handler.py
├── services/
│   ├── tool_service.py
│   ├── data_service.py
│   └── context_manager.py
├── intent.py
├── real_tools.py
├── composer.py
├── self_check.py
├── llm.py
└── autofetch.py
```

**不要一次性拆完所有模块。** 第一阶段只新增
Orchestrator、IntentRouter、Guard、PlanHandler，其余 Handler
先复用旧代码。稳定后再逐个拆出业务 Handler。

## 5. TurnContext：单轮上下文对象

Orchestrator 需要一个单轮上下文对象，避免函数之间传递大量零散参数。

示意结构：

``` python
@dataclass
class TurnContext:
    utterance: str
    history: list
    slots: dict
    last_plan: dict | None
    plan_made: bool

    city: str | None = None
    intent: str | None = None
    intent_source: str | None = None
    entities: dict = field(default_factory=dict)

    tool_results: dict = field(default_factory=dict)
    route: str | None = None
    handled: bool = False
```

职责边界：

-   `Session`：保存跨轮状态，例如历史、长期槽位、当前行程。
-   `TurnContext`：只保存当前这一轮的处理中间状态。
-   Handler：通过明确接口返回结果或状态变更，不应随意持有并修改整个
    Session。
-   不要在 `TurnContext` 中再持久化一份独立的会话状态，以免和 Session
    出现双份数据源。

## 6. IntentRouter：统一意图路由

### 6.1 推荐意图集合

在现有标签基础上，逐步细分为：

  意图                  说明                    示例
  --------------------- ----------------------- ------------------------
  `plan`                新建旅行规划            成都三天怎么玩
  `plan_modify`         修改当前行程            把第二天景点换掉
  `plan_qa`             询问已有行程内容        之前安排的酒店在哪里
  `weather`             天气查询                成都明天有雨吗
  `attraction_search`   景点查询                成都博物馆门票多少
  `food_query`          地方特色菜、美食文化    成都有什么特色美食
  `restaurant_search`   查询真实餐厅/店铺       成都有哪些好吃的火锅店
  `hotel_search`        酒店查询                推荐成都酒店
  `transport`           城际交通                成都到重庆怎么走
  `route_query`         城市内路线/地点间路线   宽窄巷子到春熙路怎么走
  `smalltalk`           闲聊                    你好
  `general`             其他通用问题            旅行需要带什么证件

### 6.2 美食与餐厅必须区分

这是意图扩展中的关键边界：

-   `food_query`：回答"吃什么"、地方菜系、特色菜和饮食文化。
-   `restaurant_search`：回答"去哪家店吃"，需要查询真实店铺或餐厅 POI。

例如：

  用户表达                 应路由到
  ------------------------ ---------------------
  成都有什么特色美食       `food_query`
  成都火锅有什么特色       `food_query`
  成都好吃的火锅店有哪些   `restaurant_search`
  春熙路附近有哪些川菜馆   `restaurant_search`

餐厅查询可复用
`search_poi(category="restaurant")`，后续再扩展距离、区域和排序能力。

### 6.3 周边查询优先级

"宽窄巷子附近有什么好吃的"包含地点实体，但用户真正要的是附近餐饮。应优先路由到
`restaurant_search`，不能被 `spot_mention` 或 `_plan_spot_gate`
当作普通景点节点问答处理。

同理，路线问题应优先进入 `route_query`，不能被景点问答 gate 吞掉。

### 6.4 分类结果结构

可以考虑让分类结果结构化：

``` python
@dataclass
class IntentResult:
    name: str
    confidence: float
    source: str
    entities: dict = field(default_factory=dict)
```

其中：

-   `name`：最终意图标签。
-   `confidence`：分类置信度，仅作辅助信号。
-   `source`：`rule` 或 `llm`。
-   `entities`：城市、景点、区域、日期等实体。

置信度不能成为把规划决策交给 LLM 的理由。LLM
兜底只补足分类，不负责决定真实班次、价格、路线或直接执行规划。

## 7. Orchestrator 的推荐处理顺序

建议将原 `_turn_impl` 中的步骤拆成明确阶段，但保留现有业务优先级。

``` text
1. 创建 TurnContext
2. 明确咨询预判
   ├── 餐厅周边查询
   ├── 美食查询
   ├── 酒店查询
   ├── 路线查询
   └── 天气/交通/景点等明确问答
3. 对非明确咨询执行行程节点问答 gate
4. 执行方案修改守卫
5. 规则意图分类
6. 仅在规则结果为 other 且像问题时，调用 LLM 分类兜底
7. 根据意图分发 Handler
8. 若为规划/规划修改：
   ├── 槽位抽取
   ├── 槽位合并
   ├── 检查缺失信息
   └── 进入规则规划流水线
9. 构建 Reply
10. Session 保存跨轮状态和对话记录
```

注意：这是一种目标职责拆分方案，不意味着可以随意改变现有守卫顺序。落地时要逐项对照
`_turn_impl` 的真实逻辑，尤其是分钟级锚点、删减、修改第 N
天和班次时刻限制。

## 8. Handler 职责划分

  ------------------------------------------------------------------------------------------------
  Handler                 职责                       复用现有能力
  ----------------------- -------------------------- ---------------------------------------------
  `WeatherHandler`        天气咨询                   `get_weather`

  `AttractionHandler`     景点信息、门票、开放时间   `search_poi`

  `FoodHandler`           地方特色菜、美食文化       美食知识库/已有回答逻辑

  `RestaurantHandler`     真实餐厅、附近餐饮         `search_poi(category="restaurant")`

  `HotelHandler`          酒店查询                   `search_hotel`

  `TransportHandler`      城际班次                   `search_transport`

  `RouteHandler`          城市内地点间路线           现有地图/路线能力，必要时扩展

  `PlanHandler`           新建和修改行程             `extract_slots`、`merge_slots`、`plan_trip`

  `GeneralHandler`        通用知识问题               现有联网检索逻辑
  ------------------------------------------------------------------------------------------------

### 酒店查询的特别说明

当前 `_stay_answer(city, utterance)`
实现中，默认按**明晚入住、后天离店**的日期口径查询，并使用当前人数；人数缺省为
2 人。它不是完全无日期查询。

后续建议：

-   用户明确提供日期：按指定入住/离店日期查询。
-   用户未提供日期：保留"明晚参考口径"，并在回答中清楚说明。
-   不得将查询到的参考起价描述成已经确认的实时房态或预订成功。
-   酒店查询为空时，考虑触发城市酒店数据补全；当前 `_stay_answer`
    代码片段中没有看到内部触发 `autofetch`。

## 9. ToolService 与 DataService

### 9.1 ToolService

`real_tools.py` 已有 `dispatch` /
工具注册机制，不必重新发明一套工具注册系统。

可以在外层封装：

-   统一工具调用入口；
-   记录调用日志；
-   统一异常处理；
-   统计耗时；
-   规范工具结果格式。

示意：

``` python
class ToolService:
    def __init__(self, agent):
        self.agent = agent

    def call(self, tool_name: str, args: dict):
        return self.agent.dispatch(tool_name, args)
```

这只是封装示意，实际应沿用项目现有 `agent` 对象和 `dispatch` 签名。

### 9.2 DataService

建议把跨业务的数据补全入口逐步统一，例如：

``` python
ensure_city_data(city, data_type, client=None)
```

它可以协调知识库查询、`autofetch`、缓存和补库结果回写。

但应先检查 `autofetch.py`
的现有职责和接口，再决定是否抽象，不要为了目录整洁重复实现已有能力。

## 10. Reply 与前后端协议

当前已知规划结果会返回：

``` python
Reply(mode, render, plan, slots)
```

建议短期先保持这个协议，减少前后端联动风险。

未来如果普通咨询也需要向前端提供结构化结果，再考虑扩展字段，例如：

-   `intent`
-   `data`
-   `sources`
-   `warnings`

是否需要扩展，必须先检查服务端序列化和前端消费方式；当前讨论没有证明前后端已经支持这些字段，因此不应直接假设可以新增后无影响。

## 11. 分阶段迁移方案

### 第一阶段：建立调度层，不改变业务行为

新增：

-   `orchestrator/orchestrator.py`
-   `orchestrator/context.py`
-   `orchestrator/intent_router.py`
-   `orchestrator/guard.py`
-   `orchestrator/result.py`

将 `_turn_impl` 逐步变成薄入口，负责创建上下文并调用
Orchestrator。第一阶段内部仍调用旧函数，目标是先迁移控制流，而不是重写业务。

### 第二阶段：拆分问答 Handler

优先拆分：

1.  `WeatherHandler`
2.  `RestaurantHandler`
3.  `FoodHandler`
4.  `HotelHandler`
5.  `TransportHandler`
6.  `AttractionHandler`
7.  `RouteHandler`

每拆一个 Handler，就保留原有行为并补充对应回归测试。

### 第三阶段：迁移规划逻辑

将槽位抽取、合并、缺失信息追问和 `plan_trip` 的入口放入 `PlanHandler`。

但
`composer`、`self_check`、工具取数和真实数据口径继续沿用现有实现，不在这一步重写规划算法。

### 第四阶段：统一数据服务和结果格式

在确认 `autofetch`、工具注册、服务端协议和前端消费逻辑后，再决定是否引入
DataService、结构化咨询数据等扩展。

## 12. 回归测试清单

### 12.1 意图路由测试

  用户输入                 预期意图/行为
  ------------------------ ---------------------
  成都三天怎么玩           `plan`
  成都特色美食             `food_query`
  成都火锅有什么特色       `food_query`
  成都好吃的火锅店有哪些   `restaurant_search`
  春熙路附近有哪些川菜馆   `restaurant_search`
  推荐成都酒店             `hotel_search`
  成都博物馆门票           `attraction_search`
  成都天气                 `weather`
  成都到重庆怎么走         `transport`
  宽窄巷子到春熙路怎么走   `route_query`
  把第二天景点换掉         `plan_modify`
  之前安排的酒店在哪里     `plan_qa`
  你好                     `smalltalk`

### 12.2 Gate 优先级测试

重点验证：

-   "宽窄巷子附近有什么好吃的"必须进入 `restaurant_search`。
-   "宽窄巷子到春熙路怎么走"必须进入 `route_query`。
-   对已有行程中的景点进行单纯询问时，仍能由行程节点 gate 正确回答。
-   明确咨询不能被行程节点 gate 错误拦截。

### 12.3 跨轮状态测试

至少覆盖以下流程：

1.  用户生成成都三日游。
2.  用户继续问"春熙路附近有什么美食"。
3.  系统回答餐饮咨询，但不能清空或重置当前行程。
4.  用户再说"把第二天景点换掉"。
5.  系统应基于原有行程和槽位进行修改，而不是当成全新规划。

还要覆盖：

-   修改第 N 天时，非法天数应被拒绝或追问。
-   分钟级锚点调整仍按原逻辑整体平移。
-   删除可选节点时不触发不必要的整体重排。
-   不允许模型擅自修改真实班次时刻。
-   普通咨询不能污染规划槽位。
-   单次约束不能被错误保存为长期偏好。

## 13. 当前已核实的源码注意事项

### 13.1 `dialogue.py`

-   `_answer(kind, utterance)` 负责按意图分发问答。
-   `poi` 分支中包含比较问题、具体景点、品类查询及泛推荐等处理。
-   具体景点查询会根据"周边/附近/周围"等词进入
    `_nearby_qa(city, spot)`，否则进入 `_spot_qa(city, spot)`。
-   `_stay_answer(city, utterance)`
    当前按明晚入住、后天离店查询酒店；人数取当前 `party_size`，缺省为
    2。
-   `_plan_spot_gate`
    的优先级较高，重构时需要防止它抢先拦截餐厅周边和路线咨询。

### 13.2 `intent.py`

-   `city_in(text)` 通过 `real_tools.CITY_GEO`
    做最长城市匹配，并有天气句式兜底。
-   存在 `stay_answer`、`poi_answer`、`recommend_answer` 等回答函数。
-   存在 `is_food_question`、`reco_keyword` 等分类辅助函数。
-   具体拆分时应继续检查完整函数实现，不能仅凭函数名假设其边界行为。

### 13.3 `real_tools.py`

-   `search_poi(category="restaurant")` 可用于餐厅 POI 检索。
-   知识库和静态 POI 种子数据优先。
-   已读取片段中出现大理、丽江、邯郸、上海等城市的静态
    POI；这不等于完整数据库绝对没有成都数据。
-   `save_preference` 当前只写内存 `MEMORY_STORE`，不是持久化存储。

## 14. 实施时必须避免的问题

此前给出的架构示例中，有几处只是伪代码，不能直接复制替换现有源码：

1.  `IntentRouter` 示例定义了 `classify()`，但 Orchestrator
    示例调用了未定义的 `classify_specific()`；需要统一接口。
2.  示例中的 `intent.looks_like_question()` 尚未核实项目中是否存在。
3.  示例中的 `llm.classify_intent()` 是建议接口，不代表当前 `llm.py`
    已实现。
4.  `Guard.check_plan_modification()`
    不能简单替代现有多个修改守卫；必须保留每个守卫的业务语义和顺序。
5.  `PlanHandler` 示例中的
    `plan_trip(self.session.slots)`、`self_check(plan)`
    等参数形式只是示意，实际必须核对真实函数签名。
6.  不要重新实现 `real_tools.py` 已有的工具注册与 `dispatch` 机制。
7.  不要在未检查 `autofetch.py` 前就重构数据补全。
8.  不要在未检查服务端和前端的情况下直接更改 `Reply` 协议。
9.  不要把多个 Handler 设计成可以互相自由委派的多个 LLM
    Agent；它们应是由 Orchestrator 明确调度的业务模块。

## 15. 最终建议

行野当前最值得做的不是更换 Agent
框架，而是**把已经存在的业务流程显式化、模块化**：

-   `Session` 继续管理跨轮状态；
-   `Orchestrator` 负责单轮流程调度；
-   `IntentRouter` 负责意图识别和分发决策；
-   `Guard` 负责保留现有高优先级业务规则；
-   各类 Handler 负责单一业务；
-   `ToolService` 复用现有工具分发；
-   `DataService` 在检查现有 `autofetch` 后再统一数据补全；
-   `PlanHandler` 继续调用规则规划引擎；
-   `composer` 和 `self_check` 继续掌管行程编排与校验；
-   LLM 只在受控范围内兜底分类和润色表达。

**推荐路线：先抽 Orchestrator 和 IntentRouter，再逐步拆
Handler；不推翻现有规划引擎，不一次性重写整个系统。**
