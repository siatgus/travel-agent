"""SQLite PendingStore（Phase 5 P2.2）。

设计依据 docs/PHASE5_P2_PLAN.md §2 与 P2.1 验收后的 P2.2 开工要求。
内存桩 `PendingStore` 保留为测试桩与无 DB 降级路径；本实现与其
**五方法签名完全一致**（每实例绑定一个 session_id，共享同一 SQLite
库文件，session_id 隔离在 SQL 条件层强制，不靠应用层判断）。

P2.2 八条验收要求逐条落点：
1. 时间契约：created_at / expires_at / consumed_at / revalidated_at
   在库内**统一为 UTC Unix epoch 整数**，所有过期判定与比较只用
   epoch 整数（`expires_at > ?` / `expires_at <= ?`），绝不做字符串
   时间比较。PendingDisambiguation 数据类的 created_at/expires_at
   仍是 ISO 字符串（v3.2.1 冻结契约），由 epoch 以 UTC 派生，仅
   用于展示与序列化，不参与任何比较。注入时钟返回 naive datetime
   时按 UTC 解释（测试确定性，不依赖本机时区）。
2. 创建事务：BEGIN IMMEDIATE → 自动过期该 Session 已到期的 open
   记录 → INSERT → COMMIT（阻断项①：到期永不阻塞新 Pending）。
3. 部分唯一索引 ux_pending_open(session_id) WHERE status='open'：
   单 Session 最多一个 open Pending，数据库级强制；并发创建由
   IntegrityError 兜底（规则 8）。
4. 消费原子性：候选校验、条件更新（受影响行数=1 才算成功）、
   消费记录写入处于同一 BEGIN IMMEDIATE 事务内。
5. 并发消费：BEGIN IMMEDIATE 写锁串行化，两线程同时消费同一
   Pending 恰好一个成功（与 P1.5 内存桩 Barrier 测试同语义）。
6. 自动过期：A 到期但无人显式 expire，create B 在同一事务内先
   把 A 条件更新为 expired 再插入 B。
7. Session 隔离：所有读写都带 session_id 条件；B 用 A 的
   pending_id 消费 → 查不到行 → 拒绝，A 的状态不受影响。
8. 内存桩兼容：内存桩 create_pending 同步补「锁内先自动过期再
   查重」，两实现状态语义一致（tests/test_pending_sqlite.py
   双实现生命周期对拍锁定）。

半成功防线：consume_pending 中消费记录 INSERT 失败 → 整体
ROLLBACK，pending 回到 open；**绝不出现 pending 已 consumed 而
consumption 记录缺失的状态**。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from travel_agent import paths
from travel_agent.pending import (DEFAULT_TTL_SECONDS, PENDING_OPEN,
                                  PENDING_CONSUMED,
                                  PendingDisambiguation,
                                  REVALIDATE_STATUSES,
                                  _ConsumeOutcome, _candidate_from_dict,
                                  _epoch, in_reselect_window)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_disambiguation (
    pending_id         TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    original_utterance TEXT NOT NULL,
    original_intent    TEXT NOT NULL,
    original_role      TEXT NOT NULL,
    candidates_json    TEXT NOT NULL,     -- 冻结候选 tuple 的 JSON
    created_at         INTEGER NOT NULL,  -- UTC Unix epoch（验收要求 1）
    expires_at         INTEGER NOT NULL,  -- UTC Unix epoch
    status             TEXT NOT NULL      -- open/consumed/cancelled/expired
);
-- 规则 8：单 Session 单 active——数据库级强制，不靠应用层判断
CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_open
    ON pending_disambiguation(session_id) WHERE status = 'open';
-- 消费结果记录（P2.3 重复选择识别的数据源；pending_id 唯一——
-- 同一 pending 只能成功消费一次，同时充当半成功回滚的失败诱因）
CREATE TABLE IF NOT EXISTS pending_consumption (
    pending_id              TEXT PRIMARY KEY,
    session_id              TEXT NOT NULL,
    candidate_id            TEXT NOT NULL,
    consumed_at             INTEGER NOT NULL,  -- UTC Unix epoch
    original_intent         TEXT NOT NULL,
    selected_name           TEXT NOT NULL,
    selected_city           TEXT,
    revalidate_status       TEXT,              -- P2.5 重验证回填
    revalidate_probe_status TEXT,
    revalidated_at          INTEGER
);
"""


def _iso_utc(epoch: int) -> str:
    """epoch → ISO 字符串（仅展示/序列化用途，绝不参与比较）。"""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S")


class SQLitePendingStore:
    """绑定单一 session_id 的 PendingStore SQLite 实现。

    与内存桩 PendingStore 的五方法签名与返回契约一致：
    - create_pending 存在 active 时抛 ValueError（规则 8）；
    - consume/cancel/expire 返回 _ConsumeOutcome，失败 reason 可归因；
    - get_active_pending 对已到期 open 记录返回 None（规则 5），
      与内存桩一致不做读取期隐式迁移（显式迁移只发生在 consume
      与 create 的自动过期路径）。
    """

    def __init__(self, session_id: str, *,
                 db_path=None,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 now=None, id_gen=None) -> None:
        if not str(session_id).strip():
            raise ValueError("session_id 不得为空（多用户库必须隔离）")
        self._session_id = str(session_id)
        self._db = Path(db_path) if db_path is not None else paths.app_db()
        self._ttl = int(ttl_seconds)
        self._now = now                          # 测试可注入时钟
        self._id_gen = id_gen or (lambda: uuid.uuid4().hex[:12])
        self._init_schema()

    # -------------------------------------------------- 连接与 schema

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None：显式管理 BEGIN/COMMIT/ROLLBACK
        con = sqlite3.connect(str(self._db), timeout=10.0,
                              isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    def _init_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(_SCHEMA)
        finally:
            con.close()

    def _now_epoch(self) -> int:
        return _epoch(self._now()) if self._now is not None \
            else int(time.time())

    def _is_expired(self, p: PendingDisambiguation) -> bool:
        """到期判定（P2.3 验收补丁冻结语义，与内存桩同语义）：

        - expires_at 是有效期**截止时刻**：等于即过期（>=）；
        - 异常/无法解析的时间值 → **失败关闭**（保守视为已过期，
          绝不让 Pending 被继续消费）。
        """
        try:
            exp = datetime.strptime(
                p.expires_at, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True
        return self._now_epoch() >= int(exp.timestamp())

    def _latest_consumption(self) -> dict | None:
        """P2.3 内部查询（不进公共契约）：本 Session 最近一次消费记录。

        十字段 dict 与内存桩同语义（session_id/pending_id/candidate_id/
        consumed_at/original_intent/selected_name/selected_city +
        P2.5b D3 重验证三列 revalidate_status/revalidate_probe_status/
        revalidated_at，未回填时为 None）；无记录返回 None。
        绝不跨 Session 读取。
        """
        con = self._connect()
        try:
            row = con.execute(
                "SELECT pending_id, session_id, candidate_id,"
                " consumed_at, original_intent, selected_name,"
                " selected_city, revalidate_status,"
                " revalidate_probe_status, revalidated_at"
                " FROM pending_consumption"
                " WHERE session_id = ?"
                " ORDER BY consumed_at DESC, rowid DESC LIMIT 1",
                (self._session_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        d = dict(row)
        d["consumed_at"] = int(d["consumed_at"])
        if d.get("revalidated_at") is not None:
            d["revalidated_at"] = int(d["revalidated_at"])
        return d

    def _latest_pending_id(self) -> str | None:
        """P2.4a 内部查询（不进公共契约，§3.2 解析顺序第 3 步）。

        本 sid 最新一条 pending 的 ID（**任意状态**：open/expired/
        consumed/cancelled 都算——终态记录仅作 evaluate 解析线索，
        绝不视为有效待处理对象）。严格按本 store 绑定的 session_id
        查询，绝不接受外部 sid。同时间戳以 rowid（入库序）作确定性
        次序依据（r5 冻结）。"""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT pending_id FROM pending_disambiguation"
                " WHERE session_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (self._session_id,)).fetchone()
        finally:
            con.close()
        return row["pending_id"] if row is not None else None

    def _has_runtime_retention_state(self, now=None) -> bool:
        """P2.4a 内部只读判定（§4.3，Runtime 保留态；不进公共契约）。

        True = 该 runtime 仍承载必须由它承担的业务状态：
        ① 存在 status='open' 的 Pending（**原始状态，无时间过滤**，
          到期未登记的 open 同样命中）；
        ② 最近一条消费记录仍在重复选择窗口内（与拦截器共用
          in_reselect_window 同一表达式，边界等值算窗口内）。
        只读零副作用；now 可注入（manager 时钟）。"""
        con = self._connect()
        try:
            open_row = con.execute(
                "SELECT 1 FROM pending_disambiguation"
                " WHERE session_id = ? AND status = 'open' LIMIT 1",
                (self._session_id,)).fetchone()
            if open_row is not None:
                return True
            row = con.execute(
                "SELECT consumed_at FROM pending_consumption"
                " WHERE session_id = ?"
                " ORDER BY consumed_at DESC, rowid DESC LIMIT 1",
                (self._session_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            return False
        n = _epoch(now) if now is not None else self._now_epoch()
        return in_reselect_window(int(row["consumed_at"]), n)

    # -------------------------------------------------- 行重建

    def _pending_from_row(self, row) -> PendingDisambiguation:
        cands = tuple(_candidate_from_dict(d)
                      for d in json.loads(row["candidates_json"]))

        def _disp(col) -> str:
            """时间展示值：正常行转 ISO；异常值原样保留（判定走
            _is_expired 失败关闭，展示值绝不参与比较）。"""
            try:
                return _iso_utc(int(row[col]))
            except (TypeError, ValueError):
                return str(row[col])

        return PendingDisambiguation(
            pending_id=row["pending_id"],
            original_utterance=row["original_utterance"],
            original_intent=row["original_intent"],
            original_role=row["original_role"],
            candidates=cands,
            created_at=_disp("created_at"),
            expires_at=_disp("expires_at"),
            status=row["status"])

    @staticmethod
    def _rollback(con: sqlite3.Connection) -> None:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # -------------------------------------------------- ① create

    def create_pending(self, original_utterance: str, original_intent: str,
                       original_role: str,
                       candidates: list[dict]) -> PendingDisambiguation:
        """创建 pending（同内存桩签名；session_id 取构造绑定值）。

        BEGIN IMMEDIATE 单事务：自动过期该 Session 已到期的 open
        记录 → INSERT → COMMIT。撞部分唯一索引 → IntegrityError →
        按规则 8 抛 ValueError（与内存桩同异常契约）。
        """
        snap = tuple(_candidate_from_dict(c) for c in candidates)
        now_e = self._now_epoch()
        # 先构造（复用 PendingDisambiguation 全部校验），再入事务
        p = PendingDisambiguation(
            pending_id=self._id_gen(),
            original_utterance=original_utterance,
            original_intent=original_intent,
            original_role=original_role,
            candidates=snap,
            created_at=_iso_utc(now_e),
            expires_at=_iso_utc(now_e + self._ttl),
            status=PENDING_OPEN)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            # 阻断项①：自动过期——到期永不阻塞新 Pending
            con.execute(
                "UPDATE pending_disambiguation SET status = 'expired'"
                " WHERE session_id = ? AND status = 'open'"
                "   AND expires_at <= ?",
                (self._session_id, now_e))
            # 失败关闭兜底：异常时间值（SQL 不可比）不满足上面的
            # 条件更新，这里用 Python 侧 _is_expired 再兜一道
            stale = con.execute(
                "SELECT * FROM pending_disambiguation"
                " WHERE session_id = ? AND status = 'open'",
                (self._session_id,)).fetchall()
            for srow in stale:
                if self._is_expired(self._pending_from_row(srow)):
                    con.execute(
                        "UPDATE pending_disambiguation"
                        " SET status = 'expired' WHERE pending_id = ?",
                        (srow["pending_id"],))
            try:
                con.execute(
                    "INSERT INTO pending_disambiguation (pending_id,"
                    " session_id, original_utterance, original_intent,"
                    " original_role, candidates_json, created_at,"
                    " expires_at, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p.pending_id, self._session_id, p.original_utterance,
                     p.original_intent, p.original_role,
                     json.dumps([c.as_dict() for c in p.candidates]),
                     now_e, now_e + self._ttl, PENDING_OPEN))
            except sqlite3.IntegrityError as e:
                self._rollback(con)
                raise ValueError(
                    "已存在 active pending，同一 Session 同时最多一个"
                    "可消费的 active pending（规则 8，部分唯一索引兜底）"
                ) from e
            con.execute("COMMIT")
        except BaseException:
            self._rollback(con)
            raise
        finally:
            con.close()
        return p

    # -------------------------------------------------- ② get

    def get_active_pending(self) -> PendingDisambiguation | None:
        """取本 Session 当前可消费的 active pending（规则 5/8）。

        过期判定用 epoch 整数比较 `expires_at > ?`；到期的 open
        记录不返回（也不做读取期隐式迁移，与内存桩一致）。
        """
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM pending_disambiguation"
                " WHERE session_id = ? AND status = 'open'"
                "   AND expires_at > ?"
                " ORDER BY created_at DESC LIMIT 1",
                (self._session_id, self._now_epoch())).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        p = self._pending_from_row(row)
        # 失败关闭兜底：异常时间值（TEXT 等 SQL 不可比数据）不被
        # SQL 过滤时，Python 侧 _is_expired 再拦一道（绝不返回可
        # 消费的过期 pending）
        if self._is_expired(p):
            return None
        return p

    def get_pending_by_id(self, pending_id: str) \
            -> PendingDisambiguation | None:
        """按 ID 取**本 Session** 的记录（含已终结状态，供归因）。

        session_id 条件强制：其他 Session 的 pending 一律不可见。
        """
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM pending_disambiguation"
                " WHERE pending_id = ? AND session_id = ?",
                (pending_id, self._session_id)).fetchone()
        finally:
            con.close()
        return self._pending_from_row(row) if row is not None else None

    # -------------------------------------------------- ③ consume（原子）

    def consume_pending(self, pending_id: str,
                        candidate_id: str) -> _ConsumeOutcome:
        """原子消费：单事务完成候选校验 + 条件更新 + 消费记录写入。

        失败契约与内存桩一致；消费记录 INSERT 失败 → 整体 ROLLBACK
        （pending 保持 open，绝无半成功状态）。
        """
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT status, candidates_json, expires_at,"
                " original_intent FROM pending_disambiguation"
                " WHERE pending_id = ? AND session_id = ?",
                (pending_id, self._session_id)).fetchone()
            if row is None:
                self._rollback(con)
                return _ConsumeOutcome(
                    ok=False,
                    reason="pending 不存在或 pending_id 不匹配")
            status = row["status"]
            if status == PENDING_CONSUMED:
                self._rollback(con)
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason="pending 已消费过（同一 pending 只能成功"
                           "消费一次）")
            if status != PENDING_OPEN:
                self._rollback(con)
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason=f"pending 状态为 {status}，不可消费")
            now_e = self._now_epoch()
            try:
                expired = int(row["expires_at"]) <= now_e
            except (TypeError, ValueError):
                expired = True   # 异常时间值 → 失败关闭（不得消费）
            if expired:
                # 到期显式登记持久化（与内存桩 consume 过期分支同语义）
                con.execute(
                    "UPDATE pending_disambiguation SET status = 'expired'"
                    " WHERE pending_id = ? AND session_id = ?"
                    "   AND status = 'open'",
                    (pending_id, self._session_id))
                con.execute("COMMIT")
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason="pending 已过期，不得继续执行（规则 5）")
            snap = tuple(_candidate_from_dict(d)
                         for d in json.loads(row["candidates_json"]))
            if candidate_id not in tuple(c.candidate_id for c in snap):
                self._rollback(con)
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason="candidate_id 不在原快照内，一律拒绝（规则 9）")
            chosen = next(c for c in snap if c.candidate_id == candidate_id)
            # 条件更新：受影响行数 = 1 才算成功（并发语义核心）
            cur = con.execute(
                "UPDATE pending_disambiguation SET status = 'consumed'"
                " WHERE pending_id = ? AND session_id = ?"
                "   AND status = 'open' AND expires_at > ?",
                (pending_id, self._session_id, now_e))
            if cur.rowcount != 1:
                self._rollback(con)
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason="pending 状态已变化，本次未消费")
            # 消费记录写入与状态迁移同事务：失败整体回滚
            try:
                con.execute(
                    "INSERT INTO pending_consumption (pending_id,"
                    " session_id, candidate_id, consumed_at,"
                    " original_intent, selected_name, selected_city)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pending_id, self._session_id, candidate_id, now_e,
                     row["original_intent"], chosen.name, chosen.city))
            except sqlite3.Error as e:
                self._rollback(con)
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason=f"消费记录写入失败，事务已回滚"
                           f"（pending 保持 open）：{e}")
            con.execute("COMMIT")
            return _ConsumeOutcome(ok=True, pending_id=pending_id,
                                   candidate=chosen)
        finally:
            con.close()

    # -------------------------------------------------- ③' revalidate
    #      （P2.5b D3：重验证回填，六契约的 SQLite 实现——§10.1
    #        修订版条件 UPDATE，WHERE 含 candidate_id 候选归属
    #        数据库层验证；无 DDL，三列自 P2.2 冻结建表预留）

    def record_revalidation(self, session_id: str, pending_id: str,
                            candidate_id: str, revalidate_status: str,
                            revalidate_probe_status: str | None) -> bool:
        """重验证结果回填（§10.1 D3 修订六契约）。

        - 单条**条件 UPDATE**（绝不 INSERT 新消费记录）：

            UPDATE pending_consumption
               SET revalidate_status=?, revalidate_probe_status=?,
                   revalidated_at=?
             WHERE pending_id=? AND session_id=? AND candidate_id=?

        - rowcount == 1 → **先校验后提交**：UPDATE 后先检查 rowcount，
          恰等于 1 才 commit 并返回 True；rowcount == 0（跨 session /
          跨 pending / 候选不符 / 无消费记录）或 **rowcount > 1（多行
          命中，唯一命中校验失败）** → **rollback**（绝不把多行修改
          提交进库）并返回 False，绝不当作成功；
        - revalidate_status 非四枚举合法值 → False（契约 6）；
        - **任何异常**（含非 SQLite 异常：时钟故障、连接关闭、任意
          RuntimeError 等，契约 4：回填异常不抛出到业务链路，不改已
          完成的消费结果）→ rollback（在事务防护内）+ False；
        - revalidated_at = INTEGER UTC epoch（self._now_epoch()，
          与 consumed_at 同源时钟，契约 5 时间语义逐位一致）。
        """
        if revalidate_status not in REVALIDATE_STATUSES:
            return False
        con = None
        try:
            con = self._connect()
            # 显式事务（与 _terminal_transition 同范式——连接为
            # isolation_level=None 显式管理模式，裸 UPDATE 会即时
            # 自动提交，rollback 无法撤销；必须 BEGIN 包住 UPDATE
            # 才能实现「先校验 rowcount 后提交」的原子性）
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE pending_consumption"
                "   SET revalidate_status = ?,"
                "       revalidate_probe_status = ?,"
                "       revalidated_at = ?"
                " WHERE pending_id = ? AND session_id = ?"
                "   AND candidate_id = ?",
                (revalidate_status, revalidate_probe_status,
                 self._now_epoch(), pending_id, str(session_id),
                 candidate_id))
            # RV-02 修正（验收 2026-11-19）：先校验后提交——恰更新
            # 一行才 COMMIT；0 行（未命中）或多行命中（唯一性被破坏）
            # 一律 ROLLBACK，绝不把错误的多行修改落库后再返回 False
            if cur.rowcount != 1:
                self._rollback(con)
                return False
            con.execute("COMMIT")
            return True
        except Exception:
            # RV-03 修正：不限于 sqlite3.Error——_now_epoch() 时钟
            # 故障、connect/close 失败等任意异常一律不向业务层传播
            # （契约 4）；事务若已开启则先回滚再返回 False
            if con is not None:
                self._rollback(con)
            return False
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

    # -------------------------------------------------- ④⑤ cancel / expire

    def cancel_pending(self, pending_id: str) -> _ConsumeOutcome:
        """用户取消 → cancelled（规则 2）。条件更新，行数=1 才成功。"""
        return self._terminal_transition(
            pending_id, "cancelled", "不可取消")

    def expire_pending(self, pending_id: str) -> _ConsumeOutcome:
        """过期登记 → expired（规则 5）。条件更新，行数=1 才成功。"""
        return self._terminal_transition(
            pending_id, "expired", "无需过期登记")

    def _terminal_transition(self, pending_id: str, target: str,
                             mismatch_hint: str) -> _ConsumeOutcome:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT status FROM pending_disambiguation"
                " WHERE pending_id = ? AND session_id = ?",
                (pending_id, self._session_id)).fetchone()
            if row is None:
                self._rollback(con)
                return _ConsumeOutcome(
                    ok=False,
                    reason="pending 不存在或 pending_id 不匹配")
            if row["status"] != PENDING_OPEN:
                self._rollback(con)
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason=f"pending 状态为 {row['status']}，"
                           f"{mismatch_hint}")
            cur = con.execute(
                "UPDATE pending_disambiguation SET status = ?"
                " WHERE pending_id = ? AND session_id = ?"
                "   AND status = 'open'",
                (target, pending_id, self._session_id))
            if cur.rowcount != 1:
                self._rollback(con)
                return _ConsumeOutcome(
                    ok=False, pending_id=pending_id,
                    reason="pending 状态已变化，本次未迁移")
            con.execute("COMMIT")
            return _ConsumeOutcome(ok=True, pending_id=pending_id)
        finally:
            con.close()
