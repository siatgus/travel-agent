# 行野智能旅行助手
# 第三阶段开发档案：智能路线规划引擎（Route Engine）

**项目名称：** 行野（Xingye）智能旅行助手  
**开发阶段：** 第三阶段  
**模块名称：** Route Engine（智能路线规划引擎）  
**前置阶段：** 第一阶段 Orchestrator 架构升级、第二阶段 DataService 统一数据服务  
**开发语言：** Python 3.10+  
**核心目标：** 建立独立、可扩展、可测试的路线优化引擎，为 Composer 提供合理的景点访问顺序和时间安排。

---

# 一、项目背景

目前行野已经具备基础的旅行规划能力，可以通过 DataService 获取景点、酒店、天气和交通数据，并通过 Composer 将景点分配到不同日期。

但现有规划方式仍然存在一些不足：

1. 景点分配主要依赖规则和地理距离，缺少系统性的路线优化。
2. 景点访问顺序不一定是最短或最合理的。
3. 没有充分考虑景点营业时间、建议游玩时长和时间窗口。
4. 对用户指定的游玩顺序、必去景点和时间限制，缺少统一的约束处理机制。
5. 当用户临时增加、删除景点时，缺少局部路线重新优化能力。
6. 路线距离与实际交通耗时之间需要更加明确的区分。

因此，需要建设独立的 Route Engine，将路线计算从 Composer 中分离出来。

# 二、阶段建设目标

本阶段不推翻现有旅行规划架构，而是在现有规则规划链路上增加独立的路线优化能力。

核心目标：

- 建立统一的路线数据模型。
- 建立景点之间的距离和交通时间矩阵。
- 实现基础路线排序算法。
- 引入 2-opt 局部优化算法。
- 支持景点营业时间和游玩时长约束。
- 生成每日详细时间表。
- 支持增加、删除景点后的局部路线调整。
- 与现有 Composer、Self Check 和 Orchestrator 集成。
- 保证原有行程规划功能兼容。

**本阶段不追求复杂算法，而是优先建立可靠、可解释、可测试的路线优化基础设施。**

# 三、总体架构设计

## 3.1 目标架构

```text
                 Orchestrator
                       |
                       v
                  PlanHandler
                       |
                       v
                  DataService
                       |
              景点及路线基础数据
                       |
                       v
                  RouteEngine
                       |
          +------------+------------+
          |            |            |
          v            v            v
    DistanceMatrix  Optimizer   TimeWindow
          |            |            |
          +------------+------------+
                       |
                       v
                 ScheduleBuilder
                       |
                       v
                 ConstraintChecker
                       |
                       v
                 RouteResult
                       |
                       v
                   Composer
                       |
                       v
                  SelfCheck
                       |
                       v
                  ReplyBuilder
```

## 3.2 模块职责

| 模块 | 主要职责 |
|---|---|
| DataService | 提供景点坐标、营业时间、游玩时长等数据 |
| DistanceMatrix | 计算景点之间的距离和交通耗时 |
| RouteOptimizer | 优化景点访问顺序 |
| TimeWindow | 处理营业时间和可访问时间窗口 |
| ScheduleBuilder | 根据路线生成每日时间安排 |
| ConstraintChecker | 检查路线及时间安排是否违反约束 |
| RouteAdjuster | 处理用户修改行程后的局部重新规划 |
| RouteEngine | 统一调度上述模块 |
| Composer | 将路线结果组织成完整旅行计划 |
| SelfCheck | 对最终旅行计划进行整体检查 |

# 四、项目目录设计

建议新增以下目录：

```text
route_engine/
├── __init__.py
├── models.py
├── route_engine.py
├── distance_matrix.py
├── route_optimizer.py
├── time_window.py
├── schedule_builder.py
├── constraint_checker.py
├── route_adjuster.py
└── config.py

tests/
└── route_engine/
    ├── test_models.py
    ├── test_distance_matrix.py
    ├── test_route_optimizer.py
    ├── test_time_window.py
    ├── test_schedule_builder.py
    ├── test_constraint_checker.py
    ├── test_route_adjuster.py
    └── test_route_engine.py
```

注意：以上是目标目录结构。正式开发前需要检查现有源码，确认是否已经存在类似功能，避免重复实现。

# 五、核心数据模型设计

## 5.1 景点模型 RouteNode

用于表示路线中的一个访问节点。

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class RouteNode:
    id: str
    name: str

    latitude: float
    longitude: float

    visit_duration: int = 90

    opening_time: Optional[str] = None
    closing_time: Optional[str] = None

    priority: int = 0

    required: bool = False
    preferred_order: Optional[int] = None
```

字段说明：

| 字段 | 说明 |
|---|---|
| id | 景点唯一标识 |
| name | 景点名称 |
| latitude | 纬度 |
| longitude | 经度 |
| visit_duration | 建议游玩时长，单位分钟 |
| opening_time | 开放时间 |
| closing_time | 闭园时间 |
| priority | 景点优先级 |
| required | 是否为必去景点 |
| preferred_order | 用户指定的访问顺序 |

注意：

- 坐标必须明确坐标系。
- 高德地图通常使用 GCJ-02。
- OpenStreetMap、OSRM 等服务通常使用 WGS84。
- 不允许未经转换直接混用坐标。
- 营业时间可能存在全天开放、分时段开放、临时闭园等情况，后续需要扩展为结构化时间窗口。

## 5.2 RouteRequest

```python
@dataclass
class RouteRequest:
    nodes: list[RouteNode]

    start_time: str = "09:00"
    end_time: str = "18:00"

    travel_mode: str = "driving"

    max_visits: Optional[int] = None

    must_visit: list[str] = None
    excluded: list[str] = None

    optimize: bool = True
```

用于承载一次路线优化请求。

## 5.3 RouteLeg

表示两个访问节点之间的交通信息。

```python
@dataclass
class RouteLeg:
    from_id: str
    to_id: str

    distance_meters: float
    duration_seconds: float

    source: str
```

其中：

- distance_meters：实际或估算道路距离。
- duration_seconds：交通耗时。
- source：数据来源，例如 OSRM、高德或直线距离估算。

## 5.4 RouteStop

表示优化后的访问节点。

```python
@dataclass
class RouteStop:
    node: RouteNode

    arrival_time: str
    departure_time: str

    travel_duration: int
    visit_duration: int
```

## 5.5 RouteResult

```python
@dataclass
class RouteResult:
    success: bool

    stops: list[RouteStop]

    total_distance: float
    total_travel_time: int
    total_visit_time: int

    algorithm: str

    warnings: list[str]
    errors: list[str]
```

RouteResult 是 Route Engine 与 Composer 之间的主要数据交换对象。

实际实现时应增加默认值、类型校验以及空列表的安全处理。

# 六、核心算法设计

## 6.1 距离矩阵 DistanceMatrix

目标：

建立所有景点两两之间的距离和交通时间矩阵。

例如有四个景点：

```text
A：宽窄巷子
B：锦里
C：武侯祠
D：杜甫草堂
```

最终生成：

```text
          A       B       C       D

A         0      25      30      35
B        25       0      10      40
C        30      10       0      30
D        35      40      30       0
```

以上数字仅用于说明矩阵结构，不代表真实交通时间。

矩阵数据应同时保存：

- 距离。
- 交通时间。
- 数据来源。
- 查询时间。
- 是否为估算值。

建议实现：

```python
class DistanceMatrix:

    def build(self, nodes, travel_mode="driving"):
        pass

    def get_distance(self, from_id, to_id):
        pass

    def get_duration(self, from_id, to_id):
        pass
```

实现要求：

1. 优先使用现有 OSRM 路线能力。
2. 根据实际情况复用高德路线 API。
3. 外部路线服务失败时，可以使用直线距离估算进行降级。
4. 降级结果必须明确标记为估算值。
5. 不得将直线距离直接当作道路距离。
6. 设置请求超时、缓存和调用限制。
7. 同一批节点尽量批量构建矩阵，减少外部 API 请求次数。

## 6.2 贪心路线排序

第一版使用最近邻贪心算法。

基本思路：

1. 确定起点。
2. 找出当前节点到其他未访问节点中交通耗时最短的节点。
3. 将该节点加入访问序列。
4. 更新当前位置。
5. 重复，直到所有节点处理完成。

伪代码：

```python
def nearest_neighbor(nodes, matrix, start_id):

    unvisited = set(node.id for node in nodes)
    route = []

    current = start_id

    while unvisited:

        candidates = [
            node_id
            for node_id in unvisited
            if node_id != current
        ]

        if not candidates:
            break

        next_node = min(
            candidates,
            key=lambda node_id:
                matrix.get_duration(current, node_id)
        )

        route.append(next_node)

        unvisited.remove(next_node)

        current = next_node

    return route
```

需要注意：

最近邻算法只解决基础排序问题，不保证全局最优。

## 6.3 2-opt 局部优化

在贪心排序基础上，引入 2-opt 优化算法。

核心思想：

通过反转路线中的一段节点，尝试减少整体路线成本。

例如：

```text
优化前：

A → B → C → D → E

优化后：

A → D → C → B → E
```

当优化后的路线成本低于原路线时，接受调整。

优化目标：

- 减少总交通耗时。
- 减少总道路距离。
- 尽量避免路线交叉和重复绕行。

建议实现：

```python
class RouteOptimizer:

    def nearest_neighbor(self, nodes, matrix):
        pass

    def two_opt(self, route, matrix):
        pass

    def optimize(self, nodes, matrix):
        pass
```

第一版使用交通时间作为主要优化指标，道路距离作为辅助指标。

必须设置迭代次数或时间上限，防止节点数量增加后计算时间失控。

## 6.4 时间窗口 TimeWindow

仅仅计算最短路线是不够的。

例如：

```text
景点 A：09:00 - 17:00
景点 B：10:00 - 18:00
景点 C：全天开放
```

如果上午 09:00 出发，按照距离最短的顺序先到 B，可能无法在 09:00 入场。

因此需要引入时间窗口。

核心处理：

1. 获取景点营业时间。
2. 获取预计到达时间。
3. 判断是否处于营业时间内。
4. 如果提前到达，则计算等待时间。
5. 如果到达时已经闭园，则判断能否调整顺序。
6. 如果无法满足，则输出冲突信息。

建议接口：

```python
class TimeWindow:

    def is_open(self, node, arrival_time):
        pass

    def calculate_wait(self, node, arrival_time):
        pass

    def find_next_available(self, node, arrival_time):
        pass
```

第一版只支持每日单个连续营业时间窗口。

后续再扩展：

- 上午、下午分时段营业。
- 午休闭馆。
- 指定日期闭馆。
- 节假日营业时间。
- 临时闭园。

# 七、每日时间表生成

ScheduleBuilder 负责把优化后的路线转换成可以执行的旅行时间表。

示例：

| 时间 | 行程 |
|---|---|
| 09:00 - 09:30 | 前往宽窄巷子 |
| 09:30 - 11:00 | 游览宽窄巷子 |
| 11:00 - 11:30 | 前往附近餐厅 |
| 11:30 - 13:00 | 午餐及休息 |
| 13:00 - 13:20 | 前往人民公园 |
| 13:20 - 14:30 | 游览人民公园 |
| 14:30 - 15:00 | 前往武侯祠 |
| 15:00 - 17:00 | 游览武侯祠 |

以上为结构示例，不代表真实路线耗时。

需要支持：

- 出发时间。
- 景点游玩时长。
- 景点间交通耗时。
- 等待时间。
- 午餐时间。
- 午休时间。
- 返回酒店时间。
- 每日结束时间。

建议接口：

```python
class ScheduleBuilder:

    def build(
        self,
        route,
        matrix,
        start_time,
        end_time
    ):
        pass
```

注意：午餐、休息、酒店等节点可以由 Composer 继续管理。Route Engine 应允许这些节点参与路线计算，但不能擅自把所有餐饮和休息节点当作普通景点处理。

# 八、约束检查 ConstraintChecker

路线优化完成后，必须进行约束检查。

检查项目：

| 检查项 | 检查内容 |
|---|---|
| 营业时间 | 到达时间是否在开放时间内 |
| 必去景点 | 是否包含所有必去节点 |
| 排除景点 | 是否包含用户明确排除的节点 |
| 时间冲突 | 是否出现行程重叠 |
| 每日时长 | 是否超过用户允许的游玩时长 |
| 交通耗时 | 是否存在不合理的交通时间 |
| 游玩时长 | 是否满足合理的停留时间 |
| 访问顺序 | 是否违反用户指定顺序 |
| 数据可信度 | 是否使用了估算路线数据 |

建议接口：

```python
class ConstraintChecker:

    def check_route(self, route, request):
        pass

    def check_schedule(self, schedule, request):
        pass

    def check_required_nodes(self, route, request):
        pass
```

必须区分：

- 硬约束：必须满足，否则不能标记为成功。
- 软约束：尽量满足，无法满足时输出警告。

例如：

硬约束：
- 用户明确要求必须游览的景点。
- 用户明确排除的景点。
- 用户指定的不可调整时间。

软约束：
- 尽量减少交通时间。
- 尽量避免折返。
- 尽量按照用户偏好安排景点。
- 尽量减少等待时间。

# 九、动态路线调整 RouteAdjuster

用户修改行程时，不应该每次都重新规划全部旅行。

例如：

用户：
“第二天增加杜甫草堂。”

系统应当：

1. 获取当前第二天行程。
2. 获取杜甫草堂数据。
3. 构建局部距离矩阵。
4. 将新景点加入候选节点。
5. 重新计算访问顺序。
6. 检查时间窗口。
7. 检查每日总时长。
8. 返回调整结果。

其他支持场景：

- 删除一个景点。
- 替换一个景点。
- 调整景点顺序。
- 指定某个景点优先游览。
- 用户要求减少交通时间。
- 某景点临时闭园。

建议接口：

```python
class RouteAdjuster:

    def add_node(self, current_route, node):
        pass

    def remove_node(self, current_route, node_id):
        pass

    def replace_node(self, current_route, old_id, new_node):
        pass

    def reorder(self, current_route, preferred_order):
        pass
```

重要原则：

用户明确指定的顺序和时间属于硬约束，不能为了优化路线擅自修改。

路线调整后必须重新执行 ConstraintChecker 和 SelfCheck。

# 十、RouteEngine 统一入口

RouteEngine 负责调度所有子模块。

目标接口：

```python
class RouteEngine:

    def __init__(
        self,
        distance_matrix,
        optimizer,
        schedule_builder,
        constraint_checker
    ):
        self.distance_matrix = distance_matrix
        self.optimizer = optimizer
        self.schedule_builder = schedule_builder
        self.constraint_checker = constraint_checker

    def plan(self, request):

        # 1. 构建距离矩阵
        matrix = self.distance_matrix.build(
            request.nodes,
            request.travel_mode
        )

        # 2. 优化路线顺序
        route = self.optimizer.optimize(
            request.nodes,
            matrix
        )

        # 3. 生成时间表
        schedule = self.schedule_builder.build(
            route,
            matrix,
            request.start_time,
            request.end_time
        )

        # 4. 检查约束
        check = self.constraint_checker.check_schedule(
            schedule,
            request
        )

        # 5. 返回统一结果
        return schedule, check
```

以上为结构示意，正式开发时应统一返回 RouteResult，并明确处理矩阵构建失败、无可行路线、部分节点无法访问等情况。

# 十一、与现有系统集成

## 11.1 与 DataService 集成

DataService 负责提供：

- 景点坐标。
- 景点名称。
- 营业时间。
- 建议游玩时长。
- 景点类别。
- 数据来源。

Route Engine 不直接访问数据库，不直接请求 POI 数据源。

## 11.2 与 Composer 集成

原有流程：

```text
DataService
    ↓
Composer
    ↓
SelfCheck
```

升级后：

```text
DataService
    ↓
RouteEngine
    ↓
Composer
    ↓
SelfCheck
```

Composer 仍然负责：

- 按天分配景点。
- 添加酒店和餐饮安排。
- 生成完整行程。
- 生成最终 Markdown。
- 输出地图数据。

Route Engine 只负责路线优化和时间安排。

## 11.3 与 Orchestrator 集成

Orchestrator 不直接参与算法计算。

它只负责：

1. 接收规划请求。
2. 调用 PlanHandler。
3. PlanHandler 获取 DataService 数据。
4. 调用 RouteEngine。
5. 将 RouteResult 交给 Composer。
6. 执行最终检查。
7. 返回 Reply。

## 11.4 与现有规划流程兼容

必须保留现有：

```text
plan_trip
composer
self_check
Reply
```

不能直接替换原有规划流水线。

建议增加配置开关：

```python
ROUTE_ENGINE_ENABLED = True
```

关闭时继续使用旧路线编排逻辑。

新旧路线结果必须能够对比，方便出现问题时快速回滚。

# 十二、开发实施计划

## 第一阶段：源码审查

目标：掌握现有路线计算能力。

重点审查：

- composer.py
- real_tools.py
- autofetch.py
- self_check.py
- dialogue.py
- 当前地图数据结构
- 当前 OSRM 调用方式

需要确认：

1. 现有 Composer 如何分配每日景点。
2. 现有路线耗时如何计算。
3. 是否已有距离矩阵或缓存。
4. 坐标使用什么坐标系。
5. 地图前端接收什么数据格式。
6. SelfCheck 当前检查哪些约束。

交付物：路线模块源码审查报告。

## 第二阶段：基础数据模型

实现：

- RouteNode
- RouteRequest
- RouteLeg
- RouteStop
- RouteResult

验收标准：

- 字段校验正确。
- 缺失数据能够安全处理。
- 坐标异常能够识别。
- 不影响原有工具和 Composer。

## 第三阶段：距离矩阵

实现：

- 节点距离矩阵。
- 节点交通耗时矩阵。
- OSRM 路线数据接入。
- 估算数据降级。
- 数据缓存。

验收标准：

- 支持多个景点之间的路线计算。
- API 失败不会导致整个系统崩溃。
- 数据来源明确。
- 不混用不同坐标系。

## 第四阶段：路线优化算法

实现：

- 最近邻算法。
- 2-opt 算法。
- 路线成本计算。
- 最大迭代次数限制。

验收标准：

- 路线包含所有必须访问的节点。
- 优化结果不劣于原始路线。
- 相同输入和配置得到稳定结果。
- 优化失败时能够回退。

## 第五阶段：时间窗口与时间表

实现：

- 营业时间检查。
- 等待时间计算。
- 游玩时长计算。
- 每日时间表生成。
- 时间冲突识别。

验收标准：

- 不安排在闭园时间进入景点。
- 不产生负数交通时间。
- 不出现访问时间重叠。
- 无法满足的时间约束必须明确告知。

## 第六阶段：动态调整

实现：

- 增加景点。
- 删除景点。
- 替换景点。
- 调整访问顺序。

验收标准：

- 修改第二天行程不会影响其他日期。
- 不丢失用户原有硬约束。
- 调整后重新执行约束检查。
- 修改失败时保留原始方案。

## 第七阶段：系统集成

将 RouteEngine 接入 Composer。

验收标准：

- 原有旅行规划功能正常。
- 旧版路线模式可随时回退。
- 地图展示正常。
- Markdown 输出正常。
- 多轮对话状态不丢失。
- 原有测试全部通过。

# 十三、测试方案

## 13.1 单元测试

| 测试模块 | 测试内容 |
|---|---|
| models | 数据模型字段及异常输入 |
| distance_matrix | 距离、交通时间矩阵 |
| route_optimizer | 贪心和 2-opt 优化 |
| time_window | 营业时间与等待时间 |
| schedule_builder | 时间表生成 |
| constraint_checker | 硬约束和软约束 |
| route_adjuster | 增删改及顺序调整 |
| route_engine | 完整路线计算链路 |

## 13.2 场景测试

场景一：普通三景点路线。

输入三个景点及其坐标，验证访问顺序和交通时间。

场景二：指定必去景点。

用户要求必须游览宽窄巷子和武侯祠，验证两个节点均保留。

场景三：景点营业时间冲突。

用户要求上午 09:00 游览一个 10:00 才开放的景点，验证系统能否计算等待时间或重新排序。

场景四：增加景点。

用户要求第二天增加一个景点，验证只调整第二天行程。

场景五：删除景点。

删除一个非必去景点，验证路线重新优化。

场景六：路线服务不可用。

模拟 OSRM 请求失败，验证降级处理及数据可信度提示。

场景七：用户指定访问顺序。

用户明确要求先去 A，再去 B，验证优化算法不会破坏硬性顺序。

场景八：路线不可行。

所有约束无法同时满足时，系统应返回冲突说明，而不是伪造可行路线。

## 13.3 回归测试

必须运行现有自建测试框架。

重点关注：

- 原有行程规划。
- 多轮行程修改。
- 指定日期规划。
- 指定景点规划。
- 预算计算。
- 地图渲染。
- 交通信息。
- 天气信息。
- 方案存档。

不得因为新增 Route Engine 导致旧功能退化。

# 十四、性能与稳定性要求

1. 路线优化算法必须设置迭代上限。
2. 距离矩阵优先批量获取。
3. 对外部路线 API 设置超时。
4. 对重复路线请求进行缓存。
5. 对异常节点进行隔离。
6. 单个景点数据缺失不应直接导致整个规划崩溃。
7. 所有估算路线必须保留来源标记。
8. 路线调整必须支持失败回滚。
9. 相同输入应尽可能产生确定性结果。
10. 路线计算过程应记录必要日志，便于问题排查。

# 十五、本阶段暂不开发的内容

为了控制开发范围，本阶段暂不引入：

- 遗传算法。
- 强化学习。
- 复杂多目标优化算法。
- OR-Tools 全量约束求解。
- 实时交通动态预测。
- 实时人流预测。
- 自动酒店预订。
- 自动门票购买。
- 大规模分布式路线计算。

这些能力可以在后续阶段根据实际需求逐步引入。

# 十六、最终验收标准

本阶段完成后，必须达到以下标准：

- [ ] 建立独立 Route Engine 模块。
- [ ] 建立统一路线数据模型。
- [ ] 支持景点距离矩阵。
- [ ] 支持交通耗时矩阵。
- [ ] 支持最近邻路线排序。
- [ ] 支持 2-opt 优化。
- [ ] 支持营业时间约束。
- [ ] 支持游玩时长计算。
- [ ] 支持每日时间表生成。
- [ ] 支持硬约束和软约束检查。
- [ ] 支持景点增删改后的路线调整。
- [ ] 支持路线数据降级和来源标记。
- [ ] 成功接入 Composer。
- [ ] 保持 SelfCheck 正常工作。
- [ ] 保持 Orchestrator 调用链兼容。
- [ ] 现有回归测试全部通过。
- [ ] 具备新旧路线模式切换和回滚能力。

# 十七、后续开发路线

Route Engine 完成后，建议继续以下阶段：

| 阶段 | 模块 | 核心目标 |
|---|---|---|
| 第一阶段 | Orchestrator | 统一调度和业务路由 |
| 第二阶段 | DataService | 统一数据获取和管理 |
| 第三阶段 | Route Engine | 智能路线优化 |
| 第四阶段 | Memory System | 持久化用户偏好与旅行记忆 |
| 第五阶段 | Dynamic Trip Management | 动态行程管理 |
| 第六阶段 | Travel Execution | 提醒、预警、预订入口、行程导出 |
| 第七阶段 | Evaluation System | 智能体评测与产品化 |

---

# 十八、开发原则

整个开发过程必须遵循以下原则：

1. **渐进式改造：** 不推翻现有系统，不一次性重写核心模块。
2. **规则优先：** 路线计算、时间计算、约束检查由确定性代码负责。
3. **数据可信：** 真实路线和估算路线必须明确区分。
4. **模块解耦：** Route Engine 不直接操作数据库，也不直接生成最终回复。
5. **兼容优先：** 保留现有 Composer 和 SelfCheck 的调用能力。
6. **测试驱动：** 每增加一个模块，都必须增加对应测试。
7. **可回滚：** 新路线算法出现问题时，能够快速切回旧逻辑。
8. **用户约束优先：** 不能为了缩短路线而破坏用户明确指定的时间、顺序和必去要求。

**本阶段最终目标：让行野不仅能够规划去哪些景点，还能够合理决定什么时候去、先去哪里、如何减少交通时间，以及用户修改行程后如何重新安排。**
