"""审计日志：面向合规留存的追加式日志（JSON Lines，按天轮转）。

**为什么需要它**：面向公众提供生成式 AI 服务，按《生成式人工智能服务管理
暂行办法》与《具有舆论属性或社会动员能力的互联网信息服务安全评估规定》，
需要留存网络日志不少于六个月，并能在监管部门依法查询时提供。原先所有记录
都用 print 打到 stdout，进程重启即丢，日志随容器销毁而消失——技术上无法满足
留存要求。

设计取舍：
- **追加写文件，不落业务库**：审计写量与业务读量解耦，日志盘坏了不影响业务；
  也避免把敏感记录塞进给前端查询的表里。
- **按天分文件**：`data/logs/audit-YYYY-MM-DD.jsonl`。单个文件坏掉只丢一天，
  归档/上报也按天切分。
- **JSON Lines**：一行一条，可直接 grep / jq / 导入 ELK，无需解析器。
- **保留 N 天自动清理**（默认 180 天，留出 6 个月合规要求的余量）：只在首次
  写入时扫描一次目录，之后每天最多扫一次，不在热路径上做 IO 目录遍历。
- **失败静默降级**：日志写失败绝不能拖垮业务请求，只吞掉并计数。

环境变量：
    TRAVEL_LOG_DIR      日志目录（默认 data/logs）
    TRAVEL_LOG_RETAIN   保留天数（默认 180）
    TRAVEL_AUDIT_LOG    0=关闭审计写入（测试用），默认开启

用法：
    from . import auditlog
    auditlog.write("chat", uid=uid, sid=sid, ip=ip, mode=reply.mode,
                   elapsed_ms=1234, has_plan=True)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

_LOCK = threading.Lock()
_LAST_CLEANUP = None          # 上次清理的日期（date 对象），按天节流
_WRITE_ERRORS = 0             # 累计写失败次数，便于后台健康检查读取


def _base_dir() -> Path:
    d = os.environ.get("TRAVEL_LOG_DIR", "").strip()
    if d:
        return Path(d)
    # 与其它数据文件保持一致：项目根下的 data/logs
    return Path(__file__).resolve().parents[2] / "data" / "logs"


def _retain_days() -> int:
    raw = os.environ.get("TRAVEL_LOG_RETAIN", "").strip()
    try:
        n = int(raw)
        return n if n >= 1 else 180
    except (TypeError, ValueError):
        return 180


def enabled() -> bool:
    return os.environ.get("TRAVEL_AUDIT_LOG", "1").strip() != "0"


def _cleanup_once_per_day(base: Path) -> None:
    """删除超过保留期的日志文件。每天最多执行一次。"""
    global _LAST_CLEANUP
    today = datetime.now().date()
    if _LAST_CLEANUP == today:
        return
    _LAST_CLEANUP = today
    cutoff = today - timedelta(days=_retain_days())
    try:
        for p in base.glob("audit-*.jsonl"):
            stamp = p.stem.replace("audit-", "")
            try:
                day = datetime.strptime(stamp, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def write(event: str, **fields) -> None:
    """追加一条审计记录。永不抛异常。"""
    global _WRITE_ERRORS
    if not enabled():
        return
    rec = {"ts": datetime.now().isoformat(timespec="milliseconds"),
           "event": event}
    for k, v in fields.items():
        if v is None or v == "":
            continue
        # 长文本截断：审计要频次与溯源线索，不需要全文；
        # 全文留在业务库里，审计文件保持小、可长期留存。
        if isinstance(v, str) and len(v) > 200:
            v = v[:200] + "…"
        rec[k] = v
    try:
        base = _base_dir()
        base.mkdir(parents=True, exist_ok=True)
        _cleanup_once_per_day(base)
        path = base / f"audit-{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        _WRITE_ERRORS += 1


def stats() -> dict:
    """后台健康检查用：日志目录、保留天数、累计写失败次数。"""
    base = _base_dir()
    files = sorted(base.glob("audit-*.jsonl")) if base.exists() else []
    return {"dir": str(base), "retain_days": _retain_days(),
            "enabled": enabled(), "files": len(files),
            "latest": files[-1].name if files else "",
            "write_errors": _WRITE_ERRORS}
