"""Repository：本地知识库访问与来源元数据。

职责边界（文档 §10）：只读本地 SQLite 与静态种子，不承担业务回答，
不调外部 API。查询主体仍走 real_tools 旧工具（KB 优先 → 静态回落的
契约在那边实现且被全部测试固化），这里补两件旧工具没给的东西：

1. city_meta：城市数据的来源与最后更新时间（list_pois/list_hotels
   不暴露 source/updated_at，行级契约不能动，所以直接查表聚合）。
2. 定向池查询：餐厅/酒店按类别取全量池（周边圈层筛选用），
   美食知识表读取。

餐厅复用 POI 基础结构（category="restaurant"），不建重复餐厅表（ADR-006）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from ... import kb
from ...tools import HOTELS_DB, POIS_DB, ROUTES_DB

NOW = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class _BaseRepository:
    """公共：kb 单例获取（可注入，测试用）与来源元数据聚合。"""

    def __init__(self, db=None):
        self._injected = db

    @property
    def db(self):
        if self._injected is not None:
            return self._injected
        return kb.get_kb()          # 全局单例；None = 知识库不可用

    def city_meta(self, city: str, table: str) -> dict | None:
        """城市级来源元数据：{source, origin_source, updated_at}。

        库不可用或该城市无行 → None（上层据此判定数据来自静态种子回落）。
        """
        db = self.db
        if db is None or table not in ("pois", "hotels", "schedules"):
            return None
        try:
            row = db.conn.execute(
                f"SELECT GROUP_CONCAT(DISTINCT source) srcs,"
                f" MAX(updated_at) latest FROM {table}"
                f" WHERE {'city=?' if table != 'schedules' else 'from_city=?'}",
                (city,)).fetchone()
        except Exception:
            return None
        if not row or not (row["srcs"] or row["latest"]):
            return None
        return {"source": row["srcs"] or "", "updated_at": row["latest"]}


class POIRepository(_BaseRepository):
    """景点 / 餐厅（同表不同 category，字段语义见 autofetch._valid_row）。"""

    def pool(self, city: str, category: str = "attraction") -> list[dict] | None:
        """按类别取城市全量池；无数据返回 None（调用方回落静态种子）。"""
        db = self.db
        if db is None:
            return None
        try:
            rows = db.list_pois(city)
        except Exception:
            return None
        if rows is None:
            return None
        return [p for p in rows if (p.get("category") or "") == category]

    def static_pool(self, city: str) -> list[dict]:
        """静态种子池（与 real_tools.search_poi 的回落同源）。"""
        return POIS_DB.get(city, [])

    def meta(self, city: str) -> dict | None:
        return self.city_meta(city, "pois")


class HotelRepository(_BaseRepository):

    def static_pool(self, city: str) -> list[dict]:
        return HOTELS_DB.get(city, [])

    def meta(self, city: str) -> dict | None:
        return self.city_meta(city, "hotels")


class TransportRepository(_BaseRepository):
    """班次池与来源（12306/航司静态口径，非实时）。"""

    def static_pool(self, from_city: str, to_city: str) -> list[dict]:
        return ROUTES_DB.get((from_city, to_city)) or []

    def meta(self, from_city: str, to_city: str) -> dict | None:
        db = self.db
        if db is None:
            return None
        try:
            row = db.conn.execute(
                "SELECT GROUP_CONCAT(DISTINCT source) srcs, MAX(updated_at) latest"
                " FROM schedules WHERE from_city=? AND to_city=?",
                (from_city, to_city)).fetchone()
        except Exception:
            return None
        if not row or not (row["srcs"] or row["latest"]):
            return None
        return {"source": row["srcs"] or "", "updated_at": row["latest"]}


class FoodRepository(_BaseRepository):
    """美食知识（菜品/文化）——与真实餐厅 POI 严格分表。"""

    def list(self, city: str) -> list[dict] | None:
        db = self.db
        if db is None:
            return None
        try:
            return db.list_food(city)
        except Exception:
            return None
