"""PendingDisambiguation 多轮消歧状态契约（Phase 5 P1.5）。

设计依据 docs/PHASE5_DESIGN.md v3.2.1 §7。本模块只交付**契约与
测试桩**，不上线、不接业务、不修改 Orchestrator（P2 才实现消歧
通道的运行时消费）。

九条消费规则（§7，逐条进 tests/test_pending.py）：
1. 用户按序号选择只能引用当前 pending 的候选快照；候选 ID 与展示
   顺序冻结，会话期间 KB 变化不影响快照。
2. 用户取消 → status=cancelled，pending 清除。
3. 用户提出新问题（与 pending 无关的意图）→ 不得误消费旧 pending。
4. 用户修改目的地 → 候选城市归属已变化，重新走 EntityResolver，
   旧快照作废（由调用方重新验证；store 只负责清除旧 pending）。
5. pending 过期 → status=expired，不得继续执行。
6. 同一个 pending 只能成功消费一次；open→consumed 迁移**原子化**
   （锁内单步条件迁移，等效 UPDATE...WHERE status='open' 的受影响
   行数检查；禁止先读后写的两步式判断）。
7. 用户选择分店只完成消歧，不等于授权执行行程修改（调用方契约，
   本模块在 consume 返回值中如实回传所选候选，不产生任何行程动作）。
8. 同一 Session 同时最多一个可消费的 active pending；存在 active
   时再 create 一律拒绝。
9. 选择引用的 candidate_id 必须属于原快照；快照外 ID 一律拒绝。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

# ---- pending 状态枚举（§7） ----
PENDING_OPEN = "open"
PENDING_CONSUMED = "consumed"
PENDING_CANCELLED = "cancelled"
PENDING_EXPIRED = "expired"
PENDING_STATUSES = (PENDING_OPEN, PENDING_CONSUMED, PENDING_CANCELLED,
                    PENDING_EXPIRED)

# ---- 候选快照逐条字段（v3 契约，冻结不再变） ----
SNAPSHOT_FIELDS = ("candidate_id", "display_order", "entity_id",
                   "name", "city", "address")

# 默认有效期（P2 可配置；过期后不得继续执行）
DEFAULT_TTL_SECONDS = 300


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _epoch(value) -> int:
    """任意时间值 → UTC epoch 整数（唯一比较格式，P2.2 时间契约）。

    数值直接取整；aware datetime 按其时区转 epoch；naive datetime
    **按 UTC 解释**（与 SQLitePendingStore 同语义，测试确定性，
    不依赖本机时区）。
    """
    if isinstance(value, bool):
        raise TypeError(f"非法时间值：{value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    raise TypeError(f"非法时间值类型：{type(value).__name__}")


@dataclass(frozen=True)
class PendingCandidate:
    """候选快照的单条记录（P2.1 冻结结构）。

    frozen dataclass：外部拿到 Pending 对象后**无法**修改历史候选
    （frozen 只防字段重赋值，因此候选绝不用 dict 承载——dict 可被
    `c["name"] = …` 改动，违反快照冻结契约）。"""

    candidate_id: str
    display_order: int          # 展示序号（1..N，冻结）
    entity_id: str | None
    name: str
    city: str
    address: str | None

    def as_dict(self) -> dict:
        """序列化输出（形状与 P1.5 dict 快照完全兼容）。"""
        return {"candidate_id": self.candidate_id,
                "display_order": self.display_order,
                "entity_id": self.entity_id,
                "name": self.name,
                "city": self.city,
                "address": self.address}


def _candidate_from_dict(d: dict) -> PendingCandidate:
    """dict 快照 → PendingCandidate（store 转换入口，字段校验）。

    拷贝语义：转换即脱离调用方 dict，入参后续修改不污染快照。"""
    missing = [k for k in SNAPSHOT_FIELDS if k not in d]
    if missing:
        raise ValueError(f"候选快照缺少冻结字段：{missing}")
    return PendingCandidate(
        candidate_id=str(d["candidate_id"]),
        display_order=int(d["display_order"]),
        entity_id=d.get("entity_id"),
        name=str(d["name"]),
        city=str(d["city"]),
        address=d.get("address"))


@dataclass(frozen=True)
class PendingDisambiguation:
    """一次执行阻断（action=clarify）对应的消歧状态快照。

    frozen=True：pending_id 与候选快照一经创建不可变（规则 1 的
    契约级保障）；status 的迁移只经由 PendingStore 的原子方法完成。
    """

    pending_id: str
    original_utterance: str       # 触发消歧的原始句子
    original_intent: str          # 当时的细分意图
    original_role: str            # 候选当时的角色
    candidates: tuple[PendingCandidate, ...]  # 冻结候选快照
    created_at: str               # ISO 时间
    expires_at: str               # 过期时间
    status: str = PENDING_OPEN    # open / consumed / cancelled / expired

    def __post_init__(self) -> None:
        if self.status not in PENDING_STATUSES:
            raise ValueError(f"非法 pending status：{self.status!r}，"
                             f"允许值 {PENDING_STATUSES}")
        if not self.original_utterance.strip():
            raise ValueError("original_utterance 不得为空")
        if self.status == PENDING_OPEN and not self.candidates:
            raise ValueError("open 状态的 pending 至少需要一个候选快照")
        for c in self.candidates:
            if not isinstance(c, PendingCandidate):
                raise TypeError(
                    "候选快照必须是 PendingCandidate 冻结结构，"
                    f"收到 {type(c).__name__}（dict 快照请经 "
                    "PendingStore.create_pending 转换）")
        orders = [c.display_order for c in self.candidates]
        if orders and sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError(f"display_order 必须是 1..N 连续序号，"
                             f"实际 {sorted(orders)}")
        if len({c.candidate_id for c in self.candidates}) != \
                len(self.candidates):
            raise ValueError("candidate_id 在快照内必须唯一")

    def snapshot_candidate_ids(self) -> tuple[str, ...]:
        """快照内全部合法 candidate_id（规则 9 的判定依据）。

        行为与返回结构与 P1.5 完全一致。"""
        return tuple(c.candidate_id for c in self.candidates)

    def candidate_by_id(self, candidate_id: str) -> PendingCandidate | None:
        """按 ID 取快照内候选（frozen，不可变）；快照外 ID 返回 None。

        P2.1 起返回 PendingCandidate；需要 dict 的调用方用
        candidate_as_dict()。"""
        for c in self.candidates:
            if c.candidate_id == candidate_id:
                return c
        return None

    def candidate_as_dict(self, candidate_id: str) -> dict | None:
        """按 ID 取快照内候选的 dict 拷贝（兼容旧调用方）。"""
        c = self.candidate_by_id(candidate_id)
        return c.as_dict() if c is not None else None

    def as_dict(self) -> dict:
        return {
            "pending_id": self.pending_id,
            "original_utterance": self.original_utterance,
            "original_intent": self.original_intent,
            "original_role": self.original_role,
            "candidates": [c.as_dict() for c in self.candidates],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }


@dataclass
class _ConsumeOutcome:
    """consume_pending 的返回值（规则 7：只回传消歧结果，无行程动作）。

    candidate 契约（P2.1 冻结）：成功时为**冻结 PendingCandidate**
    （不是 dict）；需要 dict 的旧调用方使用 candidate_as_dict()。
    """
    ok: bool
    pending_id: str | None = None
    candidate: PendingCandidate | None = None  # 所选快照候选（冻结结构）
    reason: str = ""                # 失败原因（可归因）

    def candidate_as_dict(self) -> dict | None:
        """兼容垫片（P2.1 冻结）：所选候选的 dict 拷贝；失败为 None。"""
        return self.candidate.as_dict() if self.candidate is not None \
            else None


class PendingStore:
    """单 Session 消歧存储（测试桩 / 参考实现，P2 才接入业务）。

    原子性：consume/cancel/expire 的状态迁移都在锁内完成「条件迁移」
    ——先在锁内检查 status 是否满足迁移前条件，不满足直接失败返回；
    检查与写入之间不释放锁，等效于数据库条件更新
    （UPDATE ... WHERE status='open' 的受影响行数检查）。
    **不采用**先读后写的两步式实现。
    """

    def __init__(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 now=None, id_gen=None,
                 session_id: str = "") -> None:
        self._lock = threading.Lock()
        self._active: PendingDisambiguation | None = None  # 规则 8
        self._ttl = ttl_seconds
        self._now = now or datetime.now      # 测试可注入时钟
        self._id_gen = id_gen or (lambda: uuid.uuid4().hex[:12])
        # P2.3：本实例即一个 Session；消费结果记录（与 SQLite 同语义，
        # 供 PendingInterceptor 重复选择识别的内部查询路径）
        self._session_id = str(session_id)
        self._consumptions: list[dict] = []

    # -------------------------------------------------- ① create

    def create_pending(self, original_utterance: str, original_intent: str,
                       original_role: str,
                       candidates: list[dict]) -> PendingDisambiguation:
        """创建 pending。存在 active pending 时拒绝（规则 8）。"""
        now = self._now()
        with self._lock:
            if self._active is not None and \
                    self._active.status == PENDING_OPEN and \
                    self._is_expired(self._active):
                # P2.2：锁内先自动过期再查重（与 SQLite 实现同语义，
                # 阻断项①：到期永不阻塞新 Pending，规则 5+8）
                self._active = replace(self._active,
                                       status=PENDING_EXPIRED)
            if self._active is not None and \
                    self._active.status == PENDING_OPEN:
                raise ValueError(
                    f"已存在 active pending（{self._active.pending_id}），"
                    "同一 Session 同时最多一个可消费的 active pending")
            snap = tuple(_candidate_from_dict(c) for c in candidates)
            p = PendingDisambiguation(
                pending_id=self._id_gen(),
                original_utterance=original_utterance,
                original_intent=original_intent,
                original_role=original_role,
                candidates=snap,
                created_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=self._ttl)),
                status=PENDING_OPEN)
            self._active = p
            return p

    # -------------------------------------------------- ② get

    def get_active_pending(self) -> PendingDisambiguation | None:
        """取当前可消费的 active pending；无 / 已终结返回 None。

        规则 5：过期判定在此显式完成——过期的 open pending 不会被
        返回（调用方只能走 expire_pending 显式登记后重新提问）。"""
        with self._lock:
            p = self._active
            if p is None or p.status != PENDING_OPEN:
                return None
            if self._is_expired(p):
                return None
            return p

    def get_pending_by_id(self, pending_id: str) -> PendingDisambiguation | None:
        """按 ID 取（含已终结状态，供归因与测试）；不存在返回 None。"""
        with self._lock:
            p = self._active
            if p is not None and p.pending_id == pending_id:
                return p
            return None

    # -------------------------------------------------- ③ consume（原子）

    def consume_pending(self, pending_id: str,
                        candidate_id: str) -> _ConsumeOutcome:
        """原子消费：open → consumed 只能成功一次（规则 6/9）。

        单步条件迁移（锁内检查+写入不释放锁）；以下情形一律失败：
        - pending_id 不匹配 / pending 不存在；
        - status 非 open（已消费/已取消/已过期）；
        - candidate_id 不在原快照内；
        - pending 已过有效期。
        """
        with self._lock:
            p = self._active
            if p is None or p.pending_id != pending_id:
                return _ConsumeOutcome(
                    ok=False, reason="pending 不存在或 pending_id 不匹配")
            if p.status == PENDING_CONSUMED:
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason="pending 已消费过（同一 pending 只能成功消费一次）")
            if p.status != PENDING_OPEN:
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason=f"pending 状态为 {p.status}，不可消费")
            if self._is_expired(p):
                p = replace(p, status=PENDING_EXPIRED)
                self._active = p
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason="pending 已过期，不得继续执行（规则 5）")
            if candidate_id not in p.snapshot_candidate_ids():
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason="candidate_id 不在原快照内，一律拒绝（规则 9）")
            # ---- P2.3-R1 原子性：先完成全部可失败前置步骤 ----
            # （候选读取、时间获取、消费记录构造、**状态对象构造**），
            # 任何一步抛异常都不改变 pending 状态（等效 P2.2 的单事务
            # ROLLBACK：绝无「pending 已 consumed 但记录缺失」或反向
            # 「记录已写但状态未变」的半成功状态）。
            cand = p.candidate_by_id(candidate_id)
            consumed_at = _epoch(self._now())   # 时钟/epoch 异常在此抛出
            record = {
                "session_id": self._session_id,
                "pending_id": pending_id,
                "candidate_id": candidate_id,
                "consumed_at": consumed_at,
                "original_intent": p.original_intent,
                "selected_name": cand.name if cand is not None else "",
                "selected_city": cand.city if cand is not None else None,
            }
            # replace 会重跑 __post_init__（可失败），因此同样提前到
            # 前置段完成构造
            consumed = replace(p, status=PENDING_CONSUMED)
            # ---- 条件迁移：检查与写入同锁，open→consumed 单次成功 ----
            # 迁移段只剩两次纯写入，之间无任何可失败步骤
            self._consumptions.append(record)
            self._active = consumed
            return _ConsumeOutcome(
                ok=True, pending_id=pending_id, candidate=cand)

    # -------------------------------------------------- ④ cancel

    def cancel_pending(self, pending_id: str) -> _ConsumeOutcome:
        """用户取消 → cancelled，pending 清除（规则 2）。"""
        with self._lock:
            p = self._active
            if p is None or p.pending_id != pending_id:
                return _ConsumeOutcome(
                    ok=False, reason="pending 不存在或 pending_id 不匹配")
            if p.status != PENDING_OPEN:
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason=f"pending 状态为 {p.status}，不可取消")
            self._active = replace(p, status=PENDING_CANCELLED)
            return _ConsumeOutcome(ok=True, pending_id=pending_id)

    # -------------------------------------------------- ⑤ expire

    def expire_pending(self, pending_id: str) -> _ConsumeOutcome:
        """过期登记 → expired，不得继续执行（规则 5）。"""
        with self._lock:
            p = self._active
            if p is None or p.pending_id != pending_id:
                return _ConsumeOutcome(
                    ok=False, reason="pending 不存在或 pending_id 不匹配")
            if p.status != PENDING_OPEN:
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason=f"pending 状态为 {p.status}，无需过期登记")
            self._active = replace(p, status=PENDING_EXPIRED)
            return _ConsumeOutcome(ok=True, pending_id=pending_id)

    # -------------------------------------------------- 内部

    def _latest_consumption(self) -> dict | None:
        """P2.3 内部查询（不进公共契约）：本 Session 最近一次消费记录。

        与 SQLitePendingStore 同语义（七字段 dict）；无记录返回 None。
        """
        with self._lock:
            return dict(self._consumptions[-1]) \
                if self._consumptions else None

    def _is_expired(self, p: PendingDisambiguation) -> bool:
        """到期判定（P2.3 验收补丁冻结语义）：

        - expires_at 是有效期**截止时刻**：等于即过期（>=）；
        - 异常/无法解析的时间值 → **失败关闭**（保守视为已过期，
          绝不让 Pending 被继续消费），与 SQLite 实现同语义。
        """
        try:
            exp = datetime.strptime(p.expires_at, "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            return True
        # P2.2 时间契约：epoch 整数比较（naive ISO 与 naive 时钟按
        # UTC 同约定转换，偏移相消，内部自洽）
        return _epoch(self._now()) >= _epoch(exp)


def _snap(cid: str, order: int, name: str, city: str,
          entity_id: str | None = None, address: str | None = None) -> dict:
    """测试与调用方的快照构造辅助（六冻结字段齐全）。"""
    return {"candidate_id": cid, "display_order": order,
            "entity_id": entity_id, "name": name, "city": city,
            "address": address}
