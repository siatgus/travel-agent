"""POIHandler 与 poi 细分 Handler（设计文档 §8 第二阶段）。

细分意图（attraction_search / food_query / restaurant_search /
hotel_search / route_query）各自独立 Handler，但 **refine 兜底链的
顺序保留**：refine_intent 只决定「先试哪一路」，某一路没接住
（返回 None，比如联网失败）就按原链路继续往后试，兜底仍是泛推荐
——拆分前后行为一致（原 Session._poi_answer 注释原话）。

各 Handler 复用 Session 的同名业务方法，不重写业务。
"""

from __future__ import annotations

from ... import intent
from ._base import resolve_city


class _SubHandler:
    """poi 细分基类：子类声明 session 方法名，handle 委托调用。"""

    _method = ""

    def handle(self, session, city: str, utterance: str) -> str | None:
        return getattr(session, self._method)(city, utterance)


class ComparisonHandler(_SubHandler):
    """类比问句（「有没有像迪士尼一样的地方」）→ 联网检索。"""

    _method = "_comparison_answer"


class RouteHandler(_SubHandler):
    """城市内地点间路线（「宽窄巷子到春熙路怎么走」）。"""

    _method = "_route_answer"


class AttractionHandler(_SubHandler):
    """景点咨询三路：点名景点（含周边泛问）、品类问句、关键词推荐。"""

    _method = "_attraction_answer"


class FoodHandler(_SubHandler):
    """food_query：地方特色菜、美食文化（吃什么，不是去哪家店）。"""

    _method = "_food_answer"


class RestaurantHandler(_SubHandler):
    """restaurant_search：真实餐厅/店铺 POI（去哪家店吃）。"""

    _method = "_restaurant_answer"


class HotelHandler(_SubHandler):
    """hotel_search：酒店查询（参考起价口径，不冒充实时房态）。"""

    _method = "_hotel_answer"


class POIHandler:
    """poi 粗分类入口：按 refine_intent 排序细分链，逐路兜底。"""

    # (细分意图, Handler)：顺序 = 原链路顺序，绝不能重排
    CHAIN = (
        ("general", ComparisonHandler()),
        ("route_query", RouteHandler()),
        ("attraction_search", AttractionHandler()),
        ("food_query", FoodHandler()),
        ("restaurant_search", RestaurantHandler()),
        ("hotel_search", HotelHandler()),
    )

    def handle(self, session, utterance: str) -> str:
        city = resolve_city(session, utterance)
        if not city:
            return intent.poi_answer("")
        session.last_city = city
        fine = intent.refine_intent(utterance, city)
        names = [n for n, _ in self.CHAIN]
        start = names.index(fine) if fine in names else 0
        for _, handler in self.CHAIN[start:]:
            said = handler.handle(session, city, utterance)
            if said:
                return said
        return intent.poi_answer(city, client=session.client)
