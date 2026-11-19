"""PendingRuntimeManager：pending 消费通道的 Runtime 装配与生命周期
管理（Phase 5 P2.4a）。

设计依据 docs/PHASE5_P24_PLAN.md r5 §4（隔离/淘汰）与 §4.5（生命周期
同步）。职责：

- **按 sid 隔离装配**：每个 sid 独立持有 store + PendingInterceptor
  （memory/sqlite 均为独立实例，禁止共享单例 / 空 sid 规避）；
- **lease 引用计数**：获取 runtime 即授 lease（manager 锁内
  active_ops += 1），业务执行不持 manager 锁，finally 归还——
  修复「已取得引用、尚未进入 store 锁」的生命周期竞态（§4.5）；
- **LRU 淘汰双闸**（§4.2/§4.3）：闸 1 active_ops > 0 → 跳过；
  闸 2 memory runtime 处于业务状态保留态（含到期未登记 open、
  重复选择窗口内消费记录）→ 跳过；SQLite runtime 可随时淘汰
  （状态持久化，淘汰不删除任何 DB 记录）；全部保留态时允许暂时
  超上限并记 shadow 警告（宁可靠内多缓存，不丢会话状态）；
- **统一锁顺序：manager 锁 → store 锁（→ DB 事务）**，全程单向
  绝不逆向；lease 归只发生在全部 store 锁释放之后（批准条件冻结）。

本模块属装配层适配：不修改 P2.3 evaluate/act 业务逻辑，不创建
Pending（生产者仍冻结），不接 Handler。
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

from travel_agent.pending import PendingStore, _epoch
from travel_agent.pending_interceptor import PendingInterceptor

# SQLite store 惰性导入：channel=0 时绝不触发 paths/app_db 探测
_SQLiteStore = None


def _sqlite_store_cls():
    global _SQLiteStore
    if _SQLiteStore is None:
        from travel_agent.pending_store_sqlite import SQLitePendingStore
        _SQLiteStore = SQLitePendingStore
    return _SQLiteStore


def default_store_factory(sid: str):
    """按 TRAVEL_PENDING_STORE 装配该 sid 的独立 store 实例。

    memory（默认）：进程内独立实例；sqlite：绑定 paths.app_db() 的
    独立 SQLitePendingStore（session 隔离由 P2.2 SQL 条件保证）。
    """
    kind = os.environ.get("TRAVEL_PENDING_STORE", "memory").strip().lower()
    if kind == "sqlite":
        return _sqlite_store_cls()(sid)
    if kind in ("", "memory"):
        return PendingStore(session_id=str(sid))
    raise ValueError(f"非法 TRAVEL_PENDING_STORE：{kind!r}（memory/sqlite）")


class PendingRuntime:
    """一个 sid 的完整 pending 运行时（store + 拦截器 + lease 状态）。"""

    __slots__ = ("sid", "store", "interceptor", "active_ops", "last_used")

    def __init__(self, sid: str, store, interceptor: PendingInterceptor,
                 last_used) -> None:
        self.sid = str(sid)
        self.store = store
        self.interceptor = interceptor
        self.active_ops = 0        # lease 计数（manager 锁内读写）
        self.last_used = _epoch(last_used)


class PendingRuntimeManager:
    """Runtime 装配、lease 生命周期与 LRU 淘汰（§4/§4.5 冻结实现）。"""

    def __init__(self, store_factory=None, *, max_runtimes: int | None = None,
                 now=None) -> None:
        self._factory = store_factory or default_store_factory
        if max_runtimes is None:
            try:
                max_runtimes = int(
                    os.environ.get("TRAVEL_PENDING_RUNTIME_MAX", "256"))
            except ValueError:
                max_runtimes = 256
        self._max = max(1, int(max_runtimes))
        self._now = now or time.time          # 可注入时钟（测试确定性）
        # manager 锁：保护 dict + active_ops；锁内无慢操作
        self._lock = threading.Lock()
        self._runtimes: OrderedDict[str, PendingRuntime] = OrderedDict()
        self.warnings: list[str] = []         # shadow 警告（超限不淘汰等）

    # -------------------------------------------------- lease 生命周期

    def acquire(self, sid: str) -> PendingRuntime:
        """取得该 sid 的 runtime 并授 lease（§4.5.1）。

        空 sid 一律拒绝（评审冻结：不得以空字符串/默认 sid 规避
        session 隔离）。同 sid 双检只建一个实例。
        """
        sid = str(sid)
        if not sid.strip():
            raise ValueError("pending runtime 需要 session_id，禁止空 sid")
        with self._lock:
            rt = self._runtimes.get(sid)
            if rt is None:
                store = self._factory(sid)
                rt = PendingRuntime(sid, store,
                                    PendingInterceptor(store),
                                    last_used=self._now())
                self._runtimes[sid] = rt
            rt.active_ops += 1
            rt.last_used = _epoch(self._now())   # datetime/数值均可
            self._runtimes.move_to_end(sid)
            if len(self._runtimes) > self._max:
                self._evict_locked()
            return rt

    def release(self, runtime: PendingRuntime) -> None:
        """归还 lease。

        批准条件冻结时序：调用方必须在**全部 store 锁释放之后**才
        调用本方法（gate 的 finally 保证）；本方法只取 manager 锁，
        绝不在持 store 锁时被调用。"""
        with self._lock:
            runtime.active_ops = max(0, runtime.active_ops - 1)
            # 归还后容量可能释放：超限即触发淘汰（同一 manager 锁内）
            if len(self._runtimes) > self._max:
                self._evict_locked()

    # -------------------------------------------------- 淘汰（双闸）

    def evict(self) -> None:
        """主动触发一轮 LRU 淘汰（acquire 超限时亦自动触发）。"""
        with self._lock:
            self._evict_locked()

    def _evict_locked(self) -> None:
        """淘汰扫描（调用方必须已持 manager 锁；§4.5.2 双闸）。"""
        while len(self._runtimes) > self._max:
            evicted = False
            now_e = _epoch(self._now())
            for sid, rt in list(self._runtimes.items()):
                # 闸 1：lease 活跃（含「已取得引用尚未进 store 锁」）
                # → 跳过，绝不淘汰持引用的 runtime
                if rt.active_ops > 0:
                    continue
                # 闸 2：memory runtime 处于业务状态保留态 → 跳过；
                # SQLite runtime 状态持久化，随时可淘汰（不删 DB 记录）
                if self._is_memory_store(rt.store) and \
                        rt.store._has_runtime_retention_state(now_e):
                    continue
                # 可淘汰：同 manager 临界区内移除（连接关闭——SQLite
                # 为逐操作连接，无长持连接，丢引用即释放；见 §4.5.3）
                del self._runtimes[sid]
                evicted = True
                break
            if not evicted:
                # 全部处于保留/lease 态：允许暂时超上限，记 shadow 警告
                self.warnings.append(
                    f"runtime overflow kept: {len(self._runtimes)} > "
                    f"{self._max}（全部 lease 活跃或业务状态保留）")
                return

    # -------------------------------------------------- 观测（测试/排查）

    def snapshot(self) -> dict:
        """只读观测点：sid → (active_ops, store 实例 id)。"""
        with self._lock:
            return {sid: (rt.active_ops, id(rt.store))
                    for sid, rt in self._runtimes.items()}

    def runtime(self, sid: str) -> PendingRuntime | None:
        with self._lock:
            return self._runtimes.get(str(sid))

    # -------------------------------------------------- 内部

    @staticmethod
    def _is_memory_store(store) -> bool:
        return not isinstance(store, _sqlite_store_cls())
