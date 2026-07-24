"""命令行批处理：python process_cli.py <文件或目录...>"""
import sys, warnings; warnings.filterwarnings("ignore")
from pathlib import Path
from core import db
from extraction import pipeline

def collect(args):
    out = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out += [f for f in p.iterdir() if f.suffix.lower() in (".pdf",".png",".jpg",".jpeg")]
        elif p.exists():
            out.append(p)
    return out

if __name__ == "__main__":
    db.init_db()
    files = collect(sys.argv[1:])
    if not files:
        print("用法: python process_cli.py <文件或目录...>"); sys.exit(1)
    invs = []
    for f in files:
        for inv in pipeline.process_local(f):   # 一个文件可能解析出多张发票
            invs.append(inv)
            print(f"[OK] {inv.file_name} | {inv.parse_method} | risk={inv.risk_score} | "
                  f"invoice_no={inv.f('invoice_no').value} total={inv.f('total_due').value}")
    out = pipeline.export_excel(invs)
    print(f"\nExcel 导出: {out}")
