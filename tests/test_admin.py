"""admin.py 管理后端测试：重点覆盖「大语言模型设置」功能。

防线：
1. 对求值后的 admin.PAGE 提取 <script>，交给 node --check（有 node 才跑）；
2. 模型设置层单元断言（llm.settings_* 读写、脱敏、优先级）；
3. 起真实管理服务走 HTTP 全链路：设置保存/校验/脱敏回显/测试连接的错误路径。
   真实联网调用不进测试（结果不确定），连通性由「测试连接」按钮人工触发。

运行：python test_admin.py    失败时退出码为 1。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

from travel_agent import admin
from travel_agent import llm

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def _get(base: str, path: str) -> tuple[int, dict | str]:
    try:
        with urllib.request.urlopen(base + path, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    if raw.lstrip().startswith(("{", "[")):
        return 200, json.loads(raw)
    return 200, raw


def _post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {}


def find_node() -> str | None:
    exe = shutil.which("node")
    if exe:
        return exe
    managed = Path(r"C:/Users/Administrator/.workbuddy/binaries/node/versions/"
                   r"22.22.2-6/node.exe")
    return str(managed) if managed.exists() else None


# ---------------------------------------------------------------- 页面语法

def test_evaluated_js_syntax() -> None:
    node = find_node()
    if not node:
        print("[SKIP] 未找到 node，跳过 JS 语法检查")
        return
    js = re.search(r"<script>(.*?)</script>", admin.PAGE, re.S).group(1)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        tmp = f.name
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        check("求值后管理页 JS 语法合法（防换行转义回退）", r.returncode == 0,
              r.stderr.strip()[:200])
    finally:
        Path(tmp).unlink(missing_ok=True)


def _view(name: str) -> str:
    """取某个视图的 HTML 片段（到下一个视图或 </main> 为止）。"""
    m = re.search(r'<div class="view" id="v-%s">(.*?)(?=<div class="view"|</main>)'
                  % name, admin.PAGE, re.S)
    return m.group(1) if m else ""


def test_nav_layout() -> None:
    nav = re.search(r'<nav class="nav">(.*?)</nav>', admin.PAGE, re.S)
    links = re.findall(r'href="#/([a-z]+)"', nav.group(1)) if nav else []
    check("导航栏含四个视图且顺序为 概览/数据/用户/模型",
          links == ["overview", "data", "users", "model"], f"links={links}")

    data, users = _view("data"), _view("users")
    check("数据管理与用户管理已分区：数据视图无用户表",
          "poi-table" in data and "log-table" in data
          and "user-table" not in data and "usage-table" not in data)
    check("用户视图独立：只含用户/用量表，不含 POI",
          "user-table" in users and "usage-table" in users
          and "poi-table" not in users and "log-table" not in users)
    check("模型设置独立成视图",
          "llm-enabled" in _view("model") and "llm-enabled" not in data
          and "llm-enabled" not in users)
    check("模型视图含「当前生效」卡片且渲染逻辑接线",
          all(k in _view("model") for k in ("active-card", "ac-badge", "ac-model"))
          and "renderActive(" in admin.PAGE)
    check("概览视图承载看板与新鲜度表",
          "id=\"cards\"" in _view("overview") and "fresh-table" in _view("overview"))
    check("hash 路由与视图切换已接线",
          "hashchange" in admin.PAGE and "showView(" in admin.PAGE)


def test_page_served(base: str) -> None:
    status, html = _get(base, "/")
    ok = status == 200 and "大语言模型设置" in html and "testLlm" in html
    check("管理页 200 且含模型设置区块", ok, f"status={status}")


# ---------------------------------------------------------------- 设置层单元

def test_settings_roundtrip(tmp: Path) -> None:
    os.environ["TRAVEL_LLM_SETTINGS"] = str(tmp / "s1.json")
    cfg = llm.save_settings({"enabled": True, "provider": "deepseek",
                             "api_key": "sk-test-1234567890abcdef",
                             "base_url": "https://api.deepseek.com/v1",
                             "model": "deepseek-chat", "timeout": 60})
    check("save_settings 落盘并附 updated_at",
          bool(cfg.get("updated_at")) and (tmp / "s1.json").exists())

    s = llm.load_settings()
    check("load_settings 读回一致", s.get("model") == "deepseek-chat"
          and s.get("enabled") is True)

    summary = llm.settings_summary()
    check("settings_summary 脱敏：不含明文 Key",
          "sk-test-1234567890" not in json.dumps(summary)
          and summary["key_masked"].endswith("cdef") and "****" in summary["key_masked"],
          f"masked={summary['key_masked']}")
    check("settings_summary 标记 has_key", summary["has_key"] is True)


def test_mask_key() -> None:
    # 用假 Key 测打码逻辑，别把真实 Key 写进仓库
    check("mask_key 长钥露出头尾", llm.mask_key("sk-1234567890abcdef1234")
          == "sk-123****1234", llm.mask_key("sk-1234567890abcdef1234"))
    check("mask_key 短钥只露两位", llm.mask_key("abc12345") == "ab****")
    check("mask_key 空值为空串", llm.mask_key("") == "" and llm.mask_key(None) == "")


def test_from_settings_priority(tmp: Path) -> None:
    os.environ["TRAVEL_LLM_SETTINGS"] = str(tmp / "s2.json")
    (tmp / "s2.json").write_text("{}", encoding="utf-8")

    old_env = {k: os.environ.pop(k, None) for k in
               ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")}
    try:
        check("无任何配置 → from_settings 返回 None", llm.from_settings() is None)

        os.environ["OPENAI_API_KEY"] = "sk-env-key"
        os.environ["OPENAI_MODEL"] = "env-model"
        c = llm.from_settings()
        check("无设置文件时回落环境变量",
              c is not None and c.model == "env-model",
              f"model={getattr(c, 'model', None)}")

        llm.save_settings({"enabled": True, "api_key": "sk-file-key",
                           "base_url": "https://api.example.com/v1",
                           "model": "file-model", "timeout": 45})
        c = llm.from_settings()
        check("设置文件优先于环境变量",
              c is not None and c.model == "file-model" and c.api_key == "sk-file-key"
              and c.base_url == "https://api.example.com/v1" and c.timeout == 45.0,
              f"model={getattr(c, 'model', None)}")

        llm.save_settings({"enabled": False})
        c = llm.from_settings()
        check("设置文件关闭后回落环境变量",
              c is not None and c.model == "env-model",
              f"model={getattr(c, 'model', None)}")
    finally:
        for k, v in old_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


# ---------------------------------------------------------------- HTTP 链路

def test_llm_http(base: str, tmp: Path) -> None:
    os.environ["TRAVEL_LLM_SETTINGS"] = str(tmp / "s3.json")
    (tmp / "s3.json").write_text("{}", encoding="utf-8")

    status, j = _get(base, "/api/llm/settings")
    check("GET 设置：空配置回显", status == 200 and j.get("ok") is True
          and j["settings"]["has_key"] is False and j["settings"]["enabled"] is False,
          f"j={j}")

    status, j = _post(base, "/api/llm/settings",
                      {"enabled": True, "base_url": "https://api.deepseek.com/v1",
                       "model": "deepseek-chat", "timeout": 60})
    check("启用但无 Key → 拒绝保存", status == 400 and j.get("ok") is False
          and "Key" in j.get("error", ""), f"status={status} j={j}")

    status, j = _post(base, "/api/llm/settings",
                      {"enabled": True, "base_url": "not-a-url",
                       "model": "m", "api_key": "sk-valid-key-123"})
    check("Base URL 非法 → 拒绝保存", status == 400 and j.get("ok") is False,
          f"status={status} j={j}")

    status, j = _post(base, "/api/llm/settings",
                      {"enabled": True, "provider": "deepseek",
                       "base_url": "https://api.deepseek.com/v1",
                       "model": "deepseek-chat", "timeout": 60,
                       "api_key": "sk-admin-save-12345678"})
    check("完整配置 → 保存成功", status == 200 and j.get("ok") is True,
          f"status={status} j={j}")

    status, j = _get(base, "/api/llm/settings")
    s = j.get("settings", {})
    check("保存后回显：enabled/has_key 为真且 Key 脱敏",
          status == 200 and s.get("enabled") is True and s.get("has_key") is True
          and "sk-admin-save" not in json.dumps(j) and "****" in s.get("key_masked", ""),
          f"j={j}")
    a = s.get("active") or {}
    check("active 摘要与配置一致（当前生效模型可见）",
          a.get("enabled") is True and a.get("source") == "settings"
          and a.get("model") == "deepseek-chat"
          and "sk-admin-save" not in json.dumps(a) and "****" in a.get("key_masked", ""),
          f"active={a}")
    c = s.get("chat") or {}
    check("chat 探测字段存在（对话服务在线与否都返回结构）",
          isinstance(c.get("online"), bool)
          and (c["online"] is False or {"enabled", "model", "source"} <= c.keys()),
          f"chat={c}")
    # 旧版对话服务（health 无 llm 字段）不能被误判为未启用：
    # has_detail=False 时卡片必须以配置层为准显示「已启用」
    check("旧版服务 health 缺 llm 字段时带 has_detail=False 标记",
          c.get("online") is not True or c.get("has_detail") in (True, False),
          f"chat={c}")
    check("页面在服务未上报详情时回退到配置值",
          "has_detail" in admin.PAGE and "detailed?!!chat.enabled:!!a.enabled" in admin.PAGE
          and "版本较旧" in admin.PAGE)

    # 二次保存不带 Key → 不覆盖已存 Key（留空即不改）
    status, j = _post(base, "/api/llm/settings",
                      {"enabled": True, "provider": "deepseek",
                       "base_url": "https://api.deepseek.com/v1",
                       "model": "deepseek-reasoner", "timeout": 60, "api_key": ""})
    check("留空 Key 再保存：模型更新且 Key 保留",
          status == 200 and llm.load_settings().get("api_key") == "sk-admin-save-12345678"
          and llm.load_settings().get("model") == "deepseek-reasoner",
          f"status={status} j={j}")

    # 测试连接的错误路径（不联网）
    (tmp / "s4.json").write_text("{}", encoding="utf-8")
    os.environ["TRAVEL_LLM_SETTINGS"] = str(tmp / "s4.json")
    status, j = _post(base, "/api/llm/test", {})
    check("测试连接：无任何 Key 时明确报错", status == 200 and j.get("ok") is False
          and "Key" in j.get("error", ""), f"j={j}")


def test_users_http(base: str, tmp: Path) -> None:
    os.environ["TRAVEL_APP_DB"] = str(tmp / "users.db")
    from travel_agent import auth as auth_mod

    for p in ("13600004444", "13600005555"):
        r = auth_mod.send_code(p)
        assert auth_mod.login(p, r["dev_code"])["ok"]

    status, j = _get(base, "/api/users")
    users = j.get("users", [])
    check("用户列表 200 且手机号脱敏",
          status == 200 and j.get("ok") is True and len(users) >= 2
          and all("****" in u["phone"] for u in users), f"j={str(j)[:200]}")

    uid = users[-1]["id"]
    bal0 = users[-1]["balance"]
    status, j = _post(base, "/api/user/credits",
                      {"user_id": uid, "delta": 100, "reason": "测试充值"})
    check("调账充值成功", status == 200 and j.get("ok") is True, f"j={j}")
    status, j = _get(base, "/api/users")
    check("调账后余额更新",
          next(u for u in j["users"] if u["id"] == uid)["balance"] == bal0 + 100)

    status, j = _post(base, "/api/user/credits",
                      {"user_id": 99999, "delta": 10, "reason": "无此用户"})
    check("不存在用户调账 400", status == 400 and j.get("ok") is False,
          f"status={status}")

    status, j = _get(base, "/api/usage")
    check("用量流水接口 200", status == 200 and j.get("ok") is True)


def test_search_ui() -> None:
    p = admin.PAGE
    check("POI 条件搜索控件齐全",
          all(k in p for k in ('id="q-poi"', 'id="flt-cat"', 'id="flt-src"',
                               'id="poi-count"')))
    check("用户条件搜索控件齐全",
          all(k in p for k in ('id="q-user"', 'id="flt-status"', 'id="flt-min"')))
    check("用量与日志搜索控件齐全",
          all(k in p for k in ('id="q-usage"', 'id="q-usage-model"',
                               'id="q-log"', 'id="flt-log-status"')))
    check("表格行内编辑已接线",
          all(k in p for k in ('function startEdit', 'function saveRow',
                               'function cancelRow', 'id="e-name"', 'id="e-lat"')))
    check("用户行内调账已接线",
          'function applyRowAdjust' in p and 'id="radj-delta"' in p)
    check("POI 城市下拉含「全部城市」",
          '<option value="*">全部城市</option>' in p)
    check("搜索结果计数与空态提示存在",
          '命中 ' in p and '没有符合条件的 POI' in p)


def test_poi_search_http(base: str) -> None:
    status, j = _get(base, "/api/pois?city=*")
    items = j.get("items", []) if isinstance(j, dict) else []
    cities = sorted({p["city"] for p in items})
    check("POI 搜索：city=* 跨城市返回",
          status == 200 and len(cities) >= 2, f"cities={cities}")

    kw = items[0]["name"][:2] if items else "上海"
    status, j = _get(base, "/api/pois?city=*&q=" + urllib.parse.quote(kw))
    hit = j.get("items", [])
    check("POI 关键词搜索命中且每条都含关键词",
          status == 200 and bool(hit) and all(
              kw in p["name"] or kw in (p["area"] or "") or kw in p["poi_id"]
              for p in hit), f"kw={kw} n={len(hit)}")

    status, j = _get(base, "/api/pois?city=*&category=restaurant")
    hit = j.get("items", [])
    check("POI 类型筛选只返回 restaurant",
          status == 200 and bool(hit) and all(p["category"] == "restaurant" for p in hit))

    status, j = _get(base, "/api/pois?city=*&source=seed")
    hit = j.get("items", [])
    check("POI 来源筛选只返回 seed",
          status == 200 and bool(hit) and all(p["source"] == "seed" for p in hit))

    status, j = _get(base, "/api/pois?city=*&q=" + urllib.parse.quote("绝不存在zzz"))
    check("POI 搜索无结果返回空列表", status == 200 and j.get("items") == [])

    status, j = _get(base, "/api/pois?city=*&category=restaurant&source=seed&q="
                     + urllib.parse.quote("上海"))
    check("POI 多条件组合生效",
          status == 200 and all(p["city"] == "上海" and p["category"] == "restaurant"
                                for p in j.get("items", [])))


def test_inline_edit_http(base: str) -> None:
    """模拟前端行内编辑：只改名称/费用，其余字段原样回传，验证不被默认值覆盖。"""
    status, s = _get(base, "/api/stats")
    city = (s.get("freshness") or [{"city": "大理"}])[0]["city"]

    status, j = _post(base, "/api/poi/save", {"city": city, "item": {
        "name": "行内编辑测试点", "geo": [30.0, 120.0], "category": "attraction",
        "area": "测试区", "appeal": 4, "duration_min": 60, "cost": 0,
        "open_hours": "09:00-17:00", "tags": ["测试"], "note": "保留我",
        "intensity": "high", "indoor_alternative": "室内备选"}})
    pid = j.get("poi_id")
    check("行内编辑：先建测试 POI", status == 200 and bool(pid), f"j={j}")
    if not pid:
        return

    try:
        status, j = _get(base, "/api/pois?city=" + urllib.parse.quote(city))
        row = next(p for p in j["items"] if p["poi_id"] == pid)

        item = {"poi_id": row["poi_id"], "name": "行内编辑改名",
                "category": row["category"], "area": row["area"],
                "geo": [row["geo_lat"], row["geo_lon"]], "appeal": row["appeal"],
                "duration_min": row["duration_min"], "cost": 55,
                "open_hours": row["open_hours"], "open_days": row["open_days"],
                "need_booking": bool(row["need_booking"]),
                "tags": json.loads(row["tags"] or "[]"),
                "intensity": row["intensity"],
                "indoor_alternative": row["indoor_alternative"],
                "note": row["note"]}
        status, j = _post(base, "/api/poi/save", {"city": city, "item": item})
        check("行内编辑保存成功", status == 200 and j.get("ok"), f"j={j}")

        status, j = _get(base, "/api/pois?city=" + urllib.parse.quote(city))
        row2 = next(p for p in j["items"] if p["poi_id"] == pid)
        check("行内编辑：改动生效且 tags/note/intensity 未被默认值覆盖",
              row2["name"] == "行内编辑改名" and row2["cost"] == 55
              and json.loads(row2["tags"]) == ["测试"]
              and row2["note"] == "保留我" and row2["intensity"] == "high",
              f"row2={row2}")
    finally:
        _post(base, "/api/poi/delete", {"poi_id": pid})
        status, j = _get(base, "/api/pois?city=" + urllib.parse.quote(city))
        check("行内编辑测试数据已清理",
              all(p["poi_id"] != pid for p in j.get("items", [])))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="travel_admin_test_"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"（测试服务已起：{base}）\n")

    test_evaluated_js_syntax()
    test_nav_layout()
    test_search_ui()
    test_settings_roundtrip(tmp)
    test_mask_key()
    test_from_settings_priority(tmp)
    test_page_served(base)
    test_llm_http(base, tmp)
    test_poi_search_http(base)
    test_inline_edit_http(base)
    test_users_http(base, tmp)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条管理端断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
