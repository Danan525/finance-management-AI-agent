"""通用（版式无关）字段提取测试。"""
import unittest
from decimal import Decimal

from extraction.extract.pdf_text import pdfdoc_from_word_tuples
from extraction.parse import generic


def _lines(words):
    # words: (x0, y0, x1, y1, text)
    return pdfdoc_from_word_tuples(words).lines


class TestGenericExtract(unittest.TestCase):
    def test_grid_label_above_value(self):
        """标签一行、值在正下方按 x 列对齐（网格版式）。"""
        words = [
            (59, 10, 110, 18, "INVOICE"), (112, 10, 140, 18, "NO."),
            (221, 10, 250, 18, "ISSUE"), (252, 10, 300, 18, "DATE"),
            (59, 30, 140, 38, "HCG-2025-1002"),
            (221, 30, 245, 38, "28"), (247, 30, 300, 38, "December"), (302, 30, 330, 38, "2025"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["invoice_no"], "HCG-2025-1002")
        self.assertEqual(g["invoice_date"], "28 December 2025")

    def test_inline_currency(self):
        words = [(43, 10, 80, 18, "Currency:"), (85, 10, 110, 18, "GBP")]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["currency"], "GBP")

    def test_total_picks_max_over_lineitem_header(self):
        """明细表 'TOTAL' 列头不应冒充总额；总额取最大候选（=真 TOTAL DUE）。"""
        words = [
            (486, 10, 520, 18, "TOTAL"),                 # 明细表列头
            (490, 30, 540, 38, "19,000.00"),             # 第一行明细金额
            (343, 60, 375, 68, "TOTAL"), (377, 60, 410, 68, "DUE"),
            (490, 60, 540, 68, "92,000.00"),             # 真总额
        ]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["total_due"], "92,000.00")

    def test_tax_advisory_not_mistaken_as_tax(self):
        """服务名 'Tax Advisory' 不应被当作税额标签（整格匹配）。"""
        words = [
            (43, 10, 61, 18, "Tax"), (63, 10, 110, 18, "Advisory"),
            (318, 10, 326, 18, "2"), (411, 10, 470, 18, "32,500.00"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertNotIn("sales_tax", g)

    def test_label_less_date_on_invoice_no_row(self):
        """发票日期无 ISSUE DATE 标签时，从发票号同行取签发日期。"""
        words = [
            (43, 10, 60, 18, "No."), (62, 10, 140, 18, "GP-2025-1001"),
            (300, 10, 360, 18, "12"), (362, 10, 400, 18, "January"), (402, 10, 430, 18, "2025"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["invoice_no"], "GP-2025-1001")
        self.assertEqual(g["invoice_date"], "12 January 2025")

    def test_billto_block_name_address_email(self):
        """BILL TO 多行块：名 + 地址 + 邮箱（值与标签右对齐、左缘错位）。"""
        words = [
            (494, 10, 540, 18, "BILLED"), (542, 10, 560, 18, "TO"),
            (407, 30, 560, 38, "NovaTech Solutions GmbH"),
            (467, 50, 560, 58, "Friedrichstrasse 76"),
            (488, 70, 560, 78, "10117 Berlin"),
            (453, 90, 560, 98, "m.chen@novatech.de"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["customer_name"], "NovaTech Solutions GmbH")
        self.assertIn("Berlin", g["customer_address"])
        self.assertEqual(g["contact_email"], "m.chen@novatech.de")

    def test_bank_details_block(self):
        """银行明细：行名/户名/账号/SWIFT，'Account No.' 尾点不算值。"""
        words = [
            (68, 10, 200, 18, "PAYMENT / BANK DETAILS"),
            (68, 30, 100, 38, "Bank"), (167, 30, 260, 38, "JPMorgan Chase Bank NA"),
            (68, 50, 130, 58, "Account Name"), (167, 50, 280, 58, "Ardent Financial Solutions"),
            (68, 70, 130, 78, "Account No."), (167, 70, 240, 78, "783-291847"),
            (68, 90, 130, 98, "SWIFT/BIC"), (167, 90, 230, 98, "CHASUS33"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["bank_name"], "JPMorgan Chase Bank NA")
        self.assertEqual(g["bank_account_no"], "783-291847")
        self.assertEqual(g["bank_swift"], "CHASUS33")

    def test_period_range_split_and_label_collision(self):
        """服务期间区间：'起 - 止'，且同行下个标签(CURRENCY)不污染取值。"""
        words = [
            (60, 10, 90, 18, "TERMS"), (221, 10, 280, 18, "PERIOD"), (383, 10, 440, 18, "CURRENCY"),
            (60, 30, 90, 38, "Net 15"),
            (221, 30, 360, 38, "30 November 2025 - 26 December 2025"), (383, 30, 410, 38, "EUR"),
        ]
        g = generic.extract_generic(_lines(words))
        # 返回原始日期文本（ISO 由下游 _set_date_field 转换），便于定位回原件
        self.assertEqual(g["period_start"], "30 November 2025")
        self.assertEqual(g["period_end"], "26 December 2025")

    def test_line_items_generic(self):
        """无 'Item #' 表头时按 DESCRIPTION/QTY/TOTAL 解析明细行 + 续行并入描述。"""
        words = [
            (45, 10, 200, 18, "DESCRIPTION"), (309, 10, 330, 18, "QTY"),
            (413, 10, 440, 18, "RATE"), (493, 10, 520, 18, "TOTAL"),
            (45, 30, 160, 38, "Data Migration Services"), (315, 30, 322, 38, "1"),
            (396, 30, 450, 38, "27,000.00"), (482, 30, 540, 38, "27,000.00"),
            (45, 50, 200, 58, "Full data lift-and-shift."),     # 续行（无金额）
            (343, 80, 410, 88, "TOTAL"), (411, 80, 440, 88, "DUE"), (493, 80, 540, 88, "27,000.00"),
        ]
        items = generic.extract_line_items(_lines(words))
        self.assertEqual(len(items), 1)
        self.assertIn("Data Migration", items[0]["description"])
        self.assertIn("lift-and-shift", items[0]["description"])    # 续行并入
        self.assertEqual(items[0]["amount"], "27,000.00")
        self.assertEqual(items[0]["quantity"], "1")

    def test_line_items_service_named_tax_not_truncated(self):
        """名为 'Tax Advisory' 的服务行不应被当税额行截断，多笔费用须全识别。"""
        words = [
            (45, 10, 200, 18, "SERVICE DESCRIPTION"), (309, 10, 330, 18, "QTY"),
            (380, 10, 430, 18, "UNIT PRICE"), (493, 10, 530, 18, "AMOUNT"),
            (45, 30, 160, 38, "Fund Administration"), (318, 30, 325, 38, "3"),
            (411, 30, 460, 38, "44,500.00"), (508, 30, 560, 38, "133,500.00"),
            (45, 50, 160, 58, "Tax Advisory"), (318, 50, 325, 58, "2"),
            (411, 50, 460, 58, "32,500.00"), (513, 50, 560, 58, "65,000.00"),
            (45, 70, 160, 78, "KYC/AML Review"), (318, 70, 325, 78, "1"),
            (411, 70, 460, 78, "18,500.00"), (513, 70, 560, 78, "18,500.00"),
            (349, 95, 410, 103, "Subtotal"), (502, 95, 560, 103, "217,000.00"),
        ]
        items = generic.extract_line_items(_lines(words))
        self.assertEqual(len(items), 3)
        self.assertEqual([it["amount"] for it in items], ["133,500.00", "65,000.00", "18,500.00"])
        self.assertEqual(items[0]["quantity"], "3")
        self.assertEqual(items[0]["unit_price"], "44,500.00")

    def test_line_amount_rightmost_when_unit_and_total_merged(self):
        """单价列与合计列被并成一格时，行金额取最右（=行合计），而非单价。"""
        words = [
            (60, 10, 160, 18, "DESCRIPTION"), (326, 10, 350, 18, "QTY"),
            (424, 10, 460, 18, "UNIT"), (509, 10, 540, 18, "TOTAL"),
            # 单价 45,000.00@428 与合计 135,000.00@491 间距小 → 会被并入一格
            (60, 30, 134, 38, "Market Research Report"), (332, 30, 339, 38, "3"),
            (428, 30, 478, 38, "45,000.00"), (491, 30, 545, 38, "135,000.00"),
            (371, 60, 430, 68, "Subtotal"), (491, 60, 545, 68, "135,000.00"),
        ]
        items = generic.extract_line_items(_lines(words))
        self.assertEqual(items[0]["amount"], "135,000.00")     # 取行合计，非单价 45,000.00

    def test_narrative_line_items_wrap_independent(self):
        """散文'合并服务费'叙述：拼接整段再按分号拆 → 不受换行宽度影响（Word/PDF 一致）。"""
        from extraction.parse import amount as amt
        narrative = ("Charge Narrative For the billing period ending 2026-06-07, Acme provided "
                     "the following work in sentence form: data reconciliation support at USD 660.00; "
                     "onsite configuration session at USD 1,040.00; training session at USD 825.00. "
                     "The charge is presented as a consolidated service fee. The subtotal is USD 2,525.00.")
        items = generic.extract_narrative_line_items(narrative)
        self.assertEqual([it["description"] for it in items],
                         ["data reconciliation support", "onsite configuration session", "training session"])
        # 金额带币种代码也能解析为 Decimal（parse_amount 容忍前导币种码）
        from decimal import Decimal
        self.assertEqual(amt.parse_amount(items[0]["amount"])[0], Decimal("660.00"))
        self.assertEqual(amt.parse_amount("USD 1,040.00")[0], Decimal("1040.00"))

    def test_vertical_table_line_items(self):
        """Word/docx 表格被 fitz 提取成竖排（每单元格一行）时，仍能重组成明细行。"""
        cells = ["Line Items", "DESCRIPTION", "QTY", "UNIT PRICE", "AMOUNT",
                 "Exception queue analysis", "5", "USD 132.00", "USD 660.00",
                 "Monthly platform subscription", "3", "USD 240.00", "USD 720.00",
                 "Subtotal: USD 980.00", "Total Due: USD 980.00"]
        words = [(45, 10 + i * 20, 200, 18 + i * 20, t) for i, t in enumerate(cells)]
        items = generic.extract_line_items(_lines(words))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["description"], "Exception queue analysis")
        self.assertEqual(items[0]["quantity"], "5")
        self.assertEqual(items[0]["unit_price"], "132.00")
        self.assertEqual(items[0]["amount"], "660.00")
        self.assertEqual(items[1]["amount"], "720.00")        # 在 Subtotal 处止

    def test_explode_strips_money_from_description(self):
        """规则①：描述里混入金额时剥离，金额落到 amount。"""
        out = generic.explode_description("Consulting services USD 5,000.00", None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["description"], "Consulting services")   # 金额已剥离
        self.assertEqual(out[0]["amount"], "USD 5,000.00")

    def test_subtotal_inline_value_not_from_total_below(self):
        """竖排 'Subtotal: USD 2,741.00' 的内联值应取到 2,741.00，不因分支括号缺失漏取、
        回退拿到下方 Total Due 的额（税前额被误成总额的回归）。"""
        cells = ["DESCRIPTION", "QTY", "UNIT PRICE", "AMOUNT",
                 "Platform subscription", "3", "USD 240.00", "USD 720.00",
                 "Onsite configuration", "2", "USD 520.00", "USD 1,040.00",
                 "Usage monitoring", "6", "USD 88.50", "USD 531.00",
                 "Closeout review", "1", "USD 450.00", "USD 450.00",
                 "Subtotal: USD 2,741.00", "Tax: USD 212.43", "Total Due: USD 2,953.43"]
        words = [(45, 10 + i * 20, 260, 18 + i * 20, t) for i, t in enumerate(cells)]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g.get("subtotal"), "2,741.00")     # 取自身内联值，非下方 Total Due
        self.assertEqual(g.get("sales_tax"), "212.43")
        self.assertEqual(g.get("total_due"), "2,953.43")

    def test_prose_tax_not_from_service_named_tax(self):
        """散文税额兜底不得跨过单词，把服务行 'Tax Advisory … 2' 的数字误当税额。"""
        bad = ("Fund Administration 133,500.00 Tax Advisory 2 32,500.00 65,000.00 "
               "Subtotal GBP 217,000.00 Total Due GBP 217,000.00")
        out = generic.prose_amounts(bad)
        self.assertNotIn("sales_tax", out)                  # 不再误抓成税额=2
        # 正当散文仍能抽取 subtotal / tax / total
        ok = generic.prose_amounts("The subtotal is USD 2,605.00. Applicable tax is USD 201.89. "
                                   "The total amount due is USD 2,806.89.")
        self.assertEqual(ok["subtotal"], "2,605.00")
        self.assertEqual(ok["sales_tax"], "201.89")
        self.assertEqual(ok["total_due"], "2,806.89")

    def test_is_summary_desc_filters_totals_not_services(self):
        """合计/税/小计汇总行判定：汇总标签为真，含额外词的服务名为假。"""
        for d in ["Subtotal", "Sales Tax", "Total Due", "Net Amount", "VAT (10%)",
                  "Total", "GST", "Amount Due", "Grand Total", "Subtotal:"]:
            self.assertTrue(generic.is_summary_desc(d), d)
        for d in ["Tax Advisory", "Total Station Rental", "Subtotal reconciliation service",
                  "Fund Administration", "VAT registration filing"]:
            self.assertFalse(generic.is_summary_desc(d), d)

    def test_net_fees_and_invoice_total_not_line_items(self):
        """竖排 'Net fees'(小计) / 'Invoice Total'(总额) 不得混入明细，其金额应进对应字段。
        （fee 同时命中描述/金额列名正则曾致 'Net fees' 被误当表头 → 真实明细丢失的回归。）"""
        cells = ["Description", "Amount", "Monthly subscription", "USD 950.00",
                 "Guest user pack", "USD 150.00", "Net fees", "USD 1,100.00",
                 "Invoice Total", "USD 1,100.00"]
        words = [(45, 10 + i * 20, 240, 18 + i * 20, t) for i, t in enumerate(cells)]
        items = generic.extract_line_items(_lines(words))
        self.assertEqual([it["description"] for it in items],
                         ["Monthly subscription", "Guest user pack"])   # 合计行不混入
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g.get("subtotal"), "1,100.00")    # Net fees → 小计
        self.assertEqual(g.get("total_due"), "1,100.00")   # Invoice Total → 总额

    def test_letter_prefixed_currency_symbol(self):
        """字母前缀货币符号 HK$ / US$：金额整体识别（不残留 'HK'），合计行 'Net fees HK$…' 被过滤、
        真明细描述干净、字段（含 HK$ 前缀）与勾稽正确。"""
        rows = [("Description", "Amount"), ("Advisory service", "HK$11,610.00"),
                ("Net fees", "HK$11,610.00"), ("Sales Tax 3.00%", "HK$348.30"),
                ("Invoice Total", "HK$11,958.30")]
        words = []
        for i, (d, a) in enumerate(rows):
            words.append((40, 10 + i * 20, 160, 18 + i * 20, d))
            words.append((320, 10 + i * 20, 400, 18 + i * 20, a))
        items = generic.extract_line_items(_lines(words))
        self.assertEqual([it["description"] for it in items], ["Advisory service"])   # 合计行不入、无 HK 残留
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g.get("subtotal"), "HK$11,610.00")
        self.assertEqual(g.get("sales_tax"), "HK$348.30")
        self.assertEqual(g.get("total_due"), "HK$11,958.30")

    def test_fee_in_title_not_mistaken_as_header(self):
        """标题 'STATEMENT OF FEES'（含 fee）不得被当明细表头（fee 同时命中描述/金额列名正则），
        真表头 Description/Amount 才是；明细为两条真服务、合计行不混入、税额入字段。"""
        words = [
            (31, 10, 200, 18, "STATEMENT OF FEES"),
            (31, 40, 120, 48, "Description"), (405, 40, 450, 48, "Amount"),
            (31, 60, 180, 68, "Payroll processing"), (390, 60, 450, 68, "USD 680.00"),
            (31, 80, 200, 88, "MPF report preparation"), (390, 80, 450, 88, "USD 220.00"),
            (308, 100, 360, 108, "Net fees"), (405, 100, 460, 108, "USD 900.00"),
            (273, 120, 360, 128, "Sales Tax 4.00%"), (405, 120, 460, 128, "USD 36.00"),
            (282, 140, 360, 148, "Invoice Total"), (405, 140, 460, 148, "USD 936.00"),
        ]
        items = generic.extract_line_items(_lines(words))
        self.assertEqual([it["description"] for it in items],
                         ["Payroll processing", "MPF report preparation"])
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g.get("subtotal"), "900.00")
        self.assertEqual(g.get("sales_tax"), "36.00")
        self.assertEqual(g.get("total_due"), "936.00")

    def test_inline_description_single_item(self):
        """内联 'Description: 服务名' + 'Amount Due: 金额' → 单明细（零售式简单发票）。"""
        words = [(40, 10, 300, 18, "Description: Monthly finance retainer"),
                 (40, 30, 300, 38, "Amount Due: USD 4,500.00")]
        items = generic.extract_line_items(_lines(words))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["description"], "Monthly finance retainer")
        self.assertEqual(items[0]["amount"], "4,500.00")

    def test_footer_sentence_not_header(self):
        """含 service/fee 的长页脚句子不得被当明细表头 → 不产生垃圾明细。"""
        words = [(40, 10, 700, 18, "There will be an administration fee for reactivation "
                                   "of services after suspension of the account")]
        self.assertEqual(generic.extract_line_items(_lines(words)), [])

    def test_tax_label_with_inline_bare_rate(self):
        """'Sales Tax 5.00%'（标签带**裸**税率、无括号）应识别为税额标签，税额取下一行、税率抽出；
        含税发票 Subtotal+Tax=Total 勾稽平。"""
        cells = ["Description", "Amount", "Regulatory filing review", "USD 1,800.00",
                 "AML monitoring pack", "USD 950.00", "Board memo drafting", "USD 650.00",
                 "Net fees", "USD 3,400.00", "Sales Tax 5.00%", "USD 170.00",
                 "Invoice Total", "USD 3,570.00"]
        words = [(45, 10 + i * 20, 240, 18 + i * 20, t) for i, t in enumerate(cells)]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g.get("subtotal"), "3,400.00")
        self.assertEqual(g.get("sales_tax"), "170.00")
        self.assertEqual(g.get("tax_rate"), "5.00%")
        self.assertEqual(g.get("total_due"), "3,570.00")
        self.assertEqual(len(generic.extract_line_items(_lines(words))), 3)   # 三条明细，合计行不混入

    def test_eu_number_format(self):
        """欧式数字（点千分位、逗号小数 1.234,56）应正确提取小计/税/总额与明细。"""
        cells = ["Description", "Amount", "Consulting", "€ 33.000,00", "Support", "€ 26.400,00",
                 "Subtotal", "€ 59.400,00", "VAT (8%)", "€ 4.752,00", "Total Due", "€ 64.152,00"]
        words = [(45, 10 + i * 20, 240, 18 + i * 20, t) for i, t in enumerate(cells)]
        from extraction.parse import amount as amt
        g = generic.extract_generic(_lines(words))
        self.assertEqual(amt.parse_amount(g.get("subtotal"))[0], Decimal("59400.00"))
        self.assertEqual(amt.parse_amount(g.get("sales_tax"))[0], Decimal("4752.00"))
        self.assertEqual(amt.parse_amount(g.get("total_due"))[0], Decimal("64152.00"))

    def test_space_thousands_split_into_word_tokens(self):
        """空格千分位（1 234,56）——fitz 会在空格处切成多个 word；合计行应拼回而非只取首片。"""
        words = [
            (45, 10, 120, 18, "Subtotal"),
            (460, 10, 474, 18, "88"), (477, 10, 516, 18, "400,00"), (519, 10, 526, 18, "€"),
            (45, 30, 120, 38, "Total Due"),
            (460, 30, 481, 38, "106"), (484, 30, 523, 38, "080,00"), (526, 30, 533, 38, "€"),
        ]
        g = generic.extract_generic(_lines(words))
        from extraction.parse import amount as amt
        self.assertEqual(amt.parse_amount(g.get("subtotal"))[0], Decimal("88400.00"))
        self.assertEqual(amt.parse_amount(g.get("total_due"))[0], Decimal("106080.00"))

    def test_currency_code_suffix(self):
        """币种码写在金额后：1,900.00 USD。"""
        cells = ["Description", "Amount", "Design work", "1,900.00 USD",
                 "Subtotal", "1,900.00 USD", "Total Due", "1,900.00 USD"]
        words = [(45, 10 + i * 20, 260, 18 + i * 20, t) for i, t in enumerate(cells)]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g.get("total_due"), "1,900.00")

    def test_total_incl_pct_gst_label(self):
        """'Total (incl. 10% GST)'（括号里带税率）应识别为总额标签。"""
        self.assertEqual(generic._label_match("Total (incl. 10% GST)")[0], "total_due")
        self.assertEqual(generic._label_match("Total (incl. GST)")[0], "total_due")

    def test_double_tax_lines_summed(self):
        """两条税行（GST + PST）应累加进 sales_tax（加拿大等）。"""
        cells = ["Subtotal", "4,000.00", "GST (5%)", "200.00", "PST (7%)", "280.00", "Total", "4,480.00"]
        words = [(45, 100 + i * 20, 240, 114 + i * 20, t) for i, t in enumerate(cells)]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(Decimal(g["sales_tax"]), Decimal("480.00"))    # 200 + 280

    def test_multilingual_summary_labels(self):
        """日/德/法 小计/税/合计标签识别（值在相邻格，整格即标签）。"""
        for lbl, field in [("小計", "subtotal"), ("消費税", "sales_tax"), ("合計", "total_due"),
                           ("Zwischensumme", "subtotal"), ("MwSt. (19%)", "sales_tax"),
                           ("Gesamtbetrag", "total_due"), ("Sous-total (HT)", "subtotal"),
                           ("TVA (20%)", "sales_tax"), ("Total TTC", "total_due")]:
            m = generic._label_match(lbl)
            self.assertIsNotNone(m, lbl)
            self.assertEqual(m[0], field, lbl)

    def test_canada_pst_hst_is_tax(self):
        """加拿大 PST/HST/QST 应识别为税额标签（与 GST 并存时可累加）。"""
        for lbl in ("PST (7%)", "HST (13%)", "QST (9.975%)"):
            self.assertEqual(generic._label_match(lbl)[0], "sales_tax", lbl)

    def test_is_watermark(self):
        """水印/印章词识别：纯水印/重复/被裁断词尾/印章短语算；真实服务名不算。"""
        self.assertTrue(generic.is_watermark("COPY COPY COPY"))
        self.assertTrue(generic.is_watermark("PAID"))
        self.assertTrue(generic.is_watermark("ORIGINAL ORIGINAL ORIG"))   # 被页宽裁断的词尾
        self.assertTrue(generic.is_watermark("作废 作废 作废"))
        self.assertTrue(generic.is_watermark("★ 上海云帆 发票专用章"))
        self.assertFalse(generic.is_watermark("Copy editing service"))    # 真实服务名不误判
        self.assertFalse(generic.is_watermark("技术咨询服务"))
        self.assertFalse(generic.is_watermark("Consulting fee"))

    def test_watermark_row_not_a_line_item(self):
        """明细区里的水印行（COPY COPY COPY）不应被当成一条明细。"""
        words = [(45, 120, 240, 138, "Description"), (400, 120, 480, 138, "Amount")]
        y = 150
        for desc, amt in [("Consulting", "$1,000.00"), ("COPY COPY COPY", ""), ("Design work", "$500.00")]:
            words.append((45, y, 240, y + 14, desc))
            if amt:
                words.append((400, y, 480, y + 14, amt))
            y += 22
        items = generic.extract_line_items(_lines(words))
        self.assertEqual(len(items), 2)                                   # 水印行被剔除
        self.assertTrue(all(not generic.is_watermark(it["description"]) for it in items))

    def test_label_trailing_colon(self):
        """标签带尾部冒号（值在相邻格）仍应识别：Subtotal: / Total Due: / Sales Tax (8%):"""
        self.assertEqual(generic._label_match("Subtotal:")[0], "subtotal")
        self.assertEqual(generic._label_match("Total Due:")[0], "total_due")
        self.assertEqual(generic._label_match("Sales Tax (8%):")[0], "sales_tax")

    def test_prefix_word_tax(self):
        """前缀词税（Consumption/Withholding Tax）识别为税额；'Tax Advisory' 仍不误命中。"""
        self.assertEqual(generic._label_match("Consumption Tax (10%)")[0], "sales_tax")
        self.assertEqual(generic._label_match("Withholding Tax")[0], "sales_tax")
        self.assertIsNone(generic._label_match("Tax Advisory"))

    def test_receipt_no_is_invoice_no(self):
        self.assertEqual(generic._label_match("Receipt No: RCP-1")[0], "invoice_no")

    def test_tax_invoice_no(self):
        """'Tax Invoice No:'（含 Tax 前缀）应识别为发票号，而非漏掉。"""
        self.assertEqual(generic._label_match("Tax Invoice No: SG-2026-0012")[0], "invoice_no")
        self.assertEqual(generic._label_match("Tax Invoice #")[0], "invoice_no")

    def test_proforma_and_credit_note_no(self):
        self.assertEqual(generic._label_match("Proforma No: PF-1")[0], "invoice_no")
        self.assertEqual(generic._label_match("Credit Note No: CN-1")[0], "invoice_no")
        self.assertEqual(generic._label_match("Total Credit")[0], "total_due")

    def test_adjustment_rows_not_line_items(self):
        """折扣/运费/四舍五入等调整行不算服务明细；含额外词的服务名不误删。"""
        for d in ["Discount", "Less discount", "Freight", "Shipping", "Rounding", "Round off"]:
            self.assertTrue(generic.is_summary_desc(d), d)
        for d in ["Ocean freight", "Express shipping service", "Consulting"]:
            self.assertFalse(generic.is_summary_desc(d), d)

    def test_cn_vat_column_aware(self):
        """中国增值税发票：金额列非最右（税额最右）→ 按序取 金额=倒数第二钱、税额=最后钱；
        小计=Σ金额、税额=Σ税额、总额=价税合计。"""
        words = [(210, 10, 300, 18, "增值税专用发票"),
                 (40, 40, 220, 48, "货物或应税劳务、服务名称"), (430, 40, 470, 48, "金额"), (500, 40, 560, 48, "税率 税额"),
                 (40, 60, 120, 68, "技术咨询服务"), (350, 60, 410, 68, "10000.00"), (430, 60, 490, 68, "10000.00"), (500, 60, 560, 68, "6% 600.00"),
                 (40, 80, 120, 88, "系统集成"), (350, 80, 410, 88, "5000.00"), (430, 80, 490, 88, "5000.00"), (500, 80, 560, 88, "6% 300.00"),
                 (40, 110, 360, 118, "价税合计（大写）（小写）¥15900.00")]
        r = generic.extract_cn_vat(_lines(words))
        self.assertIsNotNone(r)
        self.assertEqual(r["subtotal"], "15000.00")
        self.assertEqual(r["sales_tax"], "900.00")
        self.assertEqual(r["total_due"], "15900.00")
        self.assertEqual(len(r["line_items"]), 2)

    def test_total_wording_variants(self):
        """总额的多种表述都识别为 total_due（含香港 (HK$) 后缀、incl. GST）。"""
        for t in ["Amount Payable", "Balance Due", "Amount Due", "Total Payable", "Net Payable",
                  "Balance Outstanding", "Amount Owing", "Amount to Pay", "Please Pay", "Sum Due",
                  "Final Total", "Total Charges", "Total (incl. GST)", "Total incl. VAT",
                  "Total Due (HK$)", "Grand Total (HKD)"]:
            r = generic._label_match(t)
            self.assertTrue(r and r[0] == "total_due", (t, r))

    def test_more_invoice_no_and_subtotal_wording(self):
        for t in ["Our Ref:", "Document No:", "Invoice Ref:"]:
            self.assertEqual(generic._label_match(t)[0], "invoice_no", t)
        for t in ["Goods Total", "Total excl. GST", "Amount before tax"]:
            self.assertEqual(generic._label_match(t)[0], "subtotal", t)

    def test_prose_tax_not_from_excl_incl_gst(self):
        """'Total excl. GST'/'Total incl. VAT' 里的税词不被散文兜底当成税额。"""
        self.assertNotIn("sales_tax", generic.prose_amounts("Total excl. GST HK$5,096.00 Please Pay HK$5,096.00"))
        self.assertNotIn("sales_tax", generic.prose_amounts("Total incl. VAT £1,200.00"))

    def test_cgst_sgst_summed(self):
        self.assertEqual(generic._label_match("CGST (9%)")[0], "sales_tax")
        self.assertEqual(generic._label_match("SGST (9%)")[0], "sales_tax")

    def test_chinese_vertical_columns(self):
        """竖排表格中文列名（项目/数量/金额/单价）应被 _col_of 识别。"""
        self.assertEqual(generic._col_of("项目"), "description")
        self.assertEqual(generic._col_of("金额"), "amount")
        self.assertEqual(generic._col_of("数量"), "quantity")
        self.assertEqual(generic._col_of("单价"), "unit_price")

    def test_chinese_labels(self):
        """中文发票标签：发票号码/小计/税额/价税合计/开票日期。"""
        self.assertEqual(generic._label_match("发票号码")[0], "invoice_no")
        self.assertEqual(generic._label_match("开票日期")[0], "invoice_date")
        self.assertEqual(generic._label_match("小计")[0], "subtotal")
        self.assertEqual(generic._label_match("税额（6%）")[0], "sales_tax")
        self.assertEqual(generic._label_match("价税合计")[0], "total_due")
        # 中文明细表头与合计行
        self.assertTrue(generic._looks_li_header("项目 金额"))
        self.assertTrue(generic.is_summary_desc("小计"))
        self.assertTrue(generic.is_summary_desc("价税合计"))
        self.assertFalse(generic.is_summary_desc("技术咨询服务"))

    def test_invoice_total_label_not_customer(self):
        """'Invoice Total' 应识别为总额，不因 'invoice to'+tal 前缀误判成客户名；Bill To 仍是客户。"""
        self.assertEqual(generic._label_match("Invoice Total")[0], "total_due")
        self.assertEqual(generic._label_match("Bill To")[0], "customer_name")
        self.assertEqual(generic._label_match("Billed To:")[0], "customer_name")

    def test_explode_keeps_clean_single_service(self):
        """干净表格行（金额已在独立列、描述无金额）原样保留。"""
        out = generic.explode_description("Market Research Report", "135,000.00",
                                          base_qty="3", base_unit="45,000.00")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["description"], "Market Research Report")
        self.assertEqual(out[0]["amount"], "135,000.00")
        self.assertEqual(out[0]["quantity"], "3")

    def test_explode_splits_on_semicolon(self):
        """规则②：分号分隔 → 拆成不同服务。"""
        out = generic.explode_description("Design; Develop; Deploy", "900.00")
        self.assertEqual([o["description"] for o in out], ["Design", "Develop", "Deploy"])
        self.assertEqual(out[0]["amount"], "900.00")    # 原总额留首段防丢
        self.assertIsNone(out[1]["amount"])

    def test_explode_splits_on_inline_amounts(self):
        """规则②：一条描述里多个内嵌金额 → 各金额结束一个服务。"""
        out = generic.explode_description("Setup fee USD 1,000.00 Monthly hosting USD 2,000.00", None)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["description"], "Setup fee")
        self.assertEqual(out[0]["amount"], "USD 1,000.00")
        self.assertEqual(out[1]["description"], "Monthly hosting")
        self.assertEqual(out[1]["amount"], "USD 2,000.00")

    def test_explode_does_not_split_quantities_or_dates(self):
        """裸小整数/日期不被当金额，不误拆。"""
        out = generic.explode_description("24 month support plan from 30 November 2025", "5,000.00")
        self.assertEqual(len(out), 1)
        self.assertIn("24 month support plan", out[0]["description"])
        self.assertEqual(out[0]["amount"], "5,000.00")

    def test_currency_fallback_header_footer_label(self):
        self.assertEqual(generic.currency_fallback("UNIT (USD) ... All amounts in USD"), "USD")
        self.assertEqual(generic.currency_fallback("Currency: GBP"), "GBP")
        self.assertEqual(generic.currency_fallback("AMOUNT (AUD)"), "AUD")

    def test_incl_tax_fallback(self):
        rate, tax = generic.incl_tax_fallback("TOTAL DUE GBP 89,700.00 Incl. tax (15%): GBP 11,700.00")
        self.assertEqual(rate, "15%")
        self.assertEqual(tax, "11,700.00")

    def test_company_name_skips_noise(self):
        """公司名识别：跳过 'SIMULATED INVOICE'/'Invoice No:'/地址，认带后缀的公司名。"""
        self.assertFalse(generic.looks_like_company("SIMULATED INVOICE"))
        self.assertFalse(generic.looks_like_company("SIMULATED"))
        self.assertFalse(generic.looks_like_company("Invoice No: GP-2025-1001"))
        self.assertFalse(generic.looks_like_company("1400 Market Test Ave, Suite 210"))
        self.assertTrue(generic.looks_like_company("Northbridge Labs LLC"))
        self.assertTrue(generic.looks_like_company("Greyvane Partners"))
        self.assertEqual(generic.company_score("Northbridge Labs LLC"), 1)

    def test_issuer_skips_doc_title_and_disclaimer(self):
        """开票方块跳过 'SIMULATED INVOICE' 标题与免责声明，取真正的公司名。"""
        words = [
            (60, 5, 200, 13, "SIMULATED"), (260, 5, 320, 13, "INVOICE"),
            (60, 20, 360, 28, "NOT FOR PAYMENT - parser test fixture"),
            (60, 38, 240, 46, "Northbridge Labs LLC"),
            (60, 56, 360, 64, "1400 Market Test Ave, Suite 210, Seattle"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["issuer_name"], "Northbridge Labs LLC")

    def test_find_phone(self):
        """电话识别：取带 + 的国际号/Tel 标签号；不误命中发票号/账号。"""
        self.assertEqual(generic.find_phone("+852 2100 8800"), "+852 2100 8800")
        self.assertEqual(generic.find_phone("Tel: 020 7710 3000"), "020 7710 3000")
        self.assertIsNone(generic.find_phone("Invoice No: GP-2025-1001"))
        self.assertIsNone(generic.find_phone("004-738201-881"))   # 账号，非电话
        # 病态超长 "+1 1 1 …"：位数远超 15，不应存成数 KB "电话"
        r = generic.find_phone("Call +" + "1 " * 5000)
        self.assertTrue(r is None or len(r) < 40)

    def test_issuer_phone_extracted(self):
        words = [
            (60, 10, 200, 18, "Halcyon Consulting Group"),
            (60, 30, 360, 38, "Suite 1400, 1 King Street West, Toronto, Canada"),
            (60, 50, 160, 58, "+1 416 867 3000"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["issuer_phone"], "+1 416 867 3000")
        self.assertEqual(g["issuer_name"], "Halcyon Consulting Group")

    def test_addr_noisy_detection(self):
        self.assertTrue(generic.addr_noisy("Date: 12 January 2025, Terms: Net 30"))
        self.assertTrue(generic.addr_noisy("NOT FOR PAYMENT, Northbridge Labs LLC"))
        self.assertTrue(generic.addr_noisy("77 Control Circle, Charge Narrative"))
        self.assertFalse(generic.addr_noisy("1400 Market Test Ave, Suite 210, Seattle, WA 98101"))

    def test_address_block_stops_at_section(self):
        """BILL TO 地址块在 'Charge Narrative'/'Line Items' 等区块头停住，不漫入正文。"""
        words = [
            (60, 10, 90, 18, "Bill To"),
            (60, 30, 200, 38, "Vertex Health Sandbox"),
            (60, 50, 320, 58, "77 Control Circle, Raleigh, NC 27601"),
            (60, 70, 200, 78, "Charge Narrative"),
            (60, 90, 360, 98, "For the billing period ending 2026-06-03 ..."),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertIn("Raleigh", g["customer_address"])
        self.assertNotIn("Narrative", g["customer_address"])
        self.assertNotIn("billing period", g["customer_address"])

    def test_issuer_block(self):
        words = [
            (421, 5, 480, 13, "INVOICE"),
            (60, 20, 240, 28, "Halcyon Consulting Group"),
            (60, 40, 320, 48, "Suite 1400, 1 King Street West, Toronto, Canada"),
            (60, 60, 160, 68, "+1 416 867 3000"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["issuer_name"], "Halcyon Consulting Group")
        self.assertIn("Toronto", g["issuer_address"])

    def test_section_header_not_taken_as_customer(self):
        """'BILL TO' 下方若是区块标题/列头（CONTACT 等），不应当作客户名。"""
        words = [
            (60, 10, 80, 18, "BILL"), (82, 10, 100, 18, "TO"),
            (60, 30, 110, 38, "CONTACT"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertNotIn("customer_name", g)


if __name__ == "__main__":
    unittest.main()


class TestPdfTextNormalize(unittest.TestCase):
    """PDF 文本归一：nbsp 家族→空格、真连字符(U+2010/2011/2012)→'-'，不动 en/em dash。"""

    def test_norm_nbsp_and_hyphens(self):
        from extraction.extract.pdf_text import _norm_text
        self.assertEqual(_norm_text("JP‑2026‑5005"), "JP-2026-5005")   # 非断行连字符
        self.assertEqual(_norm_text("A\xa0B"), "A B")                            # nbsp
        self.assertEqual(_norm_text("2026‐05‐20"), "2026-05-20")       # 连字符/图形连字符
        self.assertEqual(_norm_text("x​y"), "xy")                           # 零宽移除

    def test_norm_keeps_en_em_dash(self):
        from extraction.extract.pdf_text import _norm_text
        self.assertEqual(_norm_text("2026–2027"), "2026–2027")         # en dash 不动
        self.assertEqual(_norm_text("a—b"), "a—b")                     # em dash 不动


class TestStripMoneyBounded(unittest.TestCase):
    """_strip_money 对超长畸形描述有界（防 O(n²) 算法复杂度 DoS）+ 正常描述不受影响。"""

    def test_long_input_bounded_fast(self):
        import time
        s = "consulting " + "at USD " * 5000            # 旧实现此处 ~30s
        t = time.time(); r = generic._strip_money(s); dt = time.time() - t
        self.assertLess(dt, 1.0, "超长输入应在 1s 内完成（有界）")
        self.assertLessEqual(len(r), generic._MAX_DESC_LEN)

    def test_normal_desc_unchanged(self):
        self.assertEqual(generic._strip_money("Consulting services at USD 1,200.00"), "Consulting services")
        self.assertEqual(generic._strip_money("Data migration"), "Data migration")


class TestCapSplit(unittest.TestCase):
    """拆分数量上限：单文件拆出的发票数超上限只取前 N（防小文件放大成海量记录）。"""

    def test_cap_truncates(self):
        from extraction import pipeline
        from core import config
        seq = list(range(config.MAX_INVOICES_PER_FILE + 50))
        capped = pipeline._cap_split(seq, "test")
        self.assertEqual(len(capped), config.MAX_INVOICES_PER_FILE)

    def test_cap_under_limit_untouched(self):
        from extraction import pipeline
        seq = [1, 2, 3]
        self.assertEqual(pipeline._cap_split(seq, "test"), seq)
        self.assertIsNone(pipeline._cap_split(None, "test"))


class TestIntegerAmountResolve(unittest.TestCase):
    """无千分位无小数的整数金额（日元/韩元或没写千分位）在金额值槽里应被取到。"""
    def test_jpy_integer_tax_resolved(self):
        rows = [("Subtotal", "JPY 150000"), ("Tax (10%)", "JPY 15000"), ("Total Due", "JPY 165000")]
        words = []
        y = 100
        for lbl, val in rows:
            words.append((40, y, 200, y + 12, lbl)); words.append((500, y, 590, y + 12, val)); y += 20
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g.get("subtotal"), "150000")
        self.assertEqual(g.get("sales_tax"), "15000")
        self.assertEqual(g.get("total_due"), "165000")


class TestSectionSubtotals(unittest.TestCase):
    """分组小计发票（多个 subtotal 标签）→ 取"总小计"（与 total 自洽/最大），非首个 section 小计。"""
    def test_grand_subtotal_picked(self):
        cells = ["Section A subtotal", "1,500.00", "Section B subtotal", "3,000.00",
                 "Subtotal", "4,500.00", "Tax", "0.00", "Total Due", "4,500.00"]
        words = [(45, 100 + i * 20, 240, 114 + i * 20, t) for i, t in enumerate(cells)]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["subtotal"], "4,500.00")     # 总小计，非 1,500 section 小计


class TestPeriodYtdColumns(unittest.TestCase):
    """本期/累计(YTD)双金额列账单：取金额排除 YTD 累计列，只取本期。"""
    def test_total_picks_this_period_not_ytd(self):
        words = [
            (40, 100, 200, 112, "Description"), (400, 100, 470, 112, "This Period"),
            (505, 100, 580, 112, "Year to Date"),
            (40, 120, 120, 132, "Electricity"), (400, 120, 450, 132, "$100.00"), (505, 120, 560, 132, "$600.00"),
            (40, 150, 160, 162, "Total This Period"), (400, 150, 450, 162, "$150.00"), (505, 150, 560, 162, "$900.00"),
        ]
        g = generic.extract_generic(_lines(words))
        self.assertEqual(g["total_due"], "$150.00")      # 本期，不是 YTD $900.00


class TestHighPrecisionAndDualColumnLines(unittest.TestCase):
    """加密/高精度明细金额全精度保留 + 本期/累计双列明细取本期列。"""
    def test_high_precision_money_token(self):
        # "2.500000"（6 位小数）不再被欧式千分位吃成 "2.500"；欧式数字仍正确
        self.assertEqual(generic._MONEY.findall("2.500000 ETH"), ["2.500000"])
        self.assertEqual(generic._MONEY.findall("0.041500"), ["0.041500"])
        self.assertEqual(generic._MONEY.findall("1.234,56"), ["1.234,56"])       # 欧式不受影响
        self.assertEqual(generic._MONEY.findall("2.500.000"), ["2.500.000"])     # 欧式千分位不受影响

    def test_dual_column_line_items_this_period(self):
        words = [
            (40, 100, 200, 112, "Description"), (400, 100, 470, 112, "This Period"),
            (505, 100, 580, 112, "Year to Date"),
            (40, 120, 120, 132, "Electricity"), (400, 120, 450, 132, "$100.00"), (505, 120, 560, 132, "$600.00"),
            (40, 140, 120, 152, "Water"), (400, 140, 450, 152, "$50.00"), (505, 140, 560, 152, "$300.00"),
        ]
        items = generic.extract_line_items(_lines(words))
        amts = [it["amount"] for it in items]
        self.assertIn("$100.00", amts); self.assertIn("$50.00", amts)   # 本期
        self.assertNotIn("$600.00", amts); self.assertNotIn("$300.00", amts)  # 排除 YTD
