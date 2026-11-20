#!/usr/bin/env python
"""P2.6 批次 5.0 第二阶段 · 离线影子采集（shadow_collect）。

职责
----
用一份人工构造的观察语料驱动**真实的** Orchestrator，读取 P2.6 影子触点
写入的决策记录，导出为可离线分析的 JSONL。不接入生产链路、不启动 server、
不写仓库。

授权边界（评审 2026-11-20 CONDITIONAL PASS）
-------------------------------------------
允许：本脚本、shadow_report.py、tests/observ_corpus.tsv、
      tests/test_shadow_offline.py、文档 §26 实施记录。
禁止：修改 src/ 任何文件、生产 JSONL、TRAVEL_SHADOW_LOG、Web API、
      R-01 容量策略、P3 / TAKEOVER / 灰度。

两个层次的字段契约（本脚本**硬编码**，不 import 生产常量）
--------------------------------------------------------
- 内部记录：11 键，与 P2.6 §25 契约一致；结构不符即 fail-loud。
  之所以硬编码而不 import，是为了防止「生产常量与生产记录一起改变时，
  脚本同步接受意外 schema 变化」（评审明确要求）。
- 外部 JSONL：12 字段，显式白名单，**排除 entity_text**。
  entity_text 可能含用户问句子串，即便导出白名单已排除 utterance，
  也不能据此保证产物不含原文片段，故一律不导出。

退出码
------
0 成功 / 1 参数或输出路径不允许（SystemExit 字符串）/ 2 argparse 用法错误
3 语料错误 / 4 记录契约不符 / 5 出现网络尝试（离线性不成立）
6 采集内部失败 / 7 网络闸门安装不完整（fail-closed，未开始采集）
  （6 亦覆盖 pin_env() 固定开关中途失败：该函数已就地回滚，失败原因随消息上报）

退出码与产物的关系（精确表述，不得简化成「非 0 就一定没有文件」）
  3 / 4 / 5 / 7 一律发生在 write_jsonl() 之前 → 不产生 JSONL、不留临时文件。
  6 有两个来源：写出前（全部 case 无记录、对照运行仍产生记录等）→ 无产物；
  写出后（闸门 / 环境变量 / 临时目录未能复原）→ JSONL 可能已存在，但摘要
  不输出、退出码非 0，属「产物存在但运行未成功」，须人工判读。清理失败一律
  以 [WARN] 清理未完成 打到 stderr，不静默吞掉。

网络隔离的证明边界（不得外推）
----------------------------
本脚本只监控三个 socket 入口：socket.create_connection、socket.getaddrinfo、
socket.socket.connect。`network_attempts == 0` 只能说明「本次采集期间，
三个受监控入口未检测到网络尝试」，**不等价于对进程全部网络行为的绝对证明**
（例如直接 UDP sendto、子进程、非 socket 的 I/O 均不在监控范围内）。

用法
----
    python scripts/shadow_collect.py --corpus tests/observ_corpus.tsv \
        --out /path/outside/repo/run.jsonl [--force] [--kb PATH]

stdout 只输出一个 JSON 摘要（便于机器核对）；进度日志走 stderr。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------- 常量

REPO_ROOT = Path(__file__).resolve().parents[1]

# 内部记录契约：11 键，顺序与 P2.6 §25.4 一致（硬编码，零 import）
INTERNAL_KEYS = (
    "intent", "takeover", "batch_enabled", "action", "reason",
    "entity_text", "statuses", "city_source", "entity_city",
    "confidence", "candidates_count",
)

# 外部 JSONL 契约：12 字段 = case_id + category + 10 个安全观察字段。
# 与内部 11 键的差：去掉 entity_text，加上 case_id 与 category。
EXPORT_KEYS = (
    "case_id", "category", "intent", "takeover", "batch_enabled",
    "action", "reason", "statuses", "city_source", "entity_city",
    "confidence", "candidates_count",
)

ALLOWED_CATEGORIES = (
    "weather", "poi", "transport", "plan", "plan_qa", "smalltalk", "other",
)

# 采集中被固定的环境变量（值, 是否必须在采集前断言）
ENV_PINS = {
    "USE_REAL_DATA": "0",           # tools.py 走 mock_tools（零 urllib）
    "TRAVEL_AUTOFETCH": "0",        # autofetch.ENABLED = False（import 期求值）
    "TRAVEL_DECISION_SHADOW": "1",  # 打开影子触达
    "TRAVEL_PENDING_CHANNEL": "0",  # 关闭 pending 通道（避免同轮两次 decide）
    "TRAVEL_DECISION_TAKEOVER": "0",
}

_ASSERTED_ENV = ("TRAVEL_DECISION_SHADOW", "TRAVEL_PENDING_CHANNEL",
                 "TRAVEL_DECISION_TAKEOVER")

# 被监控的网络入口（写入摘要，避免把「三个入口 0 次」读成「进程零网络」）
MONITORED_ENTRY_POINTS = (
    "socket.create_connection",
    "socket.getaddrinfo",
    "socket.socket.connect",
)

NETWORK_SCOPE_NOTE = (
    "仅覆盖上述三个受监控 socket 入口；0 次尝试不等于对进程全部网络行为的绝对证明"
)


class CorpusError(Exception):
    """语料格式错误（退出码 3）。"""


class ContractError(Exception):
    """记录结构不符内部契约（退出码 4）。"""


class NetworkAttempted(Exception):
    """离线闸门拦截到网络尝试（退出码 5）。"""


class CollectError(Exception):
    """采集内部失败（退出码 6）。"""


class GuardIncomplete(Exception):
    """网络闸门未能完整安装（退出码 7，fail-closed 中止）。"""


class EnvPinError(CollectError):
    """固定环境变量时中途失败（退出码 6）。

    `pin_env()` 已把该项失败**就地回滚**；回滚本身失败的名字会写进消息，
    不静默丢弃（复审 R3，评审 2026-11-20）。
    """


# ---------------------------------------------------------------- 网络闸门

class NetworkGuard:
    """运行时网络尝试计数：监控 connect / create_connection / getaddrinfo。

    agent.dispatch == 0 只能证明没有走那个入口，不能证明整个进程没有
    网络行为（评审 2026-11-20 明确）。本闸门把「三个受监控 socket 入口的
    连接尝试」变成可计数、可失败的事件。

    证明边界（评审 R1 要求收紧，不得外推）：
      计数为 0 仅表示「本次采集期间，socket.create_connection /
      socket.getaddrinfo / socket.socket.connect 未检测到尝试」；
      不覆盖 UDP sendto、子进程、非 socket I/O，**不构成对进程全部网络
      行为的绝对证明**。

    安装完整性：socket.socket.connect 是 C 槽位，个别实现不可写。写失败时
    置 ``partial=True`` 并降级记录，但**安装不完整即视为离线前提不成立**，
    调用方必须 fail-closed（本脚本 main() 直接以退出码 7 中止，不开始采集）。

    事务性（复审 R2，评审 2026-11-20）：
      - ``install()`` 任一步失败都会**回滚已完成的替换**，再抛
        :class:`GuardIncomplete`；不会留下「前两个入口已替换、active 仍为 False」
        的半装状态。
      - ``uninstall()`` 尽力还原并**返回还原失败的入口名**（不抛出），
        使调用方能在同一 finally 里继续恢复 dispatch / 环境变量 / 临时目录。
      - 只要还有未还原的入口，``active`` 就保持 True，外层 finally 可再试一次。
    """

    _RESTORE_ORDER = ("connect", "getaddrinfo", "create_connection")

    def __init__(self) -> None:
        self.attempts: list[tuple[str, str]] = []
        self._orig: dict[str, object] = {}
        self.active = False
        self.partial = False

    # -------------------------------------------------- 内部：按名读写
    def _set(self, name: str, value: object) -> None:
        if name == "create_connection":
            socket.create_connection = value
        elif name == "getaddrinfo":
            socket.getaddrinfo = value
        elif name == "connect":
            socket.socket.connect = value
        else:                                  # pragma: no cover - 防御
            raise AssertionError(f"未知入口：{name}")

    def _restore(self, names) -> list[str]:
        """尽力还原给定入口；返回**还原失败**的名字（不抛出）。

        用 ``name not in self._orig`` 判断「是否曾替换过」，不能用取值为 None
        判断 —— 原实现可能是 None（替身模块），跳过就会漏还原。
        """
        failed: list[str] = []
        for name in names:
            if name not in self._orig:
                continue
            try:
                self._set(name, self._orig[name])
            except BaseException as exc:       # 记录后继续还原其余入口
                failed.append(f"{name}（{type(exc).__name__}: {exc}）")
            else:
                self._orig.pop(name, None)     # 还原成功才移除备份
        return failed

    def install(self) -> None:
        """事务式安装；**三个入口在同一个事务里**，任一步失败即回滚已完成部分。

        复审 R3（评审 2026-11-20）指出的缺口：R2 版本只把前两个入口放进 try，
        第三个入口 ``connect`` 单独用 ``except (AttributeError, TypeError)`` 处理，
        于是「赋值抛 RuntimeError」时既不回滚、也不置 active，前两个入口就此留在
        进程里。现在统一处理：

        - 三个入口都按顺序装入同一个 try；
        - ``connect`` 的 AttributeError / TypeError 仍视为 C 槽位不可写
          （``partial=True``），但**同样先回滚**已替换入口，再 fail-closed；
        - 其他任何异常（含 RuntimeError）一律回滚后抛 ``GuardIncomplete``；
        - 成功返回时三个入口全部替换，``active=True``。

        不变式：``install()`` 要么三入口全部生效，要么 socket 模块保持原样并抛
        ``GuardIncomplete``（唯一例外是回滚自身失败——此时 ``active`` 保持 True，
        由 ``main()`` 外层 finally 重试并把失败上报）。
        """
        self._orig["create_connection"] = socket.create_connection
        self._orig["getaddrinfo"] = socket.getaddrinfo
        self._orig["connect"] = socket.socket.connect

        def deny_create(address, *a, **k):
            self.attempts.append(("create_connection", repr(address)))
            raise NetworkAttempted(f"create_connection denied: {address!r}")

        def deny_gai(host, port, *a, **k):
            self.attempts.append(("getaddrinfo", f"{host}:{port}"))
            raise NetworkAttempted(f"getaddrinfo denied: {host}:{port}")

        def deny_connect(sock, address, *a, **k):
            self.attempts.append(("socket.connect", repr(address)))
            raise NetworkAttempted(f"socket.connect denied: {address!r}")

        done: list[str] = []
        c_slot: str | None = None
        try:
            self._set("create_connection", deny_create)
            done.append("create_connection")
            self._set("getaddrinfo", deny_gai)
            done.append("getaddrinfo")
            try:
                self._set("connect", deny_connect)
            except (AttributeError, TypeError) as exc:
                # C 槽位不可写：先记下降级事实，再回滚前两个入口（不能带着半装退出）
                self.partial = True
                c_slot = f"C 槽位不可写（{type(exc).__name__}: {exc}）"
                raise
            done.append("connect")
        except BaseException as exc:
            # 事务回滚：把已经替换的入口按逆序还原
            failed = self._restore(tuple(reversed(done)))
            if not failed:
                # 全部还原成功 → 备份已无意义（含从未生效的 connect 备份）
                self._orig.clear()
            # 回滚未完成 → 保持 active，交给 main() 外层 finally 再试一次
            self.active = bool(failed)
            reason = c_slot or f"{type(exc).__name__}: {exc}"
            prefix = "网络闸门安装不完整" if c_slot else "网络闸门安装失败"
            raise GuardIncomplete(
                f"{prefix}（{reason}），已回滚 {done or '无'}；"
                f"回滚未完成：{failed or '无'}；未开始采集") from exc
        self.active = True

    def uninstall(self) -> list[str]:
        """尽力还原；返回还原失败的入口名（不抛出，便于调用方继续清理）。"""
        if not self.active:
            return []
        failed = self._restore(self._RESTORE_ORDER)
        self.active = bool(failed)              # 仍有残留 → 允许外层再试一次
        return failed


# ---------------------------------------------------------------- 环境与路径

_SENTINEL = "<unset>"


def snapshot_env(keys) -> dict[str, str]:
    return {k: os.environ.get(k, _SENTINEL) for k in keys}


def pin_env(extra: dict[str, str] | None = None,
            shadow_mode: str = "on") -> dict[str, str]:
    """保存并固定环境变量；返回保存的原值（含 <unset> 标记）。

    事务式：任一项写入抛出即按**已写入项**逆序回滚，再抛 ``EnvPinError``。
    复审 R3（评审 2026-11-20）指出的缺口是 R2 版本在循环里直接写，
    中途抛出时调用方的 ``saved`` 仍未赋值，外层 ``finally`` 的 ``if saved:``
    不成立 → 已改写的键永久留在进程里。现在回滚在 ``pin_env`` 内部完成，
    不依赖调用方是否拿到返回值。

    **回滚失败必须显式记录**：失败的名字与原因写进异常消息（``回滚未完成``），
    由 ``main()`` 打到 stderr 并以退出码 6 结束，不静默忽略。
    """
    pins = dict(ENV_PINS)
    if shadow_mode == "off":
        # 仅用于「影子是否增加 dispatch」的对照运行：关掉影子触点，
        # 其余环境（离线、mock 工具、pending 关闭）逐项保持不变。
        pins["TRAVEL_DECISION_SHADOW"] = "0"

    saved: dict[str, str] = {}
    applied: list[str] = []                     # 已实际改写的键（按写入顺序）
    try:
        for k, v in pins.items():
            saved[k] = os.environ.get(k, _SENTINEL)
            os.environ[k] = v
            applied.append(k)
        # 防止外层环境把影子在线探测打开（会真联网）
        saved["TRAVEL_RESOLVER_ONLINE"] = os.environ.pop("TRAVEL_RESOLVER_ONLINE",
                                                         _SENTINEL)
        applied.append("TRAVEL_RESOLVER_ONLINE")
        for k, v in (extra or {}).items():
            saved.setdefault(k, os.environ.get(k, _SENTINEL))
            os.environ[k] = v
            applied.append(k)
    except BaseException as exc:
        restored, failed = _rollback_env(applied, saved)
        raise EnvPinError(
            f"固定开关失败（{type(exc).__name__}: {exc}）；"
            f"已回滚 {restored}/{len(applied)} 项；"
            f"回滚未完成：{failed or '无'}") from exc
    return saved


def _rollback_env(applied: list[str], saved: dict[str, str]) -> tuple[int, list[str]]:
    """按已写入项逆序还原；返回**实际还原成功数**与**回滚失败**的键。

    不抛出：回滚失败由调用方写进异常消息上报，不能静默忽略。
    成功数按真实写入结果计数，不用「总数 - 失败数」倒推——否则回滚被短路时
    消息仍会声称「已全部还原」。
    """
    restored = 0
    failed: list[str] = []
    for k in reversed(applied):
        value = saved.get(k, _SENTINEL)
        try:
            if value == _SENTINEL:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value
        except BaseException as exc:            # 记录后继续还原其余键
            failed.append(f"{k}（{type(exc).__name__}: {exc}）")
        else:
            restored += 1
    return restored, failed


def restore_env(saved: dict[str, str]) -> None:
    for k, v in saved.items():
        if v == _SENTINEL:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _data_dir_from_env() -> Path | None:
    raw = os.environ.get("TRAVEL_DATA_DIR")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def validate_out_path(raw: str) -> Path:
    """--out 必须位于仓库目录与 TRAVEL_DATA_DIR 之外。

    用 realpath/resolve 规范化后再判，不使用字符串前缀比较——符号链接
    与 `..` 都能绕过前缀判断（评审 2026-11-20 明确要求）。
    """
    p = Path(raw).expanduser()
    resolved = p.resolve()
    if resolved.is_dir():
        raise SystemExit(f"[FATAL] --out 指向目录而非文件：{resolved}")
    forbidden: list[tuple[str, Path]] = [("仓库根", REPO_ROOT.resolve())]
    data_dir = _data_dir_from_env()
    if data_dir is not None:
        forbidden.append(("TRAVEL_DATA_DIR", data_dir))
    for name, base in forbidden:
        if _is_within(resolved, base):
            raise SystemExit(
                f"[FATAL] --out 不得位于{name}内（规范化后）：{resolved}")
    return resolved


# ---------------------------------------------------------------- 语料

def load_corpus(path: Path) -> list[dict]:
    if not path.is_file():
        raise CorpusError(f"语料不存在：{path}")
    rows: list[dict] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\r\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = line.split("\t")
            if parts[0] == "case_id":           # 表头
                continue
            if len(parts) != 4:
                raise CorpusError(
                    f"第 {lineno} 行列数为 {len(parts)}，应为 4（制表符分隔）")
            cid, cat, utt, ctx_raw = parts
            if not cid:
                raise CorpusError(f"第 {lineno} 行 case_id 为空")
            if cid in seen:
                raise CorpusError(f"第 {lineno} 行 case_id 重复：{cid}")
            if cat not in ALLOWED_CATEGORIES:
                raise CorpusError(
                    f"第 {lineno} 行 category 非法：{cat!r}；允许 {ALLOWED_CATEGORIES}")
            if not utt.strip():
                raise CorpusError(f"第 {lineno} 行 utterance 为空")
            try:
                ctx = json.loads(ctx_raw)
            except json.JSONDecodeError as exc:
                raise CorpusError(
                    f"第 {lineno} 行 context 不是合法 JSON：{exc}") from exc
            if not isinstance(ctx, dict):
                raise CorpusError(f"第 {lineno} 行 context 必须是 JSON 对象")
            seen.add(cid)
            rows.append({"case_id": cid, "category": cat,
                         "utterance": utt, "context": ctx})
    if not rows:
        raise CorpusError("语料为空（含注释与表头之外没有数据行）")
    return rows


# ---------------------------------------------------------------- 契约校验

def validate_record(case_id: str, rec) -> None:
    if not isinstance(rec, dict):
        raise ContractError(
            f"{case_id}：记录类型为 {type(rec).__name__}，应为 dict")
    keys = set(rec)
    want = set(INTERNAL_KEYS)
    if keys != want:
        missing = sorted(want - keys)
        extra = sorted(keys - want)
        raise ContractError(
            f"{case_id}：内部记录键不匹配；缺失={missing}；多余={extra}")


def project_record(case_id: str, category: str, rec: dict) -> dict:
    """按导出白名单投影为 12 字段（显式取键，禁止整条复制）。"""
    out = {"case_id": case_id, "category": category}
    for key in EXPORT_KEYS:
        if key in ("case_id", "category"):
            continue
        out[key] = rec.get(key)
    if set(out) != set(EXPORT_KEYS):
        raise ContractError(
            f"{case_id}：导出字段集不符；得到 {sorted(out)}；"
            f"应为 {sorted(EXPORT_KEYS)}")
    if "entity_text" in out:
        raise ContractError(f"{case_id}：导出记录不得包含 entity_text")
    return out


# ---------------------------------------------------------------- 采集

def _apply_context(session, ctx: dict) -> None:
    for k, v in (ctx.get("slots") or {}).items():
        session.slots[k] = v
    if ctx.get("plan_made"):
        session.plan_made = True
        session.last_plan = ctx.get("last_plan") or {"days": [{"day": 1, "spots": []}]}


def collect(corpus: list[dict], Session, Orchestrator) -> tuple[list[dict], dict]:
    """逐 case 采集；每 case 独立 Session + 独立 Orchestrator。"""
    exported: list[dict] = []
    per_case: dict[str, int] = {}
    no_record: list[str] = []
    warnings: dict[str, list[str]] = {}

    for case in corpus:
        cid = case["case_id"]
        session = Session(use_llm=False)
        _apply_context(session, case["context"])
        orc = Orchestrator()
        # case 隔离：新实例的记录容器必须尚未创建，杜绝跨 case 残留
        if getattr(orc, "_decision_shadow_log", None) is not None:
            raise CollectError(f"{cid}：新 Orchestrator 的记录容器非空，隔离失效")
        orc.handle(session, case["utterance"])

        log = getattr(orc, "_decision_shadow_log", None)
        if log is None:
            per_case[cid] = 0
            no_record.append(cid)
            warn = list(getattr(orc, "_pending_warnings", []) or [])
            if warn:
                warnings[cid] = warn
            continue
        if not isinstance(log, list):
            raise ContractError(
                f"{cid}：_decision_shadow_log 类型为 {type(log).__name__}，应为 list")
        if len(log) != 1:
            # 每轮恰好一次 decide → 恰好一条记录；0 条属异常（上面已分流），
            # 多条说明触点被重复触达，必须暴露而不是取最后一条。
            raise ContractError(
                f"{cid}：本轮记录数为 {len(log)}，契约要求恰好 1 条")
        rec = log[0]
        validate_record(cid, rec)
        exported.append(project_record(cid, case["category"], rec))
        per_case[cid] = 1

    return exported, {"per_case": per_case, "no_record": no_record,
                      "warnings": warnings}


# ---------------------------------------------------------------- 写出

def write_jsonl(rows: list[dict], out_path: Path, force: bool) -> str:
    """原子写出：先写临时文件，全部成功后 replace；失败不留半成品。"""
    if out_path.exists() and not force:
        raise SystemExit(
            f"[FATAL] 输出文件已存在，默认拒绝覆盖：{out_path}（如需覆盖加 --force）")
    tmp = out_path.with_name(out_path.name + f".tmp.{os.getpid()}")
    if tmp.exists():
        tmp.unlink()
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False,
                                    separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out_path)
    except BaseException:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    return hashlib.sha256(out_path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="P2.6 离线影子采集（不写仓库、不联网、不改生产）")
    ap.add_argument("--corpus", required=True, help="4 列 TSV 观察语料")
    ap.add_argument("--out", required=True, help="输出 JSONL 路径（仓库与数据目录之外）")
    ap.add_argument("--force", action="store_true", help="允许覆盖已有输出文件")
    ap.add_argument("--kb", default=None, help="指定 KB 文件（默认复制仓库 data/.kb.db）")
    ap.add_argument("--shadow-mode", choices=("on", "off"), default="on",
                    help="off 仅用于「影子是否增加 dispatch」对照运行，"
                         "不产出数据文件；off 下不写 JSONL")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 1) 路径校验必须在改动任何环境变量之前（此时 TRAVEL_DATA_DIR 仍是原值）
    out_path = validate_out_path(args.out)
    corpus_path = Path(args.corpus).expanduser().resolve()

    env_before = snapshot_env(sorted(set(ENV_PINS) | {"TRAVEL_RESOLVER_ONLINE",
                                                      "TRAVEL_KB_DB",
                                                      "TRAVEL_APP_DB"}))
    tmpdir: Path | None = None
    guard = NetworkGuard()
    saved: dict[str, str] = {}
    dispatch = {"calls": 0, "names": []}
    # 清理阶段收集的失败（评审 R2：不得静默吞掉恢复失败）
    cleanup_failures: list[str] = []
    summary: dict | None = None
    rc = 0
    try:
        corpus = load_corpus(corpus_path)

        # 2) KB 复制到临时目录：仓库 data/ 只被读取，SQLite 的 WAL 不落仓库
        tmpdir = Path(tempfile.mkdtemp(prefix="p26obs-"))
        kb_src = Path(args.kb).expanduser().resolve() if args.kb \
            else (Path(os.environ.get("TRAVEL_KB_DB")).expanduser().resolve()
                  if os.environ.get("TRAVEL_KB_DB")
                  else (REPO_ROOT / "data" / ".kb.db"))
        kb_copy = tmpdir / "kb.db"
        if kb_src.is_file():
            shutil.copy2(kb_src, kb_copy)
        extra = {"TRAVEL_KB_DB": str(kb_copy),
                 "TRAVEL_APP_DB": str(tmpdir / "app.db")}

        # 3) 固定开关（保存原值，finally 恢复）
        saved = pin_env(extra, args.shadow_mode)
        if "src" not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "src"))

        import travel_agent.agent as agent_mod          # noqa: E402
        import travel_agent.autofetch as autofetch      # noqa: E402
        import travel_agent.tools as tools              # noqa: E402
        from travel_agent.dialogue import Session       # noqa: E402
        from travel_agent.orchestrator.orchestrator import Orchestrator  # noqa: E402

        # 4) 采集前断言：开关实际值 + 离线实现
        expect = {
            "TRAVEL_DECISION_SHADOW": "0" if args.shadow_mode == "off"
                                      else ENV_PINS["TRAVEL_DECISION_SHADOW"],
            "TRAVEL_PENDING_CHANNEL": "0",
            "TRAVEL_DECISION_TAKEOVER": "0",
        }
        actual = {k: os.environ.get(k) for k in expect}
        if actual != expect:
            raise CollectError(f"开关断言失败：实际 {actual}，期望 {expect}")
        if autofetch.ENABLED:
            raise CollectError("autofetch.ENABLED 为 True，离线隔离不成立")
        tools_mod = tools.dispatch.__module__
        if not tools_mod.endswith("mock_tools"):
            raise CollectError(f"工具层不是 mock 实现：{tools_mod}")

        # 5) dispatch 计数（agent 与 tools 两处入口都包）
        orig_agent_dispatch = agent_mod.dispatch
        orig_tools_dispatch = tools.dispatch

        def wrap_dispatch(inner):
            def _w(name, *a, **k):
                dispatch["calls"] += 1
                dispatch["names"].append(name)
                return inner(name, *a, **k)
            return _w

        agent_mod.dispatch = wrap_dispatch(orig_agent_dispatch)
        tools.dispatch = wrap_dispatch(orig_tools_dispatch)

        # 6) 装网络闸门后再采集；安装不完整必须 fail-closed，绝不"带着降级继续跑"
        #    注意：install() 必须在 try 之内——它自己也会抛（R2 事务式安装失败），
        #    否则 dispatch 包装会留在进程里，污染同进程的下一次调用。
        try:
            guard.install()
            if guard.partial:
                raise GuardIncomplete(
                    "网络闸门安装不完整（socket.socket.connect 不可写），"
                    "已中止采集且未写出任何产物")
            exported, detail = collect(corpus, Session, Orchestrator)
        finally:
            # 分层清理（评审 R2）：闸门还原失败不得阻断 dispatch 恢复；
            # 两个 dispatch 各自独立 finally，任何一个失败都不跳过另一个。
            try:
                failed = guard.uninstall()
                if failed:
                    cleanup_failures.append(f"socket 入口未还原：{failed}")
            except BaseException as exc:       # 不静默：记录后继续清理
                cleanup_failures.append(
                    f"guard.uninstall() 抛出 {type(exc).__name__}: {exc}")
            finally:
                try:
                    agent_mod.dispatch = orig_agent_dispatch
                finally:
                    tools.dispatch = orig_tools_dispatch

        if guard.attempts:
            raise NetworkAttempted(
                f"采集期间拦截到 {len(guard.attempts)} 次网络尝试：{guard.attempts[:5]}")

        if args.shadow_mode == "off":
            # 对照运行：不产出数据；若仍有记录，说明影子开关没生效，必须报错
            if exported:
                raise CollectError(
                    f"对照运行（shadow=off）仍产生 {len(exported)} 条记录，"
                    "影子开关未生效")
            sha = None
            out_bytes = 0
        else:
            if not exported:
                raise CollectError("全部 case 均未产生记录，拒绝写出空产物")
            sha = write_jsonl(exported, out_path, args.force)
            out_bytes = out_path.stat().st_size

        summary = {
            "ok": True,
            "shadow_mode": args.shadow_mode,
            "corpus": str(corpus_path),
            "corpus_cases": len(corpus),
            "exported_records": len(exported),
            "per_case_records": detail["per_case"],
            "no_record_cases": detail["no_record"],
            "no_record_warnings": detail["warnings"],
            "out": str(out_path),
            "out_sha256": sha,
            "out_bytes": out_bytes,
            "export_keys": list(EXPORT_KEYS),
            "internal_keys": list(INTERNAL_KEYS),
            "kb_source": str(kb_src),
            "kb_copy": str(kb_copy),
            "kb_copied": kb_copy.is_file(),
            "offline": {
                "use_real_data": os.environ.get("USE_REAL_DATA"),
                "tools_module": tools_mod,
                "autofetch_enabled": bool(autofetch.ENABLED),
                "network_attempts": len(guard.attempts),
                "monitored_entry_points": list(MONITORED_ENTRY_POINTS),
                "network_attempts_scope": NETWORK_SCOPE_NOTE,
                "guard_partial": guard.partial,
                "dispatch_calls": dispatch["calls"],
                "dispatch_names": dispatch["names"],
            },
            "env_before": env_before,
            "env_during": snapshot_env(sorted(set(ENV_PINS) | {"TRAVEL_KB_DB",
                                                               "TRAVEL_APP_DB"})),
        }
        # 摘要延后到清理全部完成后再输出：清理失败时不得声称成功
    except CorpusError as exc:
        print(f"[FATAL] 语料错误：{exc}", file=sys.stderr)
        rc = 3
    except ContractError as exc:
        print(f"[FATAL] 记录契约不符：{exc}", file=sys.stderr)
        rc = 4
    except NetworkAttempted as exc:
        print(f"[FATAL] 离线性不成立：{exc}", file=sys.stderr)
        rc = 5
    except GuardIncomplete as exc:
        print(f"[FATAL] 离线前提不成立：{exc}", file=sys.stderr)
        rc = 7
    except CollectError as exc:
        print(f"[FATAL] 采集失败：{exc}", file=sys.stderr)
        rc = 6
    finally:
        # 三层独立清理：任一层异常都不跳过后面的恢复动作（评审 R2）
        try:
            if guard.active:
                failed = guard.uninstall()
                if failed:
                    cleanup_failures.append(f"外层重试仍未还原 socket 入口：{failed}")
        except BaseException as exc:
            cleanup_failures.append(
                f"外层 guard.uninstall() 抛出 {type(exc).__name__}: {exc}")
        try:
            if saved:
                restore_env(saved)
        except BaseException as exc:
            cleanup_failures.append(
                f"环境变量恢复失败 {type(exc).__name__}: {exc}")
        try:
            if tmpdir is not None and tmpdir.exists():
                shutil.rmtree(tmpdir, ignore_errors=True)
        except BaseException as exc:
            cleanup_failures.append(
                f"临时目录清理失败 {type(exc).__name__}: {exc}")

    # 清理结果在返回前统一处理：保留原始异常（rc），不静默吞掉恢复失败
    if cleanup_failures:
        for item in cleanup_failures:
            print(f"[WARN] 清理未完成：{item}", file=sys.stderr)
        if rc == 0:                            # 采集本身成功但状态未复原 → 判失败
            return 6
        return rc
    if rc != 0:
        return rc
    assert summary is not None                 # rc == 0 意味着摘要已装配
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
