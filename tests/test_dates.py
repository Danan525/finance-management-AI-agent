"""日期解析宽容度回归：中文/序数词/时间尾/两位年/短横/点分 都能识别；无歧义写法仍稳。"""
import unittest

from extraction.parse import dates as d


class DateParseTest(unittest.TestCase):
    def _iso(self, s):
        return d.normalize_date(s)[0]

    def test_new_formats_recognized(self):
        cases = {
            "26-06-2025": "2025-06-26",      # dd-mm-yyyy 短横
            "06-26-2025": "2025-06-26",      # mm-dd-yyyy 短横（美式）
            "06/26/25": "2025-06-26",        # 两位年（不被 %Y 误吞成 0025）
            "1st March 2026": "2026-03-01",  # 序数词在日
            "March 1st, 2026": "2026-03-01",
            "2025年6月26日": "2025-06-26",     # 中文
            "2025.06.26": "2025-06-26",      # 点分 ISO
            "2026-06-02T10:00:00": "2026-06-02",  # ISO 带时间
            "22-Apr-26": "2026-04-22",       # 月名 + 两位年
        }
        for s, iso in cases.items():
            self.assertEqual(self._iso(s), iso, f"{s!r} 应解析为 {iso}")

    def test_existing_formats_unchanged(self):
        for s, iso in {"22-Apr-2026": "2026-04-22", "06/26/2025": "2025-06-26",
                       "2026-06-01": "2026-06-01", "28 December 2025": "2025-12-28",
                       "March 1,2026": "2026-03-01"}.items():
            self.assertEqual(self._iso(s), iso)

    def test_two_digit_year_not_misread_as_year_25(self):
        # 关键守卫：%Y 会吞两位年 → 年份<1000 跳过，让位给 %y
        self.assertEqual(self._iso("06/26/25"), "2025-06-26")

    def test_non_date_still_fails(self):
        for s in ["Q2 2026", "hello world", ""]:
            iso, need = d.normalize_date(s)
            self.assertIsNone(iso)

    def test_ambiguous_day_month_flagged(self):
        # 日、月都 ≤12 且不等 → 歧义：默认(monthfirst)解读但**标待复核**
        self.assertEqual(d.normalize_date("05/06/2026"), ("2026-05-06", True))
        self.assertEqual(d.normalize_date("12/11/2026"), ("2026-12-11", True))

    def test_unambiguous_not_flagged(self):
        # 有一位 >12 → 无歧义、不标；ISO 也不受影响
        self.assertEqual(d.normalize_date("13/06/2026"), ("2026-06-13", False))
        self.assertEqual(d.normalize_date("06/26/2025"), ("2025-06-26", False))
        self.assertEqual(d.normalize_date("2026-06-05"), ("2026-06-05", False))

    def test_dayfirst_config(self):
        from core import config
        old = config.DATE_DAYFIRST
        try:
            config.DATE_DAYFIRST = True
            self.assertEqual(d.normalize_date("05/06/2026"), ("2026-06-05", True))
        finally:
            config.DATE_DAYFIRST = old


if __name__ == "__main__":
    unittest.main()
