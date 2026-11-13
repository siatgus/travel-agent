"""AutofetchAdapter：适配既有 autofetch.py（文档 §12）。

原则：复用旧能力，不重复实现（ADR-004）。ensure_city_data 的函数签名、
失败缓存（6h 进程内，分类型三张表）、Overpass/地理编码熔断（10min）、
Nominatim 节流、_valid_row 字段校验全部留在 autofetch 模块里，本类只做：

1. 统一入口翻译：ensure(city, data_type) → status 字典原样透传
   （status ∈ exists/fetched/empty/unresolved/no_kb/disabled/skip/failed，
   语义定义见 autofetch.ensure_city docstring，上游已经依赖这些取值）。
2. 数据来源日志：每次尝试落 data_source_log（成功率统计/失败排查用），
   日志失败绝不影响主链路。
"""

from __future__ import annotations

from ... import autofetch
from ..data_result import DataResult


class AutofetchAdapter:

    def __init__(self, module=None, db=None):
        self._mod = module or autofetch        # 测试可注入桩
        self._injected_db = db

    @property
    def DATA_TYPES(self) -> tuple:
        return tuple(getattr(self._mod, "DATA_TYPES",
                             ("attraction", "restaurant", "hotel")))

    def ensure(self, city: str, data_type: str, *,
               force: bool = False, client=None) -> dict:
        """统一补全入口（透传 autofetch.ensure_city_data 的真实返回）。"""
        db = self._injected_db
        try:
            res = self._mod.ensure_city_data(city, data_type, db=db,
                                             client=client, force=force)
        except Exception as e:                 # 模块级异常兜底成 failed
            res = {"status": "failed", "city": city, "pois": 0, "hotels": 0,
                   "message": f"{type(e).__name__}: {str(e)[:150]}"}
        self._log(city, data_type, res)
        return res

    def _log(self, city: str, data_type: str, res: dict) -> None:
        db = self._injected_db
        if db is None:
            from ... import kb as kb_mod
            db = kb_mod.get_kb()
        if db is None:
            return
        status = res.get("status") or "unknown"
        ok = status in ("fetched", "exists")
        db.record_data_log(
            data_type=data_type, city=city, source=f"autofetch:{status}",
            success=ok,
            error_code=None if ok else status.upper(),
            error_message="" if ok else (res.get("message") or ""))

    # ------------------------------------------------------------ 便捷视图
    @staticmethod
    def fetched_note(res: dict) -> str:
        """补库结果的机器可读摘要（DataResult.metadata.autofetch）。"""
        return res.get("status") or ""

    @staticmethod
    def as_metadata(res: dict) -> dict:
        return dict(res)
