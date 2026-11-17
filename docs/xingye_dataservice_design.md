# 晴航 2.0 第二阶段开发档案

**项目名称：** 晴航（Xingye）智能旅行助手  
**档案类型：** 技术开发档案  
**开发阶段：** 第二阶段——DataService 统一数据服务建设  
**版本：** V1.0  
**编制日期：** 2026-11-13  
**文档状态：** 技术设计基线，待源码审查与实施  
**技术栈：** Python 3.10+、SQLite、OpenAI 兼容 API、原生 HTML/CSS/JavaScript

---

# 一、项目背景

晴航是一款基于大语言模型和自研规则引擎构建的智能旅行规划助手。

项目采用自研 Agent 调度机制，不依赖 LangChain、LangGraph 等第三方 Agent 框架。系统通过规则识别、工具调用、数据获取、行程编排和结果校验完成旅行规划。

当前系统已经具备以下能力：

1. 天气查询。
2. 城际交通查询。
3. 酒店查询。
4. 景点及 POI 查询。
5. 旅行预算计算。
6. 用户偏好保存。
7. 多轮对话和旅行计划修改。
8. 基于 OSRM 的路线耗时计算。
9. 自动数据补全和本地知识库管理。

第一阶段的架构升级方向是引入 Orchestrator，逐步解耦原有 dialogue.py 中的对话调度逻辑。

第二阶段重点建设统一数据服务层 DataService，解决数据获取、缓存、补全、数据来源和异常处理分散的问题。

# 二、现有系统技术架构

## 2.1 技术栈

| 模块 | 当前技术 |
|---|---|
| 后端语言 | Python 3.10+ |
| Web 服务 | http.server.ThreadingHTTPServer |
| 主服务端口 | 8787 |
| 管理后台端口 | 8788 |
| 数据库 | SQLite |
| 账号及会话数据库 | .app.db |
| 旅行知识库 | .kb.db |
| 前端 | 原生 HTML/CSS/JavaScript |
| 地图 | 高德 JS API 2.0 / 天地图 4.0 / SVG |
| LLM 接口 | OpenAI 兼容 API |
| LLM 封装 | 自研 llm.py |
| 路线计算 | OSRM |
| 自动补全 | autofetch.py |
| 测试 | 自研测试框架 |

## 2.2 当前工具体系

系统目前包含六个工具：

| 工具 | 功能 |
|---|---|
| get_weather | 天气查询 |
| search_transport | 城际交通查询 |
| search_hotel | 酒店查询 |
| search_poi | 景点及 POI 查询 |
| calc_budget | 旅行预算计算 |
| save_preference | 用户偏好保存 |

其中 save_preference 当前使用进程内 MEMORY_STORE 字典，尚不能视为可靠的持久化记忆。

## 2.3 当前数据来源

系统主要采用：

- 本地 SQLite 知识库。
- 静态种子数据。
- 外部天气 API。
- 外部地图及 POI 服务。
- 外部交通查询接口。
- 自动数据补全机制。

已知 autofetch.py 具有缓存、熔断和节流能力。

具体缓存结构、补全函数签名及数据库表结构需要在实施前进一步审查。

# 三、当前存在的问题

## 3.1 数据访问入口分散

数据查询逻辑分散在 real_tools.py、autofetch.py 和业务对话逻辑中。

新增业务时可能需要重复实现：

- 本地数据查询。
- 外部 API 请求。
- 数据格式转换。
- 异常处理。
- 缓存控制。

## 3.2 数据返回结构不统一

不同工具的返回字段和错误表达方式可能存在差异。

上层业务需要理解不同工具的返回协议，增加了维护成本。

## 3.3 数据来源不透明

系统需要区分：

- 本地知识库数据。
- 静态种子数据。
- 外部 API 数据。
- 缓存数据。
- 历史参考数据。

尤其需要区分缓存来源和数据最初来源。

## 3.4 数据时效性管理不足

天气、交通班次、酒店基础信息、酒店实时房态具有不同的时效要求。

不能使用统一 TTL，也不能把过期数据当作实时数据。

## 3.5 餐厅数据能力有待完善

目前已有 search_poi(category="restaurant") 的基础能力，但需要进一步完善：

- 餐厅关键词搜索。
- 餐饮类别筛选。
- 周边餐厅搜索。
- 餐厅数据标准化。
- 坐标系处理。
- 数据来源及更新时间管理。

## 3.6 空结果与查询失败需要严格区分

以下情况必须使用不同状态：

1. 查询成功且有数据。
2. 查询成功但没有数据。
3. 查询失败。
4. 查询成功但仅返回过期数据。

不能将 API 超时解释成目的地没有相关数据。

# 四、第二阶段建设目标

本阶段的核心目标：

建立统一的数据访问层 DataService，使业务 Handler 不再直接处理不同数据来源的查询细节。

具体目标：

1. 建立统一 DataResult 返回协议。
2. 建立 DataPolicy 数据获取策略。
3. 建立 DataService 统一查询入口。
4. 复用现有 autofetch.py。
5. 复用现有缓存、熔断和节流机制。
6. 统一数据来源和时间信息。
7. 逐步接入景点、酒店、天气、交通。
8. 增加餐厅 POI 查询能力。
9. 保持原有工具接口兼容。
10. 保证原有规划链路不发生功能退化。

# 五、非建设目标

本阶段暂不进行以下改造：

- 不引入 LangChain、LangGraph、CrewAI。
- 不重写整个 Agent 系统。
- 不重写 composer.py。
- 不重写现有路线规划算法。
- 不更换 SQLite。
- 不重新实现一套缓存和熔断系统。
- 不实现真实酒店及交通预订。
- 不将所有业务实体合并为统一数据库大表。

# 六、目标架构

## 6.1 总体架构

用户请求进入 Orchestrator。

Orchestrator 根据意图选择业务 Handler。

Handler 通过 DataService 获取业务数据。

DataService 统一协调 Repository、Provider、缓存和自动补全。

最终返回标准化 DataResult。

总体结构：

```text
用户请求
    |
    v
Orchestrator
    |
    v
Business Handler
    |
    v
DataService
    |
    +---- Repository
    |
    +---- Provider
    |
    +---- AutofetchAdapter
    |
    +---- CacheAdapter
    |
    v
DataResult
    |
    v
Handler 组织回答
```

## 6.2 模块职责

### Orchestrator

负责意图路由、上下文管理和业务流程调度。

### Business Handler

负责具体业务需求，例如天气查询、酒店查询、餐厅推荐和景点查询。

### DataService

负责统一数据查询入口、查询策略、数据标准化和异常处理。

### Repository

负责本地知识库查询。

### Provider

负责外部 API 调用及响应解析。

### AutofetchAdapter

负责适配已有 autofetch.py。

### CacheAdapter

负责适配已有缓存实现。

# 七、建议项目目录

```text
project/
├── orchestrator/
│   ├── orchestrator.py
│   ├── context.py
│   └── handlers/
│
├── services/
│   ├── __init__.py
│   ├── data_service.py
│   ├── data_result.py
│   ├── data_policy.py
│   │
│   ├── repositories/
│   │   ├── poi_repository.py
│   │   ├── hotel_repository.py
│   │   └── transport_repository.py
│   │
│   ├── providers/
│   │   ├── weather_provider.py
│   │   ├── poi_provider.py
│   │   ├── hotel_provider.py
│   │   └── transport_provider.py
│   │
│   └── adapters/
│       ├── autofetch_adapter.py
│       └── cache_adapter.py
│
├── real_tools.py
├── autofetch.py
├── dialogue.py
├── intent.py
├── composer.py
├── self_check.py
│
└── tests/
    ├── test_data_service.py
    ├── test_data_result.py
    ├── test_data_policy.py
    ├── test_poi_repository.py
    ├── test_hotel_repository.py
    ├── test_restaurant_service.py
    └── test_data_fallback.py
```

目录为目标结构，实际迁移以现有项目结构为准。

# 八、核心数据协议设计

## 8.1 DataResult

统一数据返回结构：

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DataResult:
    ok: bool
    data: Any = None

    source: str = "unknown"
    fetched_at: str | None = None

    stale: bool = False
    cached: bool = False

    error_code: str | None = None
    error_message: str | None = None

    metadata: dict = field(default_factory=dict)
```

字段说明：

| 字段 | 含义 |
|---|---|
| ok | 查询是否成功 |
| data | 业务数据 |
| source | 当前读取来源 |
| fetched_at | 数据获取时间 |
| stale | 是否过期 |
| cached | 是否来自缓存 |
| error_code | 错误类型 |
| error_message | 错误描述 |
| metadata | 扩展元数据 |

数据来源建议：

```python
SOURCE_KB = "knowledge_base"
SOURCE_STATIC = "static_seed"
SOURCE_API = "external_api"
SOURCE_CACHE = "cache"
SOURCE_HISTORY = "historical"
```

缓存数据还应记录：

```python
metadata = {
    "origin_source": "external_api"
}
```

## 8.2 状态约定

成功且有数据：

```python
DataResult(
    ok=True,
    data=[...]
)
```

成功但无数据：

```python
DataResult(
    ok=True,
    data=[]
)
```

查询失败：

```python
DataResult(
    ok=False,
    error_code="PROVIDER_TIMEOUT"
)
```

过期数据：

```python
DataResult(
    ok=True,
    data=[...],
    stale=True
)
```

# 九、数据获取策略 DataPolicy

建议定义：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class DataPolicy:
    ttl_seconds: int
    allow_stale: bool
    autofetch_on_empty: bool
    autofetch_on_error: bool
```

初始参考策略：

| 数据类型 | TTL | 允许过期 | 空结果补全 |
|---|---:|---|---|
| 景点基础信息 | 7 天 | 是 | 是 |
| 餐厅基础信息 | 1 天 | 是 | 是 |
| 酒店基础信息 | 1 天 | 是 | 是 |
| 酒店实时房态 | 不长期缓存 | 否 | 是 |
| 天气 | 30 分钟 | 谨慎 | 是 |
| 交通班次 | 5 分钟 | 否 | 是 |
| 历史同期天气 | 30 天 | 是 | 否 |

最终 TTL 需要结合 API 限制和真实数据更新频率调整。

# 十、Repository 设计

Repository 负责本地数据库访问，不承担业务回答和外部 API 请求。

建议先实现：

- POIRepository
- HotelRepository
- TransportRepository

示例接口：

```python
class POIRepository:

    def __init__(self, db):
        self.db = db

    def search(
        self,
        city,
        keyword=None,
        category=None
    ):
        ...
```

餐厅暂时复用 POIRepository，通过 category="restaurant" 查询。

不建议立即建立重复的餐厅数据表。

# 十一、Provider 设计

Provider 负责外部 API 调用。

建议实现：

- WeatherProvider
- POIProvider
- HotelProvider
- TransportProvider

职责：

1. 请求外部服务。
2. 解析响应。
3. 转换统一数据格式。
4. 处理接口异常。

Provider 不负责意图识别、行程编排和 Session 修改。

# 十二、AutofetchAdapter 设计

复用当前 autofetch.py 的能力。

目标：

```python
class AutofetchAdapter:

    def __init__(self, autofetch_module):
        self.autofetch = autofetch_module

    def fetch_city_data(self, city, data_type):
        ...
```

该代码是设计示意，不能直接认定当前 autofetch.py 存在同名函数。

实施前必须检查：

- 真实函数名称。
- 参数签名。
- 返回结构。
- 缓存机制。
- 熔断机制。
- 节流机制。
- 数据入库方式。

原则：复用旧能力，不重复实现。

# 十三、DataService 核心接口

建议目标接口：

```python
class DataService:

    def get_poi(
        self,
        city,
        keyword=None,
        category=None
    ):
        ...

    def get_restaurants(
        self,
        city,
        keyword=None,
        near=None,
        radius=None,
        category=None
    ):
        ...

    def get_hotels(
        self,
        city,
        check_in=None,
        check_out=None,
        guests=2
    ):
        ...

    def get_weather(
        self,
        city,
        date=None
    ):
        ...

    def get_transport(
        self,
        origin,
        destination,
        date=None
    ):
        ...
```

这些属于目标接口，实际参数应根据旧工具定义确认。

# 十四、数据查询流程

标准查询流程：

1. 接收查询请求。
2. 根据数据类型获取 DataPolicy。
3. 检查有效缓存。
4. 查询本地知识库。
5. 判断数据是否满足需求。
6. 必要时触发 autofetch。
7. 必要时调用外部 Provider。
8. 标准化返回数据。
9. 执行数据质量检查。
10. 返回 DataResult。

需要注意：

- 实时数据不能仅凭本地知识库就认定有效。
- API 失败不能解释成无数据。
- 过期数据必须显式标记。
- 历史天气不能伪装成实时预报。
- 酒店基本资料和酒店房态必须分开处理。

# 十五、餐厅数据专项建设

## 15.1 业务意图区分

| 用户表达 | 意图 |
|---|---|
| 成都有什么特色美食 | food_query |
| 成都有哪些好吃的火锅店 | restaurant_search |
| 春熙路附近有什么川菜馆 | restaurant_search |
| 成都火锅有什么特色 | food_query |

美食知识和真实餐厅 POI 必须分离。

## 15.2 餐厅数据结构

```python
{
    "name": "餐厅名称",
    "city": "成都",
    "address": "餐厅地址",
    "category": "火锅",
    "longitude": None,
    "latitude": None,
    "price_level": None,
    "rating": None,
    "business_hours": None,
    "source": "external_api",
    "fetched_at": None
}
```

无可靠数据时使用 None，不编造价格、评分和营业时间。

## 15.3 周边搜索

用户询问某个景点附近的餐厅时：

1. 获取景点坐标。
2. 确定坐标系。
3. 查询附近餐厅。
4. 根据半径筛选。
5. 计算距离。
6. 按距离和其他可靠指标排序。

不得混用不同坐标系的经纬度。

# 十六、数据库设计建议

优先复用现有 .kb.db。

可以根据审查结果决定是否增加：

## 16.1 数据来源日志表

```sql
CREATE TABLE IF NOT EXISTS data_source_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_type TEXT NOT NULL,
    city TEXT,
    source TEXT NOT NULL,
    success INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT
);
```

用途：

- 数据来源记录。
- API 成功率统计。
- 自动补全失败排查。
- 城市数据覆盖分析。

## 16.2 缓存元信息表

```sql
CREATE TABLE IF NOT EXISTS data_cache_meta (
    cache_key TEXT PRIMARY KEY,
    data_type TEXT NOT NULL,
    source TEXT NOT NULL,
    origin_source TEXT,
    fetched_at TEXT NOT NULL,
    expires_at TEXT,
    checksum TEXT
);
```

该表不是强制建设项。

如果 autofetch.py 已有完整缓存元数据机制，则不重复建立。

# 十七、旧工具兼容方案

保留原有六个工具名称和调用方式。

| 原工具 | 后续策略 |
|---|---|
| get_weather | 逐步转发至 DataService |
| search_transport | 逐步转发至 DataService |
| search_hotel | 逐步转发至 DataService |
| search_poi | 逐步转发至 DataService |
| calc_budget | 保持原样 |
| save_preference | 后续单独升级持久化记忆 |

迁移采用渐进式适配，不一次性修改所有工具。

# 十八、与 Orchestrator 的集成

目标调用链：

```text
用户请求
    |
IntentRouter
    |
RestaurantHandler
    |
DataService.get_restaurants()
    |
Repository / Provider
    |
DataResult
    |
RestaurantHandler
    |
最终回答
```

Handler 负责业务逻辑，DataService 负责数据访问。

不得把所有查询逻辑重新集中到 dialogue.py 或 DataService 中。

# 十九、开发任务计划

## 任务一：源码审查与接口盘点

检查：

- real_tools.py
- autofetch.py
- .kb.db
- 现有工具返回结构
- 当前测试目录

交付：

- 接口清单。
- 数据流图。
- 数据库结构清单。
- 兼容性风险清单。

## 任务二：建立统一结果协议

新增：

- data_result.py
- data_policy.py

交付：

- DataResult。
- DataPolicy。
- 单元测试。

## 任务三：实现 DataService 最小版本

新增：

- data_service.py
- autofetch_adapter.py
- 必要的 Repository 适配器。

先接入景点、酒店、天气和交通。

## 任务四：增加餐厅能力

完成：

- 城市餐厅检索。
- 餐厅关键词检索。
- 周边餐厅检索。
- 数据标准化。
- 空结果与异常处理。

## 任务五：接入业务 Handler

逐步接入：

- WeatherHandler。
- HotelHandler。
- RestaurantHandler。
- POIHandler。
- TransportHandler。

## 任务六：回归测试

验证旧工具、规划、多轮对话、缓存、异常恢复。

# 二十、测试方案

## 20.1 DataService 测试

- 知识库命中。
- 知识库未命中。
- 自动补全成功。
- 自动补全失败。
- API 超时。
- API 空数据。
- 缓存命中。
- 缓存过期。
- 熔断状态。
- 数据来源标记。

## 20.2 餐厅测试

- 城市餐厅查询。
- 关键词查询。
- 类别查询。
- 周边查询。
- 空结果。
- Provider 超时。
- 坐标系转换。

## 20.3 多轮对话测试

```text
成都三日游
    |
成都特色美食
    |
成都火锅店
    |
第二天景点修改
```

预期：

- 咨询不破坏行程。
- 修改不丢失槽位。
- 餐厅查询不会误触发规划。
- 原有规划逻辑保持正常。

# 二十一、验收标准

| 项目 | 标准 |
|---|---|
| 旧工具兼容 | 原有六个工具不被破坏 |
| 规划回归 | 原有规划测试通过 |
| 数据协议 | 统一 DataResult |
| 数据来源 | 能识别当前来源和原始来源 |
| 异常处理 | 空数据与查询失败区分 |
| 缓存策略 | 按数据类型管理 TTL |
| 餐厅检索 | 支持城市、关键词、周边 |
| 坐标处理 | 坐标系明确 |
| 多轮上下文 | 咨询不破坏已有计划 |
| 自动补全 | 复用旧缓存、熔断和节流能力 |

建议统计：

- 数据查询成功率。
- 知识库命中率。
- 缓存命中率。
- 自动补全成功率。
- 外部 API 失败率。
- 平均查询耗时。

# 二十二、风险与回滚策略

## 风险一：旧工具接口发生变化

应对：

保留旧工具作为兼容入口，不直接删除原实现。

## 风险二：缓存逻辑重复

应对：

实施前审查 autofetch.py，禁止无依据地新建第二套缓存。

## 风险三：实时数据被过期缓存覆盖

应对：

根据数据类型配置 TTL 和过期数据策略，实时房态及班次不得使用过期结果冒充实时数据。

## 风险四：新架构影响原有规划

应对：

优先使用适配器方式接入，不直接修改 composer.py 的核心算法。

## 风险五：数据来源标记错误

应对：

区分 cache source 和 origin source，保留原始数据来源。

## 回滚策略

1. 保留旧工具实现。
2. 新增 DataService 开关。
3. 逐个业务迁移。
4. 每次迁移后运行回归测试。
5. 发生问题时将对应 Handler 切回旧工具入口。
6. 不在没有备份的情况下执行数据库结构迁移。

# 二十三、后续开发路线

第二阶段完成后，建议进入：

第三阶段：路线优化引擎。

重点：

- 景点之间的实际交通时间。
- 营业时间约束。
- 游玩时长约束。
- 每日游玩强度。
- 跨区域折返优化。

第四阶段：持久化用户记忆。

重点：

- 用户长期偏好。
- 当前旅行约束。
- 用户历史旅行偏好。
- 临时要求与长期偏好分离。

第五阶段：动态行程管理。

重点：

- 局部行程修改。
- 整体路线重新优化。
- 修改前后差异。
- 行程版本管理。
- 计划回滚。

第六阶段：评测和产品化。

重点：

- 自动化评测。
- 工具调用日志。
- 异常追踪。
- 性能监控。
- 行程导出。
- 用户体验优化。

# 二十四、开发决策记录

| 编号 | 决策 | 原因 |
|---|---|---|
| ADR-001 | 不引入 LangChain/LangGraph | 保留自研调度机制 |
| ADR-002 | 新增 DataService | 统一数据访问入口 |
| ADR-003 | 保留 real_tools.py | 降低迁移风险 |
| ADR-004 | 复用 autofetch.py | 避免重复实现缓存和熔断 |
| ADR-005 | 统一 DataResult | 降低业务层耦合 |
| ADR-006 | 餐厅复用 POI 基础结构 | 减少重复数据模型 |
| ADR-007 | 实时数据和基础资料分开处理 | 保证数据时效性 |
| ADR-008 | 渐进式迁移 | 保证原有功能稳定 |

# 二十五、待确认事项

以下事项必须在实际源码审查后确定：

1. real_tools.py 中六个工具的真实函数签名。
2. agent.dispatch 的真实返回协议。
3. autofetch.py 的函数、缓存和熔断接口。
4. .kb.db 的完整表结构。
5. 当前 POI 坐标系及转换机制。
6. 酒店数据是否区分基础资料和实时房态。
7. 当前缓存是否已有过期标记。
8. 外部 API 的限流和错误处理方式。
9. 当前测试框架的运行入口。
10. Orchestrator 第一阶段的最终代码结构。

本档案中的代码接口均属于目标设计或示意代码，不应在未经源码核对的情况下直接视为现有实现。

---

# 文档结论

晴航第二阶段的核心不是增加更多工具，而是建设统一、可靠、可扩展的数据服务基础设施。

实施过程中坚持：

**保留旧系统、逐步抽象、统一数据协议、复用现有能力、保证业务兼容。**

第二阶段完成后，晴航将拥有更加清晰的数据访问架构，为后续路线优化、餐厅推荐、持久化记忆和动态行程管理提供基础。
