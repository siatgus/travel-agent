"""LLM 网关测试：多供应商故障转移 + 按任务等级选模型 + Key 加密落盘。

防线：
1. GatewayClient：主供应商失败自动切备用、全链失败错误聚合、成功后
   model/base_url 对齐实际命中的供应商（计量口径）；
2. 轻任务路由：extract_slots 等走便宜链，rerank/未标记任务走主链；
3. MeteredClient 包 GatewayClient：用量记到真实命中模型上；
4. Key 加密：roundtrip、幂等、明文旧格式自动迁移、save/load 闭环。

运行：python test_gateway.py    失败时退出码为 1。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# —— src 布局引导 + 测试目录隔离（设置文件/主密钥都不碰真实数据） ——
_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

_TMP = Path(tempfile.mkdtemp(prefix="travel_gateway_test_"))
os.environ["TRAVEL_DATA_DIR"] = str(_TMP)

from travel_agent import llm  # noqa: E402  （env 之后导入）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


PRIMARY = {"name": "主", "api_key": "sk-p", "base_url": "https://p.example/v1",
           "model": "p-model"}
FALLBACK = {"name": "备用", "api_key": "sk-f", "base_url": "https://f.example/v1",
            "model": "f-model"}
LIGHT = {"name": "轻任务", "api_key": "sk-l", "base_url": "https://l.example/v1",
         "model": "l-model"}


def test_failover() -> None:
    calls: list[str] = []
    primary_ok = [False]                 # 第一次主供应商坏，之后恢复

    def fake(prov, system, user, *, temperature, history, timeout):
        calls.append(prov["name"])
        if prov["name"] == "主" and not primary_ok[0]:
            primary_ok[0] = True
            raise llm.LLMError("主供应商超时")
        return f"ok:{prov['model']}"

    llm.GatewayClient._call_provider = staticmethod(fake)

    gw = llm.GatewayClient(PRIMARY, [FALLBACK])
    out = gw.chat("sys", "hello")
    check("主供应商失败自动切备用", out == "ok:f-model" and calls == ["主", "备用"],
          f"{out} {calls}")
    check("成功后 model 对齐实际命中的供应商",
          gw.model == "f-model" and gw.base_url == FALLBACK["base_url"]
          and gw.last_provider == "备用", f"{gw.model} {gw.last_provider}")

    calls.clear()
    out = gw.chat("sys", "again")
    check("主供应商恢复后回到主链（无粘滞）",
          out == "ok:p-model" and calls == ["主"], f"{out} {calls}")

    def always_fail(prov, system, user, *, temperature, history, timeout):
        raise llm.LLMError(f"{prov['name']} 不可用")

    llm.GatewayClient._call_provider = staticmethod(always_fail)
    try:
        gw.chat("sys", "x")
        check("全链失败抛 LLMError", False, "未抛异常")
    except llm.LLMError as e:
        check("全链失败抛 LLMError", "主" in str(e) and "备用" in str(e), str(e)[:120])

    # json_task 语义不变：主坏一次（网络层 LLMError）→ 网关切备用 → 备用返回
    # 合法 JSON → json_task 校验通过。失败转移对上层完全透明。
    def flaky_json(prov, system, user, *, temperature, history, timeout):
        calls.append(prov["name"])
        if prov["name"] == "主":
            raise llm.LLMError("主供应商超时")
        return '{"slots": {"destination": "大理"}}'

    llm.GatewayClient._call_provider = staticmethod(flaky_json)
    gw2 = llm.GatewayClient(PRIMARY, [FALLBACK])
    val, rec = gw2.json_task("extract_slots", "sys", "user",
                             lambda raw: (raw, [], []))
    check("json_task 语义不变（失败重试内含故障转移）",
          val == {"slots": {"destination": "大理"}} and rec.ok and rec.attempts == 1,
          f"{val} {rec.as_dict()}")


def test_light_routing() -> None:
    got: list[str] = []

    def fake(prov, system, user, *, temperature, history, timeout):
        got.append(prov["name"])
        return "ok"

    llm.GatewayClient._call_provider = staticmethod(fake)
    gw = llm.GatewayClient(PRIMARY, [FALLBACK], light=LIGHT,
                           light_tasks={"extract_slots", "poi_lookup"})
    gw.task = "extract_slots"
    gw.chat("s", "u")
    check("轻任务路由到便宜链", got == ["轻任务"], str(got))
    got.clear()
    gw.task = "rerank"
    gw.chat("s", "u")
    check("重任务走主链（轻任务命中不了备用时先主后备）", got == ["主"], str(got))
    got.clear()
    gw.task = ""
    gw.chat("s", "u")
    check("未标记任务按重任务走主链", got == ["主"], str(got))


def test_metered_over_gateway() -> None:
    llm.GatewayClient._call_provider = staticmethod(
        lambda prov, system, user, *, temperature, history, timeout:
        (_ for _ in ()).throw(llm.LLMError("down")) if prov["name"] == "主"
        else "回复")

    class FakeAuth:
        """模拟 auth 模块：record_usage 收到的参数即"记账落库"内容。"""
        def __init__(self):
            self.records = []

        def record_usage(self, uid, *, model="", task="",
                         prompt_tokens=None, completion_tokens=None):
            self.records.append((uid, model, task))

    fake = FakeAuth()
    # MeteredClient.chat 内部延迟 `from . import auth`；从 sys.modules 注入替身
    import sys as _sys, types as _types
    saved = _sys.modules.get("travel_agent.auth")
    _sys.modules["travel_agent.auth"] = _types.SimpleNamespace(
        record_usage=fake.record_usage)
    try:
        gw = llm.GatewayClient(PRIMARY, [FALLBACK])
        mc = llm.MeteredClient(gw, uid=7)
        mc.task = "rerank"               # 生产链路：json_task 把 task 记在计量层
        out = mc.chat("s", "u")
        check("计量客户端包网关可用", out == "回复", out)
        check("task 已透传给内层网关（轻任务路由依赖它）", gw.task == "rerank", gw.task)
        check("用量记到实际命中的备用模型",
              fake.records and fake.records[0][0] == 7
              and fake.records[0][1] == "f-model" and fake.records[0][2] == "rerank",
              str(fake.records))
    finally:
        if saved is None:
            _sys.modules.pop("travel_agent.auth", None)
        else:
            _sys.modules["travel_agent.auth"] = saved


def test_key_encryption() -> None:
    enc = llm.encrypt_secret("sk-plain-1234567890")
    check("密文带前缀且不含明文", enc.startswith("enc:v1:") and "sk-plain" not in enc,
          enc[:40])
    check("解密还原明文", llm.decrypt_secret(enc) == "sk-plain-1234567890", "")
    check("加密幂等（已是密文原样返回）", llm.encrypt_secret(enc) == enc, "")
    check("空值不加密", llm.encrypt_secret("") == "", "")

    raw = json.loads((llm.settings_path()).read_text(encoding="utf-8")) \
        if llm.settings_path().exists() else {}
    check("主密钥已生成（data/.master_key）",
          (_TMP / ".master_key").exists(), str(_TMP))

    # 明文旧格式迁移：手写一个明文 Key 的设置文件
    sp = llm.settings_path()
    sp.write_text(json.dumps({"enabled": True, "api_key": "sk-migrate-abcdef",
                              "model": "deepseek-chat",
                              "base_url": "https://api.deepseek.com/v1"}),
                  encoding="utf-8")
    s = llm.load_settings()
    check("明文旧格式读取时解出明文", s.get("api_key") == "sk-migrate-abcdef", str(s)[:80])
    on_disk = json.loads(sp.read_text(encoding="utf-8"))
    check("迁移后落盘为密文", on_disk.get("api_key", "").startswith("enc:v1:"), "")

    # save/load 闭环：保存时加密、读取时解密
    llm.save_settings({"fallback_api_key": "sk-fb-save-9876"})
    s2 = llm.load_settings()
    check("save/load 闭环（备用 Key）", s2.get("fallback_api_key") == "sk-fb-save-9876", "")
    on_disk2 = json.loads(sp.read_text(encoding="utf-8"))
    check("落盘均为密文", all(
        (not on_disk2.get(f)) or on_disk2[f].startswith("enc:v1:")
        for f in llm.SECRET_FIELDS), str({f: on_disk2.get(f, '')[:20] for f in llm.SECRET_FIELDS}))


def test_from_settings_shapes() -> None:
    sp = llm.settings_path()
    sp.write_text(json.dumps({
        "enabled": True, "api_key": "sk-p", "model": "p-model",
        "base_url": "https://p.example/v1"}), encoding="utf-8")
    c = llm.from_settings()
    check("仅主供应商 → 单 Client（行为与旧版一致）",
          type(c).__name__ == "Client" and c.model == "p-model", type(c).__name__)

    sp.write_text(json.dumps({
        "enabled": True, "api_key": "sk-p", "model": "p-model",
        "base_url": "https://p.example/v1",
        "fallback_enabled": True, "fallback_base_url": "https://f.example/v1",
        "fallback_model": "f-model", "fallback_api_key": "sk-f",
        "light_enabled": True, "light_base_url": "https://l.example/v1",
        "light_model": "l-model", "light_api_key": "sk-l"}), encoding="utf-8")
    c2 = llm.from_settings()
    check("配齐主/备/轻 → GatewayClient", type(c2).__name__ == "GatewayClient",
          type(c2).__name__)
    check("默认轻任务名单生效",
          c2._light_tasks == set(llm.DEFAULT_LIGHT_TASKS.split(",")),
          str(c2._light_tasks))

    sp.write_text(json.dumps({
        "enabled": True, "api_key": "sk-p", "model": "p-model",
        "base_url": "https://p.example/v1",
        "fallback_enabled": True,
        "fallback_base_url": "https://f.example/v1",
        "fallback_model": "f-model"}), encoding="utf-8")   # 备用缺 Key → 不启用网关
    c3 = llm.from_settings()
    check("备用配置不完整 → 回落单 Client", type(c3).__name__ == "Client",
          type(c3).__name__)


def main() -> int:
    test_failover()
    test_light_routing()
    test_metered_over_gateway()
    test_key_encryption()
    test_from_settings_shapes()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条网关断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
