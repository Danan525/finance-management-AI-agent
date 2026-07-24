"""报表中心（module 7）：从总账取数 → 三张 IFRS 报表 + 勾稽校验。

第一增量：利润表 + 资产负债表 + 勾稽（E1 资产=负债+权益、E6 科目归类完整），勾稽不过不出表。
后续增量：现金流量表（直接法+间接法交叉验证）、Excel 导出、期末结转后的 E2/E3/E4。
"""
from . import mapping, service  # noqa: F401

__all__ = ["mapping", "service"]
