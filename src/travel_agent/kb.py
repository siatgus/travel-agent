"""SQLite 知识库：POI / 酒店 / 班次 / 城市 / 同步日志。

零依赖（stdlib sqlite3）。设计取舍：
- 首次打开自动从 real_tools 的核实数据播种（source='seed'），幂等——已有数据不覆盖。
- 工具层（real_tools）优先读本库：管理后端新增/导入的城市立即可被规划，
  库不可用时（只读 .pyz、文件损坏）静默回落静态库，规划能力永不降级。
- 实时同步做成可插拔：默认 Overpass 抓取器（主站+镜像轮询），本机网络不通时
  落 sync_log 失败记录；管理后端另提供 JSON 手动导入通道（source='manual'）。
- 每条数据带 source 与 updated_at，供看板展示新鲜度。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import paths

_LOCK = threading.Lock()
_DB: "KnowledgeBase | None" = None
_DB_PATH: Path | None = None

NOW = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_TIMEOUT = 45


def configure(path: Path) -> None:
    """在工具层导入前设置库文件位置（只读环境自动降级到 cwd）。"""
    global _DB_PATH, _DB
    _DB_PATH = path
    _DB = None


def _resolve_path(default: Path) -> Path:
    try:
        default.parent.mkdir(parents=True, exist_ok=True)
        probe = default.parent / ".kb_probe"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return default
    except OSError:                                  # 只读目录（.pyz）→ 工作目录
        alt = Path.cwd() / default.name
        alt.parent.mkdir(parents=True, exist_ok=True)
        return alt


def get_kb() -> "KnowledgeBase | None":
    """全局单例；任何异常返回 None（调用方回落静态数据）。"""
    global _DB
    if _DB is not None:
        return _DB
    try:
        path = _resolve_path(_DB_PATH or paths.kb_db())
        _DB = KnowledgeBase(path)
        _DB.seed_if_empty()
        return _DB
    except Exception:
        _DB = None
        return None


class KnowledgeBase:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS cities(
          name TEXT PRIMARY KEY, geo_lat REAL, geo_lon REAL,
          bbox TEXT DEFAULT '', created_at TEXT);
        CREATE TABLE IF NOT EXISTS pois(
          poi_id TEXT PRIMARY KEY, city TEXT NOT NULL, name TEXT NOT NULL,
          category TEXT DEFAULT 'attraction', area TEXT DEFAULT '',
          geo_lat REAL, geo_lon REAL, appeal INTEGER DEFAULT 3,
          duration_min INTEGER DEFAULT 90, cost INTEGER DEFAULT 0,
          open_hours TEXT DEFAULT '全天', open_days TEXT DEFAULT '每天',
          need_booking INTEGER DEFAULT 0, tags TEXT DEFAULT '[]',
          intensity TEXT DEFAULT 'low', indoor_alternative TEXT DEFAULT '',
          note TEXT DEFAULT '', source TEXT DEFAULT 'seed', updated_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_pois_city ON pois(city);
        CREATE TABLE IF NOT EXISTS hotels(
          hotel_id TEXT PRIMARY KEY, city TEXT NOT NULL, name TEXT NOT NULL,
          area TEXT DEFAULT '', star INTEGER DEFAULT 3, price REAL DEFAULT 0,
          geo_lat REAL, geo_lon REAL, cancel_free INTEGER DEFAULT 0,
          dist_to_core_km REAL DEFAULT 1.0, rating REAL DEFAULT 4.5,
          note TEXT DEFAULT '', source TEXT DEFAULT 'seed', updated_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_hotels_city ON hotels(city);
        CREATE TABLE IF NOT EXISTS schedules(
          code TEXT NOT NULL, from_city TEXT NOT NULL, to_city TEXT NOT NULL,
          mode TEXT DEFAULT 'train', depart TEXT DEFAULT '', arrive TEXT DEFAULT '',
          duration_min INTEGER DEFAULT 0, price REAL DEFAULT 0, note TEXT DEFAULT '',
          source TEXT DEFAULT 'seed', updated_at TEXT,
          PRIMARY KEY(code, from_city, to_city));
        CREATE TABLE IF NOT EXISTS sync_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, city TEXT,
          source TEXT, status TEXT, added INTEGER DEFAULT 0,
          updated INTEGER DEFAULT 0, message TEXT DEFAULT '');
        """)
        self.conn.commit()

    # ------------------------------------------------------------ 播种
    def seed_if_empty(self) -> dict[str, int]:
        """从 real_tools 核实数据播种；只补空缺，不覆盖已有记录（幂等）。"""
        from .real_tools import POIS_DB, HOTELS_DB, ROUTES_DB
        counts = {"pois": 0, "hotels": 0, "schedules": 0}
        with _LOCK:
            for city, items in POIS_DB.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO cities(name,created_at) VALUES(?,?)",
                    (city, NOW()))
                for p in items:
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO pois(poi_id,city,name,updated_at,source)"
                        " VALUES(?,?,?,?,'seed')",
                        (p["poi_id"], city, p["name"], NOW()))
                    if cur.rowcount:
                        counts["pois"] += 1
                        self._fill_poi(city, p)
            for city, items in HOTELS_DB.items():
                for h in items:
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO hotels(hotel_id,city,name,updated_at,source)"
                        " VALUES(?,?,?,?,'seed')",
                        (h["hotel_id"], city, h["name"], NOW()))
                    if cur.rowcount:
                        counts["hotels"] += 1
                        self._fill_hotel(city, h)
            for (a, b), items in ROUTES_DB.items():
                for s in items:
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO schedules(code,from_city,to_city,"
                        "updated_at,source) VALUES(?,?,?,?, 'seed')",
                        (s["code"], a, b, NOW()))
                    if cur.rowcount:
                        counts["schedules"] += 1
                        self._fill_schedule(a, b, s)
            self.conn.commit()
        return counts

    def _fill_poi(self, city: str, p: dict) -> None:
        geo = p.get("geo") or [None, None]
        self.conn.execute(
            "UPDATE pois SET city=?,name=?,category=?,area=?,geo_lat=?,geo_lon=?,"
            "appeal=?,duration_min=?,cost=?,open_hours=?,open_days=?,need_booking=?,"
            "tags=?,intensity=?,indoor_alternative=?,note=? WHERE poi_id=?", (
                city, p["name"], p.get("category", "attraction"),
                p.get("area", ""), geo[0], geo[1], p.get("appeal", 3),
                p.get("duration_min", 90), p.get("cost", 0), p.get("open_hours", "全天"),
                p.get("open_days", "每天"), int(p.get("need_booking", False)),
                json.dumps(p.get("tags", []), ensure_ascii=False),
                p.get("intensity", "low"), p.get("indoor_alternative", ""),
                p.get("note", ""), p["poi_id"]))

    def _fill_hotel(self, city: str, h: dict) -> None:
        geo = h.get("geo") or [None, None]
        self.conn.execute(
            "UPDATE hotels SET city=?,name=?,area=?,star=?,price=?,geo_lat=?,geo_lon=?,"
            "cancel_free=?,dist_to_core_km=?,rating=?,note=? WHERE hotel_id=?", (
                city, h["name"], h.get("area", ""),
                h.get("star", 3), h.get("price", 0), geo[0], geo[1],
                int(h.get("cancel_free", False)), h.get("dist_to_core_km", 1.0),
                h.get("rating", 4.5), h.get("note", ""), h["hotel_id"]))

    def _fill_schedule(self, a: str, b: str, s: dict) -> None:
        self.conn.execute(
            "UPDATE schedules SET mode=?,depart=?,arrive=?,duration_min=?,price=?,note=?"
            " WHERE code=? AND from_city=? AND to_city=?", (
                s.get("mode", "train"), s.get("depart", ""), s.get("arrive", ""),
                s.get("duration_min", 0), s.get("price", 0), s.get("note", ""),
                s["code"], a, b))

    # ------------------------------------------------------------ POI
    def list_pois(self, city: str) -> list[dict] | None:
        """返回与 POIS_DB 同构的 dict 列表；该城市无数据返回 None。"""
        rows = self.conn.execute("SELECT * FROM pois WHERE city=?", (city,)).fetchall()
        if not rows:
            return None
        out = []
        for r in rows:
            geo = [r["geo_lat"], r["geo_lon"]] if r["geo_lat"] is not None else [0, 0]
            out.append({
                "poi_id": r["poi_id"], "name": r["name"], "category": r["category"],
                "area": r["area"], "geo": geo, "appeal": r["appeal"],
                "duration_min": r["duration_min"], "cost": r["cost"],
                "open_hours": r["open_hours"], "open_days": r["open_days"],
                "need_booking": bool(r["need_booking"]),
                "tags": json.loads(r["tags"] or "[]"), "intensity": r["intensity"],
                "indoor_alternative": r["indoor_alternative"], "note": r["note"],
            })
        return out

    def search_pois(self, city: str = "", q: str = "", category: str = "",
                    source: str = "", limit: int = 500) -> list[dict]:
        """条件检索 POI（管理端用）。返回原始行（含 city/source/tags 字符串）。

        city 为空或 "*" 表示不限城市；q 模糊匹配名称/区域/ID；其余为精确筛选。
        """
        where, args = [], []
        if city and city != "*":
            where.append("city=?")
            args.append(city)
        if q:
            like = f"%{q}%"
            where.append("(name LIKE ? OR area LIKE ? OR poi_id LIKE ?)")
            args.extend([like, like, like])
        if category:
            where.append("category=?")
            args.append(category)
        if source:
            where.append("source=?")
            args.append(source)
        sql = "SELECT * FROM pois"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY city, LENGTH(poi_id), poi_id LIMIT ?"   # 自然序：P2 在 P10 前
        args.append(max(1, min(2000, int(limit))))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def upsert_poi(self, city: str, p: dict, source: str = "manual") -> str:
        """新增/更新一个 POI。缺 poi_id 自动生成。返回 poi_id。"""
        pid = p.get("poi_id") or ("KB-" + uuid.uuid4().hex[:8])
        geo = p.get("geo") or [None, None]
        with _LOCK:
            self.conn.execute(
                "INSERT INTO cities(name,created_at) VALUES(?,?)"
                " ON CONFLICT(name) DO NOTHING", (city, NOW()))
            self.conn.execute(
                "INSERT INTO pois(poi_id,city,name,category,area,geo_lat,geo_lon,"
                "appeal,duration_min,cost,open_hours,open_days,need_booking,tags,"
                "intensity,indoor_alternative,note,source,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(poi_id) DO UPDATE SET city=excluded.city,"
                "name=excluded.name,category=excluded.category,area=excluded.area,"
                "geo_lat=excluded.geo_lat,geo_lon=excluded.geo_lon,"
                "appeal=excluded.appeal,duration_min=excluded.duration_min,"
                "cost=excluded.cost,open_hours=excluded.open_hours,"
                "open_days=excluded.open_days,need_booking=excluded.need_booking,"
                "tags=excluded.tags,intensity=excluded.intensity,"
                "indoor_alternative=excluded.indoor_alternative,note=excluded.note,"
                "source=excluded.source,updated_at=excluded.updated_at", (
                    pid, city, p["name"], p.get("category", "attraction"),
                    p.get("area", ""), geo[0], geo[1], p.get("appeal", 3),
                    p.get("duration_min", 90), p.get("cost", 0),
                    p.get("open_hours", "全天"), p.get("open_days", "每天"),
                    int(p.get("need_booking", False)),
                    json.dumps(p.get("tags", []), ensure_ascii=False),
                    p.get("intensity", "low"), p.get("indoor_alternative", ""),
                    p.get("note", ""), source, NOW()))
            self.conn.commit()
        return pid

    def delete_poi(self, poi_id: str) -> bool:
        with _LOCK:
            cur = self.conn.execute("DELETE FROM pois WHERE poi_id=?", (poi_id,))
            self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ 酒店
    def list_hotels(self, city: str) -> list[dict] | None:
        rows = self.conn.execute("SELECT * FROM hotels WHERE city=?", (city,)).fetchall()
        if not rows:
            return None
        out = []
        for r in rows:
            geo = [r["geo_lat"], r["geo_lon"]] if r["geo_lat"] is not None else [0, 0]
            out.append({
                "hotel_id": r["hotel_id"], "name": r["name"], "area": r["area"],
                "star": r["star"], "price": r["price"], "geo": geo,
                "cancel_free": bool(r["cancel_free"]),
                "dist_to_core_km": r["dist_to_core_km"], "rating": r["rating"],
                "note": r["note"],
            })
        return out

    def upsert_hotel(self, city: str, h: dict, source: str = "manual") -> str:
        hid = h.get("hotel_id") or ("KBH-" + uuid.uuid4().hex[:8])
        geo = h.get("geo") or [None, None]
        with _LOCK:
            self.conn.execute(
                "INSERT INTO cities(name,created_at) VALUES(?,?)"
                " ON CONFLICT(name) DO NOTHING", (city, NOW()))
            self.conn.execute(
                "INSERT INTO hotels(hotel_id,city,name,area,star,price,geo_lat,geo_lon,"
                "cancel_free,dist_to_core_km,rating,note,source,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(hotel_id) DO UPDATE SET city=excluded.city,"
                "name=excluded.name,area=excluded.area,star=excluded.star,"
                "price=excluded.price,geo_lat=excluded.geo_lat,"
                "geo_lon=excluded.geo_lon,cancel_free=excluded.cancel_free,"
                "dist_to_core_km=excluded.dist_to_core_km,rating=excluded.rating,"
                "note=excluded.note,source=excluded.source,updated_at=excluded.updated_at", (
                    hid, city, h["name"], h.get("area", ""), h.get("star", 3),
                    h.get("price", 0), geo[0], geo[1],
                    int(h.get("cancel_free", False)), h.get("dist_to_core_km", 1.0),
                    h.get("rating", 4.5), h.get("note", ""), source, NOW()))
            self.conn.commit()
        return hid

    def delete_hotel(self, hotel_id: str) -> bool:
        with _LOCK:
            cur = self.conn.execute("DELETE FROM hotels WHERE hotel_id=?", (hotel_id,))
            self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ 班次
    def list_schedules(self, from_city: str, to_city: str) -> list[dict] | None:
        rows = self.conn.execute(
            "SELECT * FROM schedules WHERE from_city=? AND to_city=?"
            " ORDER BY depart", (from_city, to_city)).fetchall()
        if not rows:
            return None
        return [{"mode": r["mode"], "code": r["code"], "depart": r["depart"],
                 "arrive": r["arrive"], "duration_min": r["duration_min"],
                 "price": r["price"], "note": r["note"]} for r in rows]

    def upsert_schedule(self, from_city: str, to_city: str, s: dict,
                        source: str = "manual") -> str:
        with _LOCK:
            self.conn.execute(
                "INSERT INTO schedules(code,from_city,to_city,mode,depart,arrive,"
                "duration_min,price,note,source,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(code,from_city,to_city) DO UPDATE SET"
                " mode=excluded.mode,depart=excluded.depart,arrive=excluded.arrive,"
                " duration_min=excluded.duration_min,price=excluded.price,"
                " note=excluded.note,source=excluded.source,updated_at=excluded.updated_at", (
                    s["code"], from_city, to_city, s.get("mode", "train"),
                    s.get("depart", ""), s.get("arrive", ""),
                    s.get("duration_min", 0), s.get("price", 0),
                    s.get("note", ""), source, NOW()))
            self.conn.commit()
        return s["code"]

    # ------------------------------------------------------------ 城市 / 统计 / 日志
    def cities(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT city FROM pois UNION SELECT DISTINCT city FROM hotels"
        ).fetchall()
        return sorted(r[0] for r in rows)

    def stats(self) -> dict:
        one = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]
        return {
            "cities": one("SELECT COUNT(*) FROM (SELECT DISTINCT city FROM pois"
                          " UNION SELECT DISTINCT city FROM hotels)"),
            "pois": one("SELECT COUNT(*) FROM pois"),
            "hotels": one("SELECT COUNT(*) FROM hotels"),
            "schedules": one("SELECT COUNT(*) FROM schedules"),
            "freshness": [
                {"city": r["city"], "pois": r["n"],
                 "last_update": r["last"],
                 "sources": r["srcs"]}
                for r in self.conn.execute(
                    "SELECT city, COUNT(*) n, MAX(updated_at) last,"
                    " GROUP_CONCAT(DISTINCT source) srcs"
                    " FROM pois GROUP BY city ORDER BY city")
            ],
        }

    def logs(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def _log(self, city: str, source: str, status: str,
             added: int, updated: int, message: str) -> None:
        with _LOCK:
            self.conn.execute(
                "INSERT INTO sync_log(ts,city,source,status,added,updated,message)"
                " VALUES(?,?,?,?,?,?,?)", (NOW(), city, source, status,
                                           added, updated, message))
            self.conn.commit()

    def record_log(self, city: str, source: str, status: str,
                   added: int = 0, updated: int = 0, message: str = "") -> None:
        """对外日志入口（自动补全等模块写 sync_log 用）。"""
        self._log(city, source, status, added, updated, message)

    def upsert_city(self, name: str, geo: list[float] | None = None,
                    bbox: tuple | None = None) -> None:
        """登记城市中心坐标与包围盒（自动补全/后台录入时用）。"""
        lat, lon = (float(geo[0]), float(geo[1])) if geo else (None, None)
        box = ",".join(str(round(float(x), 4)) for x in bbox) if bbox else ""
        with _LOCK:
            self.conn.execute(
                "INSERT INTO cities(name,geo_lat,geo_lon,bbox,created_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
                " geo_lat=COALESCE(excluded.geo_lat,cities.geo_lat),"
                " geo_lon=COALESCE(excluded.geo_lon,cities.geo_lon),"
                " bbox=CASE WHEN excluded.bbox<>'' THEN excluded.bbox ELSE cities.bbox END",
                (name, lat, lon, box, NOW()))
            self.conn.commit()

    # ------------------------------------------------------------ 同步 / 导入
    def import_json(self, city: str, kind: str, items: list[dict]) -> dict:
        """管理后端手动导入通道。返回 {added, updated, errors}。"""
        added = updated = 0
        errors: list[str] = []
        for i, item in enumerate(items):
            try:
                if kind == "poi":
                    if not item.get("name") or not item.get("geo"):
                        raise ValueError("POI 需要 name 与 geo [lat,lon]")
                    pid = item.get("poi_id")
                    exists = self.conn.execute(
                        "SELECT 1 FROM pois WHERE poi_id=?", (pid,)).fetchone() \
                        if pid else None
                    self.upsert_poi(city, item, source="manual")
                elif kind == "hotel":
                    if not item.get("name") or not item.get("price"):
                        raise ValueError("酒店需要 name 与 price")
                    hid = item.get("hotel_id")
                    exists = self.conn.execute(
                        "SELECT 1 FROM hotels WHERE hotel_id=?", (hid,)).fetchone() \
                        if hid else None
                    self.upsert_hotel(city, item, source="manual")
                else:
                    raise ValueError(f"不支持的类型: {kind}")
                if exists:
                    updated += 1
                else:
                    added += 1
            except Exception as e:
                errors.append(f"第{i + 1}条: {e}")
        if added or updated:
            self._log(city, "manual-import", "success", added, updated,
                      f"kind={kind}，{len(errors)} 条被拒")
        return {"added": added, "updated": updated, "errors": errors}

    def sync_city(self, city: str, bbox: tuple | None = None,
                  fetcher=None) -> dict:
        """实时同步：默认 Overpass 抓取器（主站+镜像轮询）。

        返回 {status: success|failed|empty, added, updated, message}，全程落日志。
        """
        source = "overpass"
        try:
            fetcher = fetcher or _fetch_overpass
            items = fetcher(city, bbox)
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:160]}"
            self._log(city, source, "failed", 0, 0, msg)
            return {"status": "failed", "added": 0, "updated": 0, "message": msg}
        if not items:
            self._log(city, source, "empty", 0, 0, "上游无返回")
            return {"status": "empty", "added": 0, "updated": 0, "message": "上游无返回"}
        added = updated = 0
        for it in items:
            pid = self.upsert_poi(city, it, source=source)
            if pid:
                added += 1
        self._log(city, source, "success", added, updated,
                  f"同步 {len(items)} 条 attraction")
        return {"status": "success", "added": added, "updated": updated,
                "message": f"同步 {len(items)} 条"}


def _fetch_overpass(city: str, bbox: tuple | None) -> list[dict]:
    """Overpass 抓取器：city bbox 内的 tourism=attraction，转成 POI dict。"""
    lat, lon = _city_center(city)
    if bbox:
        s, w, n, e = bbox
    else:
        s, w, n, e = lat - 0.25, lon - 0.30, lat + 0.25, lon + 0.30
    q = (f'[out:json][timeout:{OVERPASS_TIMEOUT}];'
         f'node({s},{w},{n},{e})["tourism"="attraction"]["name"];out center 200;')
    data = urllib.parse.urlencode({"data": q}).encode()
    last_err: Exception | None = None
    for host in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(host, data=data,
                                         headers={"User-Agent": "travel-agent-demo/1.0"})
            with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT + 15) as r:
                d = json.loads(r.read())
            out = []
            for el in d.get("elements", []):
                tags = el.get("tags", {})
                name = tags.get("name")
                if not name:
                    continue
                c = el.get("center") or {}
                plat = el.get("lat", c.get("lat"))
                plon = el.get("lon", c.get("lon"))
                if plat is None:
                    continue
                fee = tags.get("fee", "no")
                out.append({
                    "name": name, "category": "attraction",
                    "area": tags.get("addr:district", city),
                    "geo": [round(plat, 4), round(plon, 4)],
                    "appeal": 3,
                    "duration_min": 90, "cost": 0 if fee == "no" else 60,
                    "open_hours": tags.get("opening_hours", "全天") or "全天",
                    "need_booking": False,
                    "tags": [v for k, v in tags.items()
                             if k in ("tourism", "historic", "leisure")][:3],
                    "note": f"OSM tags: {tags.get('tourism', '')}",
                })
            return out
        except Exception as e:
            last_err = e
    raise ConnectionError(f"Overpass 全部镜像不可达，最后错误: {last_err}")


def _city_center(city: str) -> tuple[float, float]:
    try:
        from .tools import CITY_GEO
        g = CITY_GEO.get(city)
        if g:
            return g[0], g[1]
    except Exception:
        pass
    try:                                   # 未收录城市：走地理编码（后台手动同步也能用）
        from . import real_tools
        g = real_tools.resolve_geo(city)
        if g:
            return float(g[0]), float(g[1])
    except Exception:
        pass
    raise ValueError(f"{city} 无中心坐标，请在管理后端补充城市 bbox")


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    tmp = Path.cwd() / ".kb_selftest.db"
    if tmp.exists():
        tmp.unlink()
    kb = KnowledgeBase(tmp)
    print("seed:", kb.seed_if_empty())
    print("cities:", kb.cities())
    print("大理 pois:", len(kb.list_pois("大理") or []))
    print("stats:", json.dumps(kb.stats(), ensure_ascii=False)[:200])
    r = kb.import_json("玉龙雪山测试", "poi", [
        {"name": "测试景点", "geo": [27.0, 100.1]},
        {"name": "缺geo"},
    ])
    print("import:", r)
    print("sync(off):", kb.sync_city("大理"))
    print("logs:", len(kb.logs()))
    tmp.unlink()
