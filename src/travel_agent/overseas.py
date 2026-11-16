"""城市中文名 → 地图服务英文检索名。

为什么需要这张表：Open-Meteo 的地理编码按中文名查会命中国内同名小地名
（2026-11-16 实测「东京」返回江苏一个 PPL 村庄，坐标落在镇江一带），
境外城市因此被排成国内的行程；「大阪」这类则直接查不到。维基导游的英文站
页名也是英文，中文名同样取不到页面。

所以：**中文名 → 英文名**在这一层解决，坐标与页面都交给上游按英文名查，
不在代码里硬编码坐标（坐标错了更难发现）。

只收真正热门的境外目的地，宁缺毋滥：查不到就诚实说"定位不到"，
比排到隔壁省强。国内城市走 CITY_GEO/EXTRA_CITY_GEO，不进这张表
（中国港澳台地区用英文检索名同样更稳，一并列出）。
"""

from __future__ import annotations

EN_NAMES: dict[str, str] = {
    # 东亚
    "东京": "Tokyo", "大阪": "Osaka", "京都": "Kyoto", "奈良": "Nara",
    "札幌": "Sapporo", "福冈": "Fukuoka", "名古屋": "Nagoya", "冲绳": "Okinawa",
    "那霸": "Naha", "北海道": "Hokkaido", "首尔": "Seoul", "釜山": "Busan",
    "济州": "Jeju", "济州岛": "Jeju",
    # 中国港澳台地区（英文检索名更稳）
    "台北": "Taipei", "高雄": "Kaohsiung",
    "台中": "Taichung", "香港": "Hong Kong", "澳门": "Macao",
    # 东南亚
    "新加坡": "Singapore", "曼谷": "Bangkok", "清迈": "Chiang Mai",
    "普吉": "Phuket", "普吉岛": "Phuket", "芭提雅": "Pattaya",
    "吉隆坡": "Kuala Lumpur", "槟城": "George Town", "巴厘岛": "Denpasar",
    "雅加达": "Jakarta", "河内": "Hanoi", "胡志明市": "Ho Chi Minh City",
    "岘港": "Da Nang", "金边": "Phnom Penh", "暹粒": "Siem Reap",
    "仰光": "Yangon", "马尼拉": "Manila", "长滩岛": "Boracay",
    # 南亚 / 中东
    "新德里": "New Delhi", "德里": "Delhi", "孟买": "Mumbai",
    "加德满都": "Kathmandu", "科伦坡": "Colombo", "马尔代夫": "Male",
    "迪拜": "Dubai", "阿布扎比": "Abu Dhabi", "多哈": "Doha",
    "伊斯坦布尔": "Istanbul", "特拉维夫": "Tel Aviv",
    # 欧洲
    "伦敦": "London", "巴黎": "Paris", "罗马": "Rome", "米兰": "Milan",
    "威尼斯": "Venice", "佛罗伦萨": "Florence", "那不勒斯": "Naples",
    "巴塞罗那": "Barcelona", "马德里": "Madrid", "里斯本": "Lisbon",
    "柏林": "Berlin", "慕尼黑": "Munich", "法兰克福": "Frankfurt",
    "阿姆斯特丹": "Amsterdam", "布鲁塞尔": "Brussels", "苏黎世": "Zurich",
    "日内瓦": "Geneva", "维也纳": "Vienna", "布拉格": "Prague",
    "布达佩斯": "Budapest", "华沙": "Warsaw", "雅典": "Athens",
    "哥本哈根": "Copenhagen", "斯德哥尔摩": "Stockholm", "奥斯陆": "Oslo",
    "赫尔辛基": "Helsinki", "都柏林": "Dublin", "爱丁堡": "Edinburgh",
    "莫斯科": "Moscow", "圣彼得堡": "Saint Petersburg", "冰岛": "Reykjavik",
    # 美洲
    "纽约": "New York", "洛杉矶": "Los Angeles", "旧金山": "San Francisco",
    "西雅图": "Seattle", "芝加哥": "Chicago", "波士顿": "Boston",
    "华盛顿": "Washington", "拉斯维加斯": "Las Vegas", "迈阿密": "Miami",
    "夏威夷": "Honolulu", "檀香山": "Honolulu", "温哥华": "Vancouver",
    "多伦多": "Toronto", "蒙特利尔": "Montreal", "墨西哥城": "Mexico City",
    # 大洋洲 / 非洲
    "悉尼": "Sydney", "墨尔本": "Melbourne", "布里斯班": "Brisbane",
    "奥克兰": "Auckland", "皇后镇": "Queenstown", "开罗": "Cairo",
    "开普敦": "Cape Town", "内罗毕": "Nairobi",
}


def en_name(city: str) -> str:
    """取英文名；没有收录就原样返回（交给上游按原名试）。"""
    return EN_NAMES.get((city or "").strip(), (city or "").strip())


def is_overseas(city: str) -> bool:
    return (city or "").strip() in EN_NAMES
