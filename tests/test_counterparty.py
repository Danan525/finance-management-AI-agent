"""对手方主数据：归一化 / 相似度查重 / 人工建档 / 别名归并 / 待建档队列。

隔离临时库，绝不污染真实库。红线校验：**不自动建档、不自动合并**——疑似重复只拒绝并给候选。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from core import config, counterparty as cp, db
from core.models import Classification, FieldValue, Invoice


def _mk_invoice(file_hash, issuer="ACME Inc.", customer="My Company Ltd",
                no="INV-1", doc_type="invoice"):
    inv = Invoice()
    inv.file_hash = file_hash
    inv.doc_type = doc_type
    inv.approve_status = "Pending"
    inv.set("invoice_no", FieldValue(value=no))
    inv.set("total_due", FieldValue(value="100"))
    inv.set("issuer_name", FieldValue(value=issuer))
    inv.set("customer_name", FieldValue(value=customer))
    inv.classification = Classification(account="6440 会计费")
    return inv


class NormalizeTest(unittest.TestCase):
    def test_company_suffix_stripped(self):
        for a, b in [("ACME", "ACME Inc."), ("ACME", "Acme, Inc"), ("ACME", "ACME INC"),
                     ("Halcyon Consulting", "Halcyon Consulting LLC"),
                     ("北京蓝天科技", "北京蓝天科技有限公司")]:
            self.assertEqual(cp.normalize(a), cp.normalize(b), f"{a} vs {b}")

    def test_different_companies_differ(self):
        self.assertNotEqual(cp.normalize("ACME Inc"), cp.normalize("Acorn Inc"))

    def test_similarity(self):
        self.assertEqual(cp.similarity("ACME Inc", "acme, inc."), 1.0)
        # 词序不同（Jaccard 兜住）
        self.assertGreaterEqual(cp.similarity("Acme Global Services", "Global Services Acme"), 0.9)
        self.assertLess(cp.similarity("ACME Inc", "Umbrella Corp"), cp.SUGGEST_THRESHOLD)
        self.assertEqual(cp.similarity("", "ACME"), 0.0)

    def test_suffix_only_name_survives(self):
        self.assertTrue(cp.normalize("Company"))     # 整体是后缀词也不能归一成空


class MasterDataTest(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db, self._up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = self._dir / "t.db"
        config.UPLOAD_DIR = self._dir / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR = self._db, self._up
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_register_and_resolve(self):
        p = cp.register("ACME Inc.", kind=cp.KIND_VENDOR, tax_id="T-1", by="tester")
        self.assertEqual(p["kind"], "vendor")
        self.assertEqual(cp.resolve("acme, inc")["id"], p["id"])   # 归一化后同一家
        self.assertIsNone(cp.resolve("Umbrella Corp"))
        self.assertEqual(len(cp.list_parties()), 1)

    def test_duplicate_registration_rejected_not_merged(self):
        cp.register("ACME Inc.", by="tester")
        with self.assertRaises(ValueError):        # 完全同名（归一化后）→ 已建档
            cp.register("acme inc", by="tester")
        with self.assertRaises(ValueError) as cm:  # 疑似重复（拼写差一字母）→ 拒绝 + 给候选（不自动合并）
            cp.register("Acmee Inc", by="tester")
        self.assertIn("疑似", str(cm.exception))
        self.assertEqual(len(cp.list_parties()), 1)

    def test_force_registers_another_company(self):
        cp.register("ACME Inc.", by="tester")
        p2 = cp.register("Acmee Inc", by="tester", force=True)   # 人确认确为另一家
        self.assertEqual(len(cp.list_parties()), 2)
        self.assertNotEqual(p2["id"], cp.resolve("ACME Inc.")["id"])

    def test_alias_merges_write_variant(self):
        p = cp.register("ACME Inc.", by="tester")
        cp.add_alias(p["id"], "ACME (US) Holdings", by="tester")
        self.assertEqual(cp.resolve("acme us holdings")["id"], p["id"])
        self.assertIn("ACME (US) Holdings", cp.list_parties()[0]["aliases"])
        # 同一写法不能同时归属两家
        q = cp.register("Umbrella Corp", by="tester")
        with self.assertRaises(ValueError):
            cp.add_alias(q["id"], "ACME (US) Holdings", by="tester")

    def test_candidates_flag_likely_same(self):
        cp.register("Halcyon Consulting Group", by="tester")
        cands = cp.candidates("Halcyon Consulting Group LLC")
        self.assertTrue(cands and cands[0]["likely_same"])
        self.assertEqual(cp.candidates("Totally Different Vendor"), [])

    def test_update_and_archive(self):
        p = cp.register("ACME Inc.", by="tester")
        cp.update_party(p["id"], kind=cp.KIND_BOTH, note="主要供应商")
        self.assertEqual(cp.parse_roles(cp.get_party(p["id"])["kind"]), {"vendor", "customer"})  # both 规范化成 vendor,customer
        cp.update_party(p["id"], status="archived")
        self.assertIsNone(cp.resolve("ACME Inc."))            # 归档后不再解析
        self.assertEqual(cp.list_parties(), [])
        self.assertEqual(len(cp.list_parties(include_archived=True)), 1)
        with self.assertRaises(ValueError):
            cp.update_party(p["id"], kind="不明")

    def test_pending_queue_from_invoices(self):
        db.save_invoice(_mk_invoice("h1", issuer="ACME Inc.", customer="My Company Ltd"))
        db.save_invoice(_mk_invoice("h2", issuer="ACME, Inc", customer="My Company Ltd"))
        db.save_invoice(_mk_invoice("h3", issuer="Umbrella Corp", customer="My Company Ltd"))
        db.save_invoice(_mk_invoice("h4", issuer="Bank X", customer="", doc_type="statement"))
        pend = cp.pending()
        names = {x["raw"] for x in pend}
        self.assertIn("ACME Inc.", names)                     # 两种写法归并为一条待建档
        self.assertIn("Umbrella Corp", names)
        self.assertIn("My Company Ltd", names)                # 客户侧也进队列
        self.assertNotIn("Bank X", names)                     # 流水不算对手方来源
        acme = next(x for x in pend if x["raw"] == "ACME Inc.")
        self.assertEqual(acme["count"], 2)
        self.assertEqual(acme["kind"], "vendor")
        # 建档后从队列消失，且别名写法也不再出现
        cp.register("ACME Inc.", by="tester", aliases=["ACME, Inc"])
        self.assertNotIn("ACME Inc.", {x["raw"] for x in cp.pending()})
        self.assertEqual(cp.summary()["parties"], 1)

    def test_pending_carries_candidates(self):
        cp.register("ACME Inc.", by="tester")
        db.save_invoice(_mk_invoice("h1", issuer="Acmee Inc", customer=""))
        item = next(x for x in cp.pending() if x["raw"] == "Acmee Inc")
        self.assertTrue(item["candidates"])
        self.assertEqual(item["candidates"][0]["name"], "ACME Inc.")

    def test_conflicting_alias_on_register_rejected(self):
        # 2026-08-03 自检：别名已归属他家时曾被 INSERT OR IGNORE **静默丢弃**（用户以为归并了）
        cp.register("Umbrella Corp", by="t", aliases=["Umbrella Holdings"])
        with self.assertRaises(ValueError) as cm:
            cp.register("Wayne Ltd", by="t", aliases=["Umbrella Holdings"])
        self.assertIn("已归属", str(cm.exception))
        self.assertIsNone(cp.resolve("Wayne Ltd"))          # 冲突 → 整笔建档不落库
        self.assertEqual(cp.resolve("Umbrella Holdings")["name"], "Umbrella Corp")

    def test_reactivate_conflicting_norm_rejected(self):
        # 归档后按同名建了新档，再复活旧档会出现两条 active 同 norm → resolve 不确定
        p1 = cp.register("ACME Inc.", by="t")
        cp.update_party(p1["id"], status="archived")
        cp.register("ACME", by="t")                         # 归档期间建新档（允许）
        with self.assertRaises(ValueError) as cm:
            cp.update_party(p1["id"], status="active")
        self.assertIn("无法启用", str(cm.exception))
        self.assertEqual(len(cp.list_parties()), 1)

    def test_self_kind_clears_own_company_from_queue(self):
        # 发票的"客户"常年是自家公司名，建档为本方主体后不再占着待建档队列
        db.save_invoice(_mk_invoice("h1", issuer="ACME Inc.", customer="My Company Ltd"))
        self.assertIn("My Company Ltd", {x["raw"] for x in cp.pending()})
        cp.register("My Company Ltd", kind=cp.KIND_SELF, by="t")
        self.assertNotIn("My Company Ltd", {x["raw"] for x in cp.pending()})
        self.assertEqual(cp.resolve("my company")["kind"], "self")

    def test_pending_reports_truncation(self):
        for i in range(5):
            db.save_invoice(_mk_invoice(f"h{i}", issuer=f"Vendor {i} Inc", customer=""))
        rows, total = cp.pending(limit=2, with_total=True)
        self.assertEqual((len(rows), total), (2, 5))         # 截断要报出来，不静默
        ov = cp.overview(limit=2)
        self.assertTrue(ov["summary"]["truncated"])
        self.assertEqual(ov["summary"]["pending"], 5)
        self.assertEqual(ov["summary"]["pending_shown"], 2)

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            cp.register("   ", by="tester")

    # ---- 多角色：同一实体可同时是 我方(self) + 供应商/客户（2026-08-17）----
    def test_register_multi_role(self):
        p = cp.register("云帆科技", kind="self,vendor", force=True)
        self.assertTrue(cp.has_role(p, "self") and cp.has_role(p, "vendor"))
        self.assertFalse(cp.has_role(p, "customer"))
        self.assertEqual(cp.parse_roles(p["kind"]), {"self", "vendor"})

    def test_add_and_remove_role(self):
        p = cp.register("蓝港", kind="self", force=True)          # 先建成我方
        p = cp.add_role(p["id"], "vendor")                        # 追加供应商角色
        self.assertTrue(cp.has_role(p, "self") and cp.has_role(p, "vendor"))
        p = cp.remove_role(p["id"], "self")                       # 去掉我方，仍是供应商
        self.assertEqual(cp.parse_roles(p["kind"]), {"vendor"})
        with self.assertRaises(ValueError):                       # 去到空 → 拒绝
            cp.remove_role(p["id"], "vendor")

    def test_both_and_single_compat(self):
        pb = cp.register("Acme", kind="both", force=True)         # both → vendor+customer
        self.assertEqual(cp.parse_roles(pb["kind"]), {"vendor", "customer"})
        ps = cp.register("Beta", kind="vendor", force=True)       # 旧单值仍工作
        self.assertEqual(cp.parse_roles(ps["kind"]), {"vendor"})

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            cp.register("Z", kind="weird", force=True)

    def test_self_candidates_detects_both_sided_unregistered(self):
        # 我方公司「Starlan Studio」：AR 里作开票方、AP 里作收票方 → 双向出现 → 侦测为 self 候选
        db.save_invoice(_mk_invoice("a1", issuer="Starlan Studio", customer="Meridian Corp", no="AR-1"))
        db.save_invoice(_mk_invoice("a2", issuer="Adobe Systems", customer="Starlan Studio", no="AP-1"))
        db.save_invoice(_mk_invoice("a3", issuer="Office Depot", customer="Nova Retail", no="AP-2"))
        names = {c["name"] for c in cp.self_candidates()}
        self.assertIn("Starlan Studio", names)      # 双向出现 → 候选
        self.assertNotIn("Adobe Systems", names)    # 只作开票方 → 不是
        self.assertNotIn("Meridian Corp", names)    # 只作收票方 → 不是
        # 已建档（任何角色）→ 不再打扰
        cp.register("Starlan Studio", kind=cp.KIND_SELF, by="t")
        self.assertNotIn("Starlan Studio", {c["name"] for c in cp.self_candidates()})


if __name__ == "__main__":
    unittest.main()
