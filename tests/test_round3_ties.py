"""Intermediate-state report ties that uniform fully-settled fixtures missed."""
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from core import config, db
from core.models import Invoice, FieldValue, Classification
from ledger import accounts as A, service, store
from reports import service as reports


class RealisticTies(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = patch.multiple(config, DB_PATH=root/'test.db', CHART_PATH=root/'chart.json',
                                  FX_RATES_PATH=root/'fx.json', FX_AUTO_FETCH=False)
        self.cfg.start(); db._initialized = False; db.init_db(); A.reload_chart()

    def tearDown(self):
        self.cfg.stop(); db._initialized = False; A.reload_chart(); self.tmp.cleanup()

    def acct(self, code):
        return A.canonical_account(code)

    def journal(self, rows, activity=None):
        return service.post_manual_entry([dict(account=self.acct(a), debit=d, credit=c) for a,d,c in rows],
            date='2026-08-10', activity=activity, allow_control=True, counterparty='Synthetic Counterparty')

    def asset_invoice(self, amount='1100', net='1000', tax='100', code='1500'):
        inv = Invoice(file_hash='asset', approve_status='Approved')
        for k,v in dict(total_due=amount, subtotal=net, sales_tax=tax, currency_settlement='USD',
                        invoice_no='ASSET-3', invoice_date='2026-08-01', issuer_name='Vendor').items():
            inv.set(k, FieldValue(value=v))
        inv.classification = Classification(account=self.acct(code)); db.save_invoice(inv)
        service.post_invoice_by_hash('asset', direction='AP')

    def tied(self, profit='0', cash='0'):
        r = reports.generate()
        self.assertTrue(r['can_issue'], r['checks'])
        self.assertEqual(D(r['income_statement']['net_income']), D(profit))
        self.assertEqual(D(r['cash_flow_statement']['ending']), D(cash))

    def test_asset_invoice_unpaid_partial_paid_and_close(self):
        self.asset_invoice(); self.tied()
        service.settle_invoice('asset', '440', settle_amount='440', date='2026-08-12'); self.tied(cash='-440')
        service.settle_invoice('asset', '660', date='2026-08-13'); self.tied(cash='-1100')
        service.close_period('2026-08'); self.tied(cash='-1100')

    def test_intangible_credit_refund(self):
        self.asset_invoice('-1100', '-1000', '-100', '1601'); self.tied()
        service.settle_invoice('asset', '1100', date='2026-08-12'); self.tied(cash='1100')

    def test_depreciation_reverse_repost(self):
        no = self.journal([('6602',100,0),('1509',0,100)])
        self.tied('-100')
        store.reverse_entry(no, by='test', at='2026-08-11T00:00:00Z'); self.tied()
        self.journal([('6602',80,0),('1509',0,80)]); self.tied('-80')

    def test_disposal_and_its_reversal_does_not_create_depreciation(self):
        self.journal([('1500',1000,0),('1002',0,1000)], 'investing')
        self.journal([('6602',200,0),('1509',0,200)])
        no = self.journal([('1002',900,0),('1509',200,0),('1500',0,1000),('4200',0,100)], 'investing')
        self.tied('-100','-100')
        store.reverse_entry(no, by='test', at='2026-08-11T00:00:00Z'); self.tied('-200','-1000')

    def test_inventory_capitalized_and_debt_reclassified(self):
        self.journal([('1405',1000,0),('2280',0,1000)]); self.tied()
        self.journal([('1500',600,0),('1405',0,600)]); self.tied()
        self.journal([('2280',1000,0),('2501',0,1000)]); self.tied()

    def test_non_cash_share_compensation_and_dividend_payable(self):
        self.journal([('6601',100,0),('3000',0,100)]); self.tied('-100')
        self.journal([('3200',50,0),('2280',0,50)]); self.tied('-100')

    def test_cash_asset_purchase_mistagged_operating_stays_blocked(self):
        self.journal([('1500',1000,0),('1002',0,1000)], 'operating')
        r = reports.generate()
        self.assertFalse(r['checks']['E4_cfo_direct_indirect']['ok'])
        self.assertFalse(r['can_issue'])

    def test_unknown_report_token_is_e6_not_silent_omission(self):
        config.CHART_PATH.write_text(json.dumps({'accounts':[
            ['1777','Synthetic asset','asset','debit','BalanceSheet:UnsupportedAsset'],
            ['4777','Synthetic income','revenue','credit','IncomeStatement:NetIncome']]}))
        A.reload_chart()
        self.journal([('1777',300,0),('4777',0,300)])
        checks = reports.checks()
        self.assertFalse(checks['E6_all_classified']['ok'])
        self.assertEqual(len(checks['E6_all_classified']['unclassified']),2)
        self.assertFalse(checks['can_issue'])

    def test_negative_revenue_closes_and_reopens_without_double_count(self):
        self.journal([('4000',1000,0),('1002',0,1000)],'operating')
        self.tied('-1000','-1000')
        service.close_period('2026-08'); self.tied('-1000','-1000')
        self.assertEqual(service.load_ledger().net(self.acct('4000')),D(0))
        service.reopen_period('2026-08'); self.tied('-1000','-1000')
        service.close_period('2026-08'); self.tied('-1000','-1000')

    def test_negative_expense_and_fx_gain_close(self):
        self.journal([('1002',1000,0),('6900',0,1000)],'operating')
        self.journal([('1002',110,0),('6607',0,110)],'operating')
        service.close_period('2026-08'); self.tied('1110','1110')
        for code in ('6900','6607','3300'):
            self.assertEqual(service.load_ledger().net(self.acct(code)),D(0))

    def test_zero_net_revenue_still_closes_each_nonzero_account(self):
        self.journal([('1002',100,0),('4000',0,100)],'operating')
        self.journal([('4001',100,0),('1002',0,100)],'operating')
        self.journal([('6900',70,0),('1002',0,70)],'operating')
        self.journal([('1002',30,0),('6607',0,30)],'operating')
        service.close_period('2026-08');self.tied('-40','-40')
        for code in ('4000','4001','6900','6607','3300'):
            self.assertEqual(service.load_ledger().net(self.acct(code)),D(0))


if __name__ == '__main__':
    unittest.main()
