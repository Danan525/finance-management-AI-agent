"""Regression cases from the first real-business run, including negative controls."""
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import fitz
from fastapi.testclient import TestClient
from core import config, db, counterparty, fx
from core.models import Invoice, FieldValue, Classification
from extraction import pipeline
from extraction.parse import generic
from extraction.extract import ocr
from ledger import service, store, settlement, accounts as A
from reports import service as reports


class Round2Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old = {k: getattr(config, k) for k in ("DB_PATH", "UPLOAD_DIR", "EXPORT_DIR", "FX_RATES_PATH", "FX_AUTO_FETCH")}
        config.DB_PATH = root / "test.db"
        config.UPLOAD_DIR = root / "uploads"; config.UPLOAD_DIR.mkdir()
        config.EXPORT_DIR = root / "exports"; config.EXPORT_DIR.mkdir()
        config.FX_RATES_PATH = root / "rates.json"; config.FX_AUTO_FETCH = False
        db._initialized = False; db.init_db()

    def tearDown(self):
        for k, v in self.old.items():
            setattr(config, k, v)
        db._initialized = False
        self.tmp.cleanup()

    def invoice(self, h="credit", ccy="USD", total="-120", sub="-108", tax="-12"):
        inv = Invoice(file_hash=h, approve_status="Approved")
        for k, v in dict(invoice_no=h, invoice_date="2026-08-05", total_due=total,
                         subtotal=sub, sales_tax=tax, currency_settlement=ccy,
                         issuer_name="Vendor Ltd", customer_name="Customer Ltd").items():
            inv.set(k, FieldValue(value=v))
        inv.classification = Classification(account=A.EXPENSE_DEFAULT)
        db.save_invoice(inv)
        return inv

    def test_chinese_full_name_labels_and_unambiguous_net_amount(self):
        rows = ["销售方名称: 测试商家有限公司", "购买方名称: 测试客户有限公司",
                "金额: 1080.00", "税额: 120.00", "价税合计: 1200.00"]
        parsed = generic.extract_generic([SimpleNamespace(words=[(0., 500., s)]) for s in rows])
        self.assertEqual(parsed["customer_name"], "测试客户有限公司")
        self.assertEqual(parsed["subtotal"], "1080.00")
        # A conflicting or repeated 金额 must not be promoted to the invoice net.
        for extra in (["金额: 99.00"], []):
            changed = [s.replace("价税合计: 1200.00", "价税合计: 9999.00") for s in rows] + extra
            out = generic.extract_generic([SimpleNamespace(words=[(0., 500., s)]) for s in changed])
            self.assertNotIn("subtotal", out)

    def test_stablecoin_codes_and_no_partial_suffix_match(self):
        for ccy in ("USDT", "USDC", "ETH", "BTC", "CNY"):
            self.assertEqual(generic._accept("ccy", ccy), ccy)
            self.assertEqual(generic.currency_fallback("Currency: " + ccy), ccy)
        self.assertIsNone(generic._accept("ccy", "USDTXYZ"))

    def test_ocr_grouping_repair_does_not_change_valid_formats_or_crypto(self):
        box = [[0, 0], [150, 0], [150, 30], [0, 30]]
        originals = ["1.200.00", "-12.345.67", "1.200,00", "1.200", "0.12345678", "1.23456789", "12.34.56"]
        expected = ["1,200.00", "-12,345.67", "1.200,00", "1.200", "0.12345678", "1.23456789", "12.34.56"]
        self.assertEqual([x[0] for x in ocr._fix_amount_tokens([(s, .99, box) for s in originals])], expected)
        wide = ["1，735.83", "－１，２３４．５０", "公司，735.83", "ＡＢＣ株式会社"]
        self.assertEqual([x[0] for x in ocr._fix_amount_tokens([(s, .99, box) for s in wide])],
                         ["1,735.83", "-1,234.50", "公司，735.83", "ＡＢＣ株式会社"])

    def test_credit_original_pdf_keeps_all_negative_summaries(self):
        doc = fitz.open(); page = doc.new_page()
        lines = ["Vendor Ltd", "Credit Note", "Bill To: Customer Ltd", "Invoice No: CN-2026-2",
                 "Invoice Date: 2026-08-05", "Currency: USD", "Subtotal: -108.00", "Tax: -12.00", "Total Due: -120.00"]
        for i, text in enumerate(lines):
            page.insert_text((40, 45+i*24), text)
        data = doc.tobytes(); doc.close()
        inv = pipeline.process_upload(data, "credit.pdf")[0]
        self.assertEqual([inv.f(k).value for k in ("subtotal", "sales_tax", "total_due")], [D(-108), D(-12), D(-120)])

    def test_credit_refunds_both_directions_partial_overpay_reverse_and_close(self):
        for direction in ("AR", "AP"):
            with self.subTest(direction=direction):
                h = "cn-"+direction; self.invoice(h)
                no = service.post_invoice_by_hash(h, direction=direction)
                lines = {l.account: l for l in store.get_entry(no).lines}
                tax_line = lines[A.OUTPUT_TAX if direction == "AR" else A.INPUT_TAX]
                self.assertEqual(tax_line.debit if direction == "AR" else tax_line.credit, D(12))
                self.assertEqual(settlement.open_amount(h)[0], D(-120))
                self.assertTrue(service.control_view()[direction]["ok"])
                refund = service.settle_invoice(h, "40", settle_amount="40", date="2026-08-10")
                self.assertEqual(store.get_entry(refund).cash_delta(), D(-40) if direction == "AR" else D(40))
                self.assertEqual(settlement.open_amount(h)[0], D(-80))
                with self.assertRaises(ValueError):
                    service.settle_invoice(h, "90", settle_amount="90", date="2026-08-11")
                # The locked storage layer also refuses an over-refund.
                from ledger.engine import JournalEntry, JournalLine
                ctrl = A.AR if direction == "AR" else A.AP
                e = JournalEntry(date="2026-08-11", memo="over-refund", source_kind="settlement", source_hash=h,
                    lines=[JournalLine(ctrl, debit=90), JournalLine(A.BANK, credit=90)])
                with self.assertRaises(ValueError):
                    store.post_entry(e, by="test", at="test", settle_amount=D(-90), activity="operating")
                service.settle_invoice(h, "80", date="2026-08-12")
                self.assertEqual(settlement.open_amount(h)[0], D(0))
                store.reverse_entry(refund, by="test", at="test")
                self.assertEqual(settlement.open_amount(h)[0], D(-40))
                service.settle_invoice(h, "40", date="2026-08-14")
                self.assertTrue(service.control_view()[direction]["ok"])
        service.close_period("2026-08")
        self.assertTrue(reports.checks()["can_issue"], reports.checks())

    def test_credit_mixed_signs_rejected(self):
        self.invoice(sub="108")
        with self.assertRaisesRegex(ValueError, "符号"):
            service.post_invoice_by_hash("credit", direction="AR")

    def test_credit_non_deductible_tax_reverses_gross_cost(self):
        self.invoice()
        no = service.post_invoice_by_hash("credit", direction="AP", tax_deductible=False)
        e = store.get_entry(no)
        self.assertNotIn(A.INPUT_TAX, [l.account for l in e.lines])
        self.assertEqual(next(l.credit for l in e.lines if l.account == A.EXPENSE_DEFAULT), D(120))

    def test_financing_not_in_operating_working_capital(self):
        for account in ("2280 其它应付款", "2001 短期借款", "2501 长期借款"):
            service.post_manual_entry([{"account": A.BANK, "debit": "1000"}, {"account": account, "credit": "1000"}],
                date="2026-08-10", activity="financing")
        self.assertTrue(reports.checks()["can_issue"], reports.checks())
        self.assertEqual(D(reports.cash_flow_indirect()["operating"]), D(0))
        service.post_manual_entry([{"account": "2280 其它应付款", "debit": "500"}, {"account": A.BANK, "credit": "500"}],
            date="2026-08-11", activity="financing")
        service.close_period("2026-08")
        self.assertTrue(reports.checks()["can_issue"])

    def test_crypto_cannot_silently_settle_to_bank_and_revaluation_keeps_precision(self):
        self.invoice("btc", "BTC", ".12345678", ".12345678", "0")
        fx.add_rate("BTC", "2026-08-01", "60000")
        service.post_invoice_by_hash("btc", direction="AR", as_of="2026-08-05")
        before = len(service.entries_view())
        with self.assertRaisesRegex(ValueError, "数字资产"):
            service.settle_invoice("btc", ".12345678", cash_currency="BTC", settle_amount="7407.41", date="2026-08-10")
        self.assertEqual(len(service.entries_view()), before)
        self.assertEqual(service.fx_revaluation_view("2026-08-10")["items"][0]["open_ccy"], "0.12345678")

    def test_original_download_uses_source_and_keeps_path_boundary(self):
        from gateway.main import app
        raw = config.UPLOAD_DIR / "original.docx"; raw.write_bytes(b"PK-original-upload")
        converted = config.UPLOAD_DIR / "preview.pdf"; converted.write_bytes(b"%PDF-preview")
        inv = self.invoice(); inv.file_path = str(converted); inv.file_name = "preview.pdf"
        inv.source_file_path = str(raw); inv.source_file_name = raw.name; db.save_invoice(inv)
        client = TestClient(app)
        r = client.get("/api/review/credit/original")
        self.assertEqual(r.content, raw.read_bytes())
        self.assertIn("original.docx", r.headers["content-disposition"])
        outside = Path(self.tmp.name) / "outside.docx"; outside.write_bytes(b"out-of-scope")
        inv.source_file_path = str(outside); db.save_invoice(inv)
        self.assertEqual(client.get("/api/review/credit/original").status_code, 404)
        client.close()

    def test_ocr_name_spacing_only_from_unique_registered_name(self):
        counterparty.register("North Lane Health LLC", kind="self")
        inv = Invoice(ocr_used=True)
        inv.set("issuer_name", FieldValue(raw="NorthLaneHealthLLC", value="NorthLaneHealthLLC"))
        pipeline._restore_registered_name_spacing(inv)
        self.assertEqual(inv.f("issuer_name").value, "North Lane Health LLC")
        self.assertEqual(inv.f("issuer_name").raw, "NorthLaneHealthLLC")
        counterparty.register("NorthLane Health LLC", kind="self", force=True)
        inv.f("issuer_name").value = "NorthLaneHealthLLC"
        pipeline._restore_registered_name_spacing(inv)
        self.assertEqual(inv.f("issuer_name").value, "NorthLaneHealthLLC")


if __name__ == "__main__":
    unittest.main()
