"""DataService：统一数据查询入口（行野 2.0 第二阶段核心，文档 §13/§14）。

标准查询流程：

    1. 接收查询请求
    2. 按 data_type 取 DataPolicy
    3. 转发旧工具（KB 优先 → 静态种子回落 + 筛选排序，契约在 real_tools）
    4. 包装成 DataResult，附来源/新鲜度元数据（Repository.city_meta）
    5. 「城市确实无数据」（NO_POI/NO_HOTEL）且策略允许 → AutofetchAdapter 补库
    6. 补到 → 重查并刷新元数据；补不到 → 保留失败语义（空 ≠ 失败）
    7. 补库/调用记录落 data_source_log

兼容契约（ADR-003/ADR-008）：
- real_tools 六个工具原样保留，DataService 只是转发 + 包装，不改变任何
  返回字段语义；规划链路（composer/plan_trip）继续走旧入口，零感知。
- 渐进式迁移：对话层每迁移一个 Handler，行为必须与旧路径逐字等价
  （回归测试 test_travel_query_routing 固化）。

原则重申：
- 实时数据不能仅凭本地知识库就认定有效（stale 标记 + 说话层口径说明）。
- API 失败不能解释成无数据（ok=False vs ok=True data=[] 严格分开）。
- 酒店基础资料与实时房态分开：本层只提供基础资料与起价口径，
  绝不声称任何日期「有房」。
"""

from __future__ import annotations

from .. import tools
from .adapters import (
    AutofetchAdapter, FoodRepository, HotelRepository, POIRepository,
    TransportProvider, TransportRepository, WeatherProvider,
)
from .data_policy import is_stale, policy_of
from .data_result import (
    ORIGIN_BY_ROW_SOURCE, SOURCE_API, SOURCE_KB, SOURCE_STATIC,
    SOURCE_UNKNOWN, DataResult,
)


def _haversine(a: list[float], b: list[float]) -> float:
    try:
        from ..tools import haversine_km
        return haversine_km(a, b)
    except Exception:
        return 0.0


class DataService:
    """业务 Handler 的唯一数据访问入口。依赖全部可注入（测试无网络）。"""

    def __init__(self, db=None, autofetch_mod=None,
                 poi_fn=None, hotel_fn=None,
                 weather_fn=None, transport_fn=None):
        self.poi_repo = POIRepository(db)
        self.hotel_repo = HotelRepository(db)
        self.transport_repo = TransportRepository(db)
        self.food_repo = FoodRepository(db)
        self.autofetch = AutofetchAdapter(autofetch_mod, db)
        # 旧工具转发（可注入桩；默认走门面，USE_REAL_DATA=0 时自动切 mock）
        self._poi_fn = poi_fn or (lambda args: tools.dispatch("search_poi", args))
        self._hotel_fn = hotel_fn or (lambda args: tools.dispatch("search_hotel", args))
        self.weather = WeatherProvider(weather_fn)
        self.transport = TransportProvider(transport_fn)

    # ================================================================ POI
    def get_pois(self, city: str, keyword: str | None = None,
                 category: str = "attraction", limit: int = 8,
                 near: str | None = None, *, autofetch: bool = True) -> DataResult:
        return self._query_catalogue(
            city, data_type="attraction", category=category, keyword=keyword,
            near=near, limit=limit, autofetch=autofetch)

    # ================================================================ 餐厅
    def get_restaurants(self, city: str, keyword: str | None = None,
                        near: str | None = None, limit: int = 6,
                        *, autofetch: bool = True) -> DataResult:
        """餐厅检索（文档 §15）：城市/关键词/片区， category 固定 restaurant。

        near 是片区词（如「春熙路」）——旧 search_poi 的 area 优先排序语义；
        坐标级周边检索用 get_restaurants_near。
        """
        return self._query_catalogue(
            city, data_type="restaurant", category="restaurant",
            keyword=keyword, near=near, limit=limit, autofetch=autofetch)

    def get_restaurants_near(self, city: str, anchor_geo: list[float],
                             keyword: str | None = None,
                             radius_ladder: tuple[float, ...] = (3.0, 10.0),
                             limit: int = 6, exclude: str | None = None,
                             *, autofetch: bool = True) -> DataResult:
        """坐标级周边餐厅（文档 §15.3）：锚点坐标 → 半径圈层 → 距离排序。

        坐标系：库内统一 WGS-84，haversine 直线距离（与对话层旧逻辑一致）；
        高德 GCJ-02 纠偏只发生在前端出口（render_html.ll），不在数据层。
        品类词不在招牌上时放宽（命中优先、无命中保留全池）——与餐厅搜索同策略。
        """
        lat, lon = anchor_geo[0], anchor_geo[1]
        pool = self.poi_repo.pool(city, "restaurant")
        if pool is None:
            pool = [p for p in self.poi_repo.static_pool(city)
                    if (p.get("category") or "") == "restaurant"]
        if not pool and autofetch and policy_of("restaurant").autofetch_on_empty:
            af = self.autofetch.ensure(city, "restaurant")
            if af.get("status") == "fetched":
                pool = self.poi_repo.pool(city, "restaurant") or []
        if not pool:
            return DataResult.empty(source=SOURCE_UNKNOWN,
                                    message=f"{city} 没有餐厅数据")
        if keyword:
            hit = [p for p in pool
                   if keyword in (p.get("name") or "")
                   or any(keyword in t for t in (p.get("tags") or []))]
            pool = hit or pool
        res = None
        for radius in radius_ladder:
            near = self._within(lat, lon, pool, exclude, radius)
            if not near:
                continue
            res = DataResult.success(near[:limit], source=SOURCE_UNKNOWN,
                                     radius_km=radius, anchor_geo=list(anchor_geo))
            break
        if res is None:
            res = DataResult.empty(source=SOURCE_UNKNOWN,
                                   message="半径内没有收录餐厅")
            res.metadata["radius_ladder"] = list(radius_ladder)
        self._enrich(res, "restaurant", city, policy_of("restaurant"))
        return res

    # ================================================================ 酒店
    def get_hotels(self, city: str, check_in: str, check_out: str,
                   guests: int = 2, rooms: int = 1,
                   max_price_per_night: float | None = None,
                   preferred_area: str | None = None,
                   star_level: int | None = None,
                   *, autofetch: bool = True) -> DataResult:
        """酒店基础资料与在售起价口径——绝不冒充实时房态（ADR-007）。"""
        args: dict = {"city": city, "check_in": check_in,
                      "check_out": check_out, "guests": guests}
        if rooms != 1:
            args["rooms"] = rooms
        if max_price_per_night:
            args["max_price_per_night"] = max_price_per_night
        if preferred_area:
            args["preferred_area"] = preferred_area
        if star_level:
            args["star_level"] = star_level
        policy = policy_of("hotel")
        res = DataResult.from_tool_result(self._hotel_fn(args))
        if res.ok:
            self._enrich(res, "hotel", city, policy)
        if res.is_empty_no_data() and autofetch and policy.autofetch_on_empty:
            af = self.autofetch.ensure(city, "hotel")
            res.metadata["autofetch"] = af
            if af.get("status") == "fetched":
                res = DataResult.from_tool_result(self._hotel_fn(args))
                if res.ok:
                    self._enrich(res, "hotel", city, policy)
                res.metadata["autofetch"] = af
        return res

    # ================================================================ 天气 / 交通
    def get_weather(self, city: str, start_date: str,
                    end_date: str) -> DataResult:
        """转发 WeatherProvider；结果是否为预报/历史同期由 raw.days[].is_forecast 标注。"""
        res = self.weather.get(city, start_date, end_date)
        if res.ok:
            res.source = SOURCE_API
            res.metadata.setdefault("origin_tool", "get_weather")
        return res

    def get_transport(self, from_city: str, to_city: str, date: str,
                      transport_type: str = "any",
                      party_size: int = 1) -> DataResult:
        """班次为 12306/航司静态口径；policy 不允许把过期结果当实时（allow_stale=False）。"""
        return self.transport.search(from_city, to_city, date,
                                     transport_type, party_size)

    # ================================================================ 美食知识
    def get_food_knowledge(self, city: str) -> DataResult:
        """菜品/文化知识（food_knowledge 表）——与真实餐厅 POI 分表（§15.1）。"""
        rows = self.food_repo.list(city)
        if not rows:
            return DataResult.empty(source=SOURCE_KB,
                                    message=f"{city} 暂无美食知识收录")
        return DataResult.success(rows, source=SOURCE_KB)

    # ================================================================ 补全
    def ensure(self, city: str, data_type: str, *,
               force: bool = False, client=None) -> dict:
        """定向补库（透传 autofetch.ensure_city_data 的 status 字典）。"""
        return self.autofetch.ensure(city, data_type, force=force, client=client)

    # ================================================================ 内部
    def _query_catalogue(self, city: str, *, data_type: str, category: str,
                         keyword: str | None, near: str | None, limit: int,
                         autofetch: bool) -> DataResult:
        """search_poi 转发 + 元数据 + 空结果补全（POI/餐厅共用）。"""
        args: dict = {"city": city, "category": category, "limit": limit}
        if keyword:
            args["keyword"] = keyword
        if near:
            args["near"] = near
        policy = policy_of(data_type)
        res = DataResult.from_tool_result(self._poi_fn(args))
        if res.ok:
            self._enrich(res, data_type, city, policy)
        if res.is_empty_no_data() and autofetch and policy.autofetch_on_empty:
            af = self.autofetch.ensure(city, data_type)
            res.metadata["autofetch"] = af
            if af.get("status") == "fetched":
                res = DataResult.from_tool_result(self._poi_fn(args))
                if res.ok:
                    self._enrich(res, data_type, city, policy)
                res.metadata["autofetch"] = af
        return res

    def _enrich(self, res: DataResult, data_type: str, city: str,
                policy) -> None:
        """成功结果补来源/新鲜度元数据（不改 data 本身）。"""
        repo = self.poi_repo if data_type != "hotel" else self.hotel_repo
        meta = repo.meta(city)
        if not meta:
            # kb 无行：数据来自静态种子回落（real_tools 内部契约）
            res.source = SOURCE_STATIC
            res.metadata.setdefault("origin_source", "static_seed")
            return
        res.source = SOURCE_KB
        res.fetched_at = meta.get("updated_at")
        res.metadata["row_sources"] = meta.get("source") or ""
        origins = {ORIGIN_BY_ROW_SOURCE.get(s.strip(), s.strip())
                   for s in (meta.get("source") or "").split(",") if s.strip()}
        res.metadata["origin_source"] = "+".join(sorted(origins)) or "unknown"
        res.stale = is_stale(policy, res.fetched_at,
                             res.metadata["row_sources"])

    @staticmethod
    def _within(lat: float, lon: float, items: list[dict],
                center: str | None, radius_km: float) -> list[dict]:
        """半径内、按距离由近到远排序（与 dialogue._within 同口径）。"""
        out = []
        for p in items:
            geo = p.get("geo")
            if not geo or not (geo[0] or geo[1]):
                continue
            nm = p.get("name") or ""
            if center and (nm == center or nm in center or center in nm):
                continue          # 圆心自己不算"周边"
            km = _haversine([lat, lon], geo)
            if km <= radius_km:
                out.append({**p, "_km": round(km, 1)})
        out.sort(key=lambda p: p["_km"])
        return out
