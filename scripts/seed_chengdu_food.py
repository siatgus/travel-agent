#!/usr/bin/env python3
"""成都精选茶馆/餐厅 POI 入库（source=manual，幂等可重跑）。

背景：自动抓取（Overpass/模型）对"茶馆、老字号小吃"这类点不稳定，
而「喝茶」「不吃辣」这两类偏好必须有真实 POI 才能落地成具体活动。
这里手工收录 5 个地标级点位（坐标为门店级，误差 <200m），
tags 带 午餐/晚餐/清汤/不辣/茶馆，供 composer 选餐与兴趣命中使用。
营业状态会随时间变化，note 里注明以点评/官方信息为准；
autofetch 的在线补充通道保持不变，两者互补。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travel_agent.kb import get_kb  # noqa: E402

POIS = [
    {
        "poi_id": "KB-CD-FOOD-01",
        "name": "鹤鸣茶社",
        "category": "restaurant",
        "area": "人民公园",
        "geo": [30.6596, 104.0565],
        "appeal": 5,
        "duration_min": 90,
        "cost": 30,
        "open_hours": "08:00-22:00",
        "need_booking": False,
        "tags": ["茶馆", "喝茶", "盖碗茶", "午餐", "晚餐", "老字号", "不辣"],
        "note": "人民公园内百年露天茶社，竹椅盖碗茶；不吃辣无压力，茶点小吃为主。营业状态以点评/官方信息为准",
    },
    {
        "poi_id": "KB-CD-FOOD-02",
        "name": "文殊院香园",
        "category": "restaurant",
        "area": "文殊院",
        "geo": [30.6790, 104.0741],
        "appeal": 4,
        "duration_min": 75,
        "cost": 45,
        "open_hours": "10:00-19:00",
        "need_booking": False,
        "tags": ["茶馆", "喝茶", "素食", "午餐", "晚餐", "清汤", "清淡", "不辣"],
        "note": "文殊院旁素斋茶座，全素清汤；参观文殊院顺路。营业状态以点评/官方信息为准",
    },
    {
        "poi_id": "KB-CD-FOOD-03",
        "name": "小谭豆花",
        "category": "restaurant",
        "area": "西大街",
        "geo": [30.6702, 104.0580],
        "appeal": 4,
        "duration_min": 45,
        "cost": 30,
        "open_hours": "08:00-20:00",
        "need_booking": False,
        "tags": ["午餐", "晚餐", "小吃", "豆花", "清汤", "不辣", "老字号"],
        "note": "川西名小吃，馓子豆花有清汤做法，冰醉豆花是甜口。营业状态以点评/官方信息为准",
    },
    {
        "poi_id": "KB-CD-FOOD-04",
        "name": "龙抄手",
        "category": "restaurant",
        "area": "春熙路",
        "geo": [30.6567, 104.0810],
        "appeal": 4,
        "duration_min": 45,
        "cost": 35,
        "open_hours": "08:00-21:00",
        "need_booking": False,
        "tags": ["午餐", "晚餐", "小吃", "清汤", "不辣", "老字号"],
        "note": "老字号抄手，原汤/清汤为主（红油款可避开）。营业状态以点评/官方信息为准",
    },
    {
        "poi_id": "KB-CD-FOOD-05",
        "name": "赖汤圆",
        "category": "restaurant",
        "area": "总府路",
        "geo": [30.6585, 104.0805],
        "appeal": 3,
        "duration_min": 30,
        "cost": 20,
        "open_hours": "08:30-21:00",
        "need_booking": False,
        "tags": ["午餐", "小吃", "甜", "不辣", "老字号"],
        "note": "甜口汤圆，完全无辣，适合加餐或下午茶。营业状态以点评/官方信息为准",
    },
]


def main() -> None:
    db = get_kb()
    for p in POIS:
        pid = db.upsert_poi("成都", p, source="manual")
        print(f"upserted {pid} {p['name']} ({p['category']})")


if __name__ == "__main__":
    main()
