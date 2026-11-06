"""重新生成 src/travel_agent/city_geo.py（全国地级政区中心坐标）。

做法：中文名 + 拼音 → Open-Meteo geocoding（只认拉丁输入）→ 三道校验：
  1. 条目必须是行政中心（PPLA*）或省级匹配的城镇（PPL），机场/山脉/同名村排除
  2. 返回的中文名或拼音必须与城市名一致
  3. 距省会距离合理（普通省 500~700km，跨度大的省区放宽到 1500km）
过不了就留空（宁缺勿错），并打印出来人工处理。

用法（在项目根）：python scripts/build_city_geo.py
改城市表后重跑即可；已有的人工核实坐标不会被覆盖。
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from travel_agent.city_geo import EXTRA_CITY_GEO  # noqa: E402
from travel_agent.tools import CITY_GEO  # noqa: E402

# 人工核实过的老表（city_geo 之外的），重生时跳过、不覆盖
EXISTING = set(CITY_GEO) - set(EXTRA_CITY_GEO)

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SUFFIX = ("特别行政区", "自治州", "自治县", "地区", "林区", "盟", "市", "县", "区")

GROUPS = {
    "河北省": [("唐山", "Tangshan"), ("邢台", "Xingtai"), ("保定", "Baoding"),
              ("张家口", "Zhangjiakou"), ("沧州", "Cangzhou"), ("廊坊", "Langfang"),
              ("衡水", "Hengshui")],
    "山西省": [("大同", "Datong"), ("阳泉", "Yangquan"), ("长治", "Changzhi"),
              ("晋城", "Jincheng"), ("朔州", "Shuozhou"), ("晋中", "Jinzhong"),
              ("运城", "Yuncheng"), ("忻州", "Xinzhou"), ("临汾", "Linfen"),
              ("吕梁", "Lvliang")],
    "内蒙古自治区": [("包头", "Baotou"), ("乌海", "Wuhai"), ("赤峰", "Chifeng"),
                 ("通辽", "Tongliao"), ("鄂尔多斯", "Ordos"), ("巴彦淖尔", "Bayannur"),
                 ("乌兰察布", "Ulanqab"), ("兴安盟", "Hinggan"),
                 ("锡林郭勒盟", "Xilingol"), ("阿拉善盟", "Bayanhot")],
    "辽宁省": [("鞍山", "Anshan"), ("抚顺", "Fushun"), ("本溪", "Benxi"),
              ("丹东", "Dandong"), ("锦州", "Jinzhou"), ("营口", "Yingkou"),
              ("阜新", "Fuxin"), ("辽阳", "Liaoyang"), ("盘锦", "Panjin"),
              ("铁岭", "Tieling"), ("朝阳", "Chaoyang"), ("葫芦岛", "Huludao")],
    "吉林省": [("吉林", "Jilin"), ("四平", "Siping"), ("辽源", "Liaoyuan"),
              ("通化", "Tonghua"), ("白山", "Baishan"), ("松原", "Songyuan"),
              ("白城", "Baicheng"), ("延边", "Yanbian")],
    "黑龙江省": [("齐齐哈尔", "Qiqihar"), ("鸡西", "Jixi"), ("鹤岗", "Hegang"),
               ("双鸭山", "Shuangyashan"), ("大庆", "Daqing"), ("伊春", "Yichun"),
               ("佳木斯", "Jiamusi"), ("七台河", "Qitaihe"), ("牡丹江", "Mudanjiang"),
               ("黑河", "Heihe"), ("绥化", "Suihua"), ("大兴安岭", "Da Hinggan Ling")],
    "江苏省": [("徐州", "Xuzhou"), ("常州", "Changzhou"), ("南通", "Nantong"),
              ("连云港", "Lianyungang"), ("淮安", "Huai'an"), ("盐城", "Yancheng"),
              ("镇江", "Zhenjiang"), ("泰州", "Taizhou"), ("宿迁", "Suqian")],
    "浙江省": [("嘉兴", "Jiaxing"), ("湖州", "Huzhou"), ("金华", "Jinhua"),
              ("衢州", "Quzhou"), ("台州", "Taizhou"), ("丽水", "Lishui")],
    "安徽省": [("芜湖", "Wuhu"), ("蚌埠", "Bengbu"), ("淮南", "Huainan"),
              ("马鞍山", "Maanshan"), ("淮北", "Huaibei"), ("铜陵", "Tongling"),
              ("安庆", "Anqing"), ("滁州", "Chuzhou"), ("阜阳", "Fuyang"),
              ("宿州", "Suzhou"), ("六安", "Lu'an"), ("亳州", "Bozhou"),
              ("池州", "Chizhou"), ("宣城", "Xuancheng")],
    "福建省": [("莆田", "Putian"), ("三明", "Sanming"), ("漳州", "Zhangzhou"),
              ("南平", "Nanping"), ("龙岩", "Longyan"), ("宁德", "Ningde")],
    "江西省": [("景德镇", "Jingdezhen"), ("萍乡", "Pingxiang"), ("九江", "Jiujiang"),
              ("新余", "Xinyu"), ("鹰潭", "Yingtan"), ("赣州", "Ganzhou"),
              ("吉安", "Jian"), ("宜春", "Yichun"), ("抚州", "Fuzhou"),
              ("上饶", "Shangrao")],
    "山东省": [("淄博", "Zibo"), ("枣庄", "Zaozhuang"), ("东营", "Dongying"),
              ("潍坊", "Weifang"), ("济宁", "Jining"), ("泰安", "Taian"),
              ("日照", "Rizhao"), ("临沂", "Linyi"), ("德州", "Dezhou"),
              ("聊城", "Liaocheng"), ("滨州", "Binzhou"), ("菏泽", "Heze")],
    "河南省": [("平顶山", "Pingdingshan"), ("安阳", "Anyang"), ("鹤壁", "Hebi"),
              ("新乡", "Xinxiang"), ("焦作", "Jiaozuo"), ("濮阳", "Puyang"),
              ("许昌", "Xuchang"), ("漯河", "Luohe"), ("三门峡", "Sanmenxia"),
              ("南阳", "Nanyang"), ("商丘", "Shangqiu"), ("信阳", "Xinyang"),
              ("周口", "Zhoukou"), ("驻马店", "Zhumadian"), ("济源", "Jiyuan")],
    "湖北省": [("黄石", "Huangshi"), ("十堰", "Shiyan"), ("宜昌", "Yichang"),
              ("襄阳", "Xiangyang"), ("鄂州", "Ezhou"), ("荆门", "Jingmen"),
              ("孝感", "Xiaogan"), ("荆州", "Jingzhou"), ("黄冈", "Huanggang"),
              ("咸宁", "Xianning"), ("随州", "Suizhou"), ("恩施", "Enshi")],
    "湖南省": [("株洲", "Zhuzhou"), ("湘潭", "Xiangtan"), ("衡阳", "Hengyang"),
              ("邵阳", "Shaoyang"), ("岳阳", "Yueyang"), ("常德", "Changde"),
              ("益阳", "Yiyang"), ("郴州", "Chenzhou"), ("永州", "Yongzhou"),
              ("怀化", "Huaihua"), ("娄底", "Loudi"), ("湘西", "Xiangxi")],
    "广东省": [("韶关", "Shaoguan"), ("汕头", "Shantou"), ("佛山", "Foshan"),
              ("江门", "Jiangmen"), ("湛江", "Zhanjiang"), ("茂名", "Maoming"),
              ("肇庆", "Zhaoqing"), ("惠州", "Huizhou"), ("梅州", "Meizhou"),
              ("汕尾", "Shanwei"), ("河源", "Heyuan"), ("阳江", "Yangjiang"),
              ("清远", "Qingyuan"), ("东莞", "Dongguan"), ("中山", "Zhongshan"),
              ("潮州", "Chaozhou"), ("揭阳", "Jieyang"), ("云浮", "Yunfu")],
    "广西壮族自治区": [("柳州", "Liuzhou"), ("梧州", "Wuzhou"),
                  ("防城港", "Fangchenggang"), ("钦州", "Qinzhou"),
                  ("贵港", "Guigang"), ("玉林", "Yulin"), ("百色", "Baise"),
                  ("贺州", "Hezhou"), ("河池", "Hechi"), ("来宾", "Laibin"),
                  ("崇左", "Chongzuo")],
    "海南省": [("儋州", "Danzhou"), ("琼海", "Qionghai"), ("文昌", "Wenchang"),
              ("万宁", "Wanning"), ("五指山", "Wuzhishan")],
    "四川省": [("自贡", "Zigong"), ("攀枝花", "Panzhihua"), ("泸州", "Luzhou"),
              ("德阳", "Deyang"), ("绵阳", "Mianyang"), ("广元", "Guangyuan"),
              ("遂宁", "Suining"), ("内江", "Neijiang"), ("乐山", "Leshan"),
              ("南充", "Nanchong"), ("眉山", "Meishan"), ("宜宾", "Yibin"),
              ("广安", "Guangan"), ("达州", "Dazhou"), ("雅安", "Yaan"),
              ("巴中", "Bazhong"), ("资阳", "Ziyang"), ("阿坝", "Aba"),
              ("甘孜", "Garze"), ("凉山", "Liangshan")],
    "贵州省": [("六盘水", "Liupanshui"), ("遵义", "Zunyi"), ("安顺", "Anshun"),
              ("毕节", "Bijie"), ("铜仁", "Tongren"), ("黔西南", "Qianxinan"),
              ("黔东南", "Qiandongnan"), ("黔南", "Qiannan")],
    "云南省": [("曲靖", "Qujing"), ("玉溪", "Yuxi"), ("保山", "Baoshan"),
              ("昭通", "Zhaotong"), ("普洱", "Puer"), ("临沧", "Lincang"),
              ("楚雄", "Chuxiong"), ("红河", "Honghe"), ("文山", "Wenshan"),
              ("德宏", "Dehong"), ("怒江", "Nujiang"), ("迪庆", "Diqing")],
    "西藏自治区": [("日喀则", "Shigatse"), ("昌都", "Qamdo"), ("林芝", "Nyingchi"),
                ("山南", "Shannan"), ("那曲", "Nagqu"), ("阿里", "Ngari")],
    "陕西省": [("铜川", "Tongchuan"), ("宝鸡", "Baoji"), ("咸阳", "Xianyang"),
              ("渭南", "Weinan"), ("延安", "Yanan"), ("汉中", "Hanzhong"),
              ("榆林", "Yulin"), ("安康", "Ankang"), ("商洛", "Shangluo")],
    "甘肃省": [("嘉峪关", "Jiayuguan"), ("金昌", "Jinchang"), ("白银", "Baiyin"),
              ("天水", "Tianshui"), ("武威", "Wuwei"), ("张掖", "Zhangye"),
              ("平凉", "Pingliang"), ("酒泉", "Jiuquan"), ("庆阳", "Qingyang"),
              ("定西", "Dingxi"), ("陇南", "Longnan"), ("临夏", "Linxia"),
              ("甘南", "Gannan")],
    "青海省": [("海东", "Haidong"), ("海北", "Haibei"), ("黄南", "Huangnan"),
              ("海南州", "Hainan"), ("果洛", "Guoluo"), ("玉树", "Yushu"),
              ("海西", "Haixi")],
    "宁夏回族自治区": [("石嘴山", "Shizuishan"), ("吴忠", "Wuzhong"),
                  ("固原", "Guyuan"), ("中卫", "Zhongwei")],
    "新疆维吾尔自治区": [("克拉玛依", "Karamay"), ("吐鲁番", "Turpan"),
                   ("哈密", "Hami"), ("昌吉", "Changji"),
                   ("博尔塔拉", "Bortala"), ("巴音郭楞", "Bayingolin"),
                   ("阿克苏", "Aksu"), ("克孜勒苏", "Kizilsu"),
                   ("和田", "Hotan"), ("伊犁", "Ili"), ("塔城", "Tacheng"),
                   ("阿勒泰", "Altay")],
}


def strip_suffix(name: str) -> str:
    for suf in SUFFIX:
        name = name.replace(suf, "")
    return name


def norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


# 省会坐标：用于"地级市不该离省会太远"的粗筛，专治同名村庄/机场带来的错点
CAPITAL = {
    "河北省": [38.042, 114.514], "山西省": [37.870, 112.548],
    "内蒙古自治区": [40.842, 111.749], "辽宁省": [41.805, 123.431],
    "吉林省": [43.817, 125.323], "黑龙江省": [45.803, 126.535],
    "江苏省": [32.060, 118.796], "浙江省": [30.274, 120.155],
    "安徽省": [31.820, 117.227], "福建省": [26.074, 119.296],
    "江西省": [28.682, 115.858], "山东省": [36.651, 117.120],
    "河南省": [34.746, 113.625], "湖北省": [30.592, 114.305],
    "湖南省": [28.228, 112.938], "广东省": [23.129, 113.264],
    "广西壮族自治区": [22.817, 108.366], "海南省": [20.044, 110.199],
    "四川省": [30.572, 104.066], "贵州省": [26.647, 106.630],
    "云南省": [25.038, 102.718], "西藏自治区": [29.652, 91.172],
    "陕西省": [34.341, 108.939], "甘肃省": [36.061, 103.834],
    "青海省": [36.617, 101.778], "宁夏回族自治区": [38.487, 106.231],
    "新疆维吾尔自治区": [43.825, 87.616],
}
# 只有这几个省区内部跨度大（首府到边远地州可上千公里），其余必须贴近省会
WIDE = {"内蒙古自治区", "西藏自治区", "新疆维吾尔自治区", "甘肃省", "黑龙江省"}

EN_PROV = {
    "河北": "Hebei", "山西": "Shanxi", "内蒙古": "Inner Mongolia", "辽宁": "Liaoning",
    "吉林": "Jilin", "黑龙江": "Heilongjiang", "江苏": "Jiangsu", "浙江": "Zhejiang",
    "安徽": "Anhui", "福建": "Fujian", "江西": "Jiangxi", "山东": "Shandong",
    "河南": "Henan", "湖北": "Hubei", "湖南": "Hunan", "广东": "Guangdong",
    "广西": "Guangxi", "海南": "Hainan", "四川": "Sichuan", "贵州": "Guizhou",
    "云南": "Yunnan", "西藏": "Tibet", "陕西": "Shaanxi", "甘肃": "Gansu",
    "青海": "Qinghai", "宁夏": "Ningxia", "新疆": "Xinjiang",
}

# 拼音有歧义/查不到时的补充写法（同一轮里逐个尝试）
ALIAS = {
    "吉安": ["Ji'an"], "普洱": ["Pu'er"], "百色": ["Bose"], "和田": ["Hetian"],
    "伊犁": ["Yili", "Ili"], "甘孜": ["Ganzi", "Garze"], "吕梁": ["Lüliang"],
    "湘西": ["Xiangxi"], "黔西南": ["Xingyi"], "黔南": ["Duyun"],
    "黔东南": ["Kaili"], "阿里": ["Shiquanhe"], "博尔塔拉": ["Bole"],
    "巴音郭楞": ["Korla"], "海南州": ["Gonghe"], "淮安": ["Huai'an"],
    "泰安": ["Tai'an"], "广安": ["Guang'an"], "雅安": ["Ya'an"],
    "六安": ["Lu'an"], "延安": ["Yan'an"], "榆林": ["Yulin"],
    "阿拉善盟": ["Bayanhot"], "兴安盟": ["Ulanhot"], "锡林郭勒盟": ["Xilinhot"],
    "大兴安岭": ["Jiagedaqi"], "巴彦淖尔": ["Linhe"],
    "马鞍山": ["Ma'anshan"], "日喀则": ["Xigaze", "Rikaze"],
}

# 地区/自治州用行署驻地代表（驻地是实体城市，坐标可核）
RESIDENCE = {
    "兴安盟": "Ulanhot", "锡林郭勒盟": "Xilinhot", "阿拉善盟": "Bayanhot",
    "博尔塔拉": "Bole", "阿里": "Shiquanhe", "黔西南": "Xingyi",
    "黔南": "Duyun", "黔东南": "Kaili", "巴音郭楞": "Korla",
    "海南州": "Qabqa", "大兴安岭": "Jiagedaqi", "甘孜": "Ganzi",
    "阿坝": "Barkam", "凉山": "Xichang", "恩施": "Enshi", "湘西": "Jishou",
    "延边": "Yanji", "红河": "Mengzi", "怒江": "Liuku", "迪庆": "Shangri-La",
    "山南": "Zedang", "甘南": "Hezuo", "海北": "Xihai", "黄南": "Tongren",
    "果洛": "Dawu", "海西": "Delingha", "伊犁": "Yining", "德宏": "Mangshi",
}


# 驻地不在本省辖内的特殊情况（加格达奇地理在内蒙古、行政属黑龙江）
ADMIN_EXEMPT = {"大兴安岭": ("内蒙古",), "德宏": ("云南",)}


def province_key(province: str) -> str:
    key = province.replace("省", "").replace("市", "").replace("自治区", "")
    return key.replace("壮族", "").replace("回族", "").replace("维吾尔", "")


def distance_km(a: list, b: list) -> float:
    lat1, lon1, lat2, lon2 = a[0], a[1], b[0], b[1]
    p = 0.017453292519943295
    c = (0.5 - __import__("math").cos((lat2 - lat1) * p) / 2
         + __import__("math").cos(lat1 * p) * __import__("math").cos(lat2 * p)
         * (1 - __import__("math").cos((lon2 - lon1) * p)) / 2)
    return 12742 * __import__("math").asin(c ** 0.5)


def geocode(name: str, lang: str = "zh", count: int = 10) -> list:
    url = (f"https://geocoding-api.open-meteo.com/v1/search?count={count}"
           f"&language={lang}&format=json&name={urllib.parse.quote(name)}")
    with OPENER.open(url, timeout=20) as r:
        return json.loads(r.read()).get("results") or []


def valid(x: dict, zh: str, province: str, pinyin: str) -> bool:
    """三重校验：行政中心条目 + 省份对得上 + 离省会不过分远。"""
    if x.get("country") not in ("中国", "China"):
        return False
    code = x.get("feature_code") or ""
    admin = (x.get("admin1") or "") + (x.get("admin2") or "")
    pk = province_key(province)
    admin_ok = (pk and pk in admin) or (EN_PROV.get(pk, "~") in admin)
    if not admin_ok:
        for alt in ADMIN_EXEMPT.get(zh, ()):
            if alt in admin or EN_PROV.get(alt, "~") in admin:
                admin_ok = True
                break
    if not admin_ok:
        return False
    if not code.startswith("PPL") and code != "PPL":
        return False
    nm = x.get("name", "")
    py = norm(pinyin)
    name_ok = (strip_suffix(nm) == zh or nm == zh or nm.startswith(zh)
               or norm(nm) == py or norm(nm) == py + "city")
    # 自治州/地区以驻地代表，驻地名必然与州名不同；此时只要求省级与距离校验，
    # 但优先返回 PPLA*（县级中心），避免拿到州内同名村
    if not name_ok and zh in RESIDENCE and code.startswith("PPLA"):
        name_ok = True
    if not name_ok:
        return False
    cap = CAPITAL.get(province)
    if cap:
        d = distance_km([x["latitude"], x["longitude"]], cap)
        # 行政中心允许 700km（跨度大的省区 1500km）；普通地名 500km 内，
        # 这条专治把外省同名村/机场当成城市（延安被匹配到海南那种）
        if code.startswith("PPLA"):
            limit = 1500 if province in WIDE else 700
        else:
            limit = 1200 if province in WIDE else 500
        if d > limit:
            return False
    return True


def lookup(zh: str, province: str, pinyin: str):
    tries = [pinyin] + ALIAS.get(zh, [])
    if zh in RESIDENCE:
        tries.append(RESIDENCE[zh])
    fallback = None
    for name in tries:
        for lang in ("zh", "en"):
            try:
                rows = geocode(name, lang)
            except Exception:
                continue
            for x in rows:
                if not valid(x, zh, province, name):
                    continue
                if (x.get("feature_code") or "").startswith("PPLA"):
                    return x                       # 行政中心优先
                if fallback is None:
                    fallback = x
    return fallback


def main() -> None:
    found, miss = {}, []
    for province, cities in GROUPS.items():
        for zh, pinyin in cities:
            if zh in EXISTING:
                continue
            hit = lookup(zh, province, pinyin)
            if hit:
                found[zh] = ([round(float(hit["latitude"]), 4),
                              round(float(hit["longitude"]), 4)],
                             hit.get("name"), hit.get("feature_code"), province)
            else:
                miss.append((zh, pinyin, province))
    print(f"命中 {len(found)} / 未命中 {len(miss)}")
    for zh, py, prov in miss:
        print("  MISS", zh, py, prov)
    out = ROOT / "src" / "travel_agent" / "city_geo.py"
    out.write_text(render(found), encoding="utf-8")
    print(f"已写入 {out}（{len(found)} 个城市）")


def render(found: dict) -> str:
    """按省份分组输出，便于人工核对。"""
    lines = [
        '"""全国地级行政区中心坐标（补齐 CITY_GEO 里没有的城市）。',
        "",
        "只放地级及以上：直辖市、地级市、自治州、地区、盟。县级市与景区仍走运行时",
        "地理编码（Open-Meteo 只认拉丁输入，中文名要先用这里的表兜住，否则一句",
        "「廊坊天气」会被误判成地名打错）。",
        "",
        "数据来源：Open-Meteo Geocoding（底层 GeoNames）。每个坐标都过了三关：",
        "  1. 条目必须是行政中心（PPLA*）或省级匹配的城镇（PPL），机场/山脉/同名村一律排除",
        '  2. 返回的中文名或拼音必须与城市名一致（"马鞍山"不能拿到陕西那个同名点）',
        "  3. 距省会距离必须合理（普通省 500~700km，跨度大的省区放宽到 1500km）",
        "自治州与地区取其行署驻地坐标（如延边→延吉、德宏→芒市、山南→泽当）。",
        "",
        "维护：改完城市表重跑 scripts/build_city_geo.py 即可再生本文件。",
        '"""',
        "",
        "EXTRA_CITY_GEO: dict[str, list[float]] = {",
    ]
    for province, cities in GROUPS.items():
        rows = [(zh, found[zh][0]) for zh, _ in cities
                if zh in found and zh not in EXISTING]
        if not rows:
            continue
        lines.append(f"    # {province}")
        for zh, geo in rows:
            lines.append(f'    "{zh}": [{geo[0]:.4f}, {geo[1]:.4f}],')
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
