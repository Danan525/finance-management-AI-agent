"""总账引擎（module 6）：复式记账内核 + 科目表 + 应计过账 + 试算平衡。

人工审核通过后由人显式调用 service.post_invoice 入账——**AI 绝不自动过账**。
资金结算两段式、期末结转、报表为后续增量（见 计划/实施进度与后续计划_V1.md §4）。
"""
from .engine import JournalEntry, JournalLine, Ledger  # noqa: F401
from . import accounts, posting, store, service  # noqa: F401

__all__ = ["JournalEntry", "JournalLine", "Ledger",
           "accounts", "posting", "store", "service"]
