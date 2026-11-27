#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_pdf_cleaner.py - 生成测试 PDF, 运行 pdf_cleaner.py, 验证清理效果."""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_DIR = ROOT / "test_pdfs"
OUT_DIR = ROOT / "test_out"
ASSETS = TEST_DIR / "assets"

CLEANER = ROOT / "pdf_cleaner.py"

BODY_TITLE = "技术服务合同"
BODY_LINES = [
    "甲方：某某科技有限公司",
    "乙方：某某信息技术有限公司",
    "双方经友好协商，就本事项达成如下协议：",
    "一、服务内容与期限",
    "二、费用及支付方式",
    "三、保密义务与知识产权归属",
    "四、违约责任与争议解决",
    "本合同一式两份，双方各执一份，自签署之日起生效。",
]

# (文件名, 正文标记, 应被清除的水印词, 源文件是否有图片)
CASES = [
    ("合同_带水印.pdf", ["技术服务合同", "违约责任"], ["机密", "CONFIDENTIAL", "内部资料"], True),
    ("扫描样稿_SAMPLE.pdf", ["产品宣传资料"], ["SAMPLE"], True),
    ("普通文档_对照.pdf", ["技术服务合同"], [], False),
]


def make_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    ttc = Path("C:/Windows/Fonts/simsun.ttc")
    if ttc.exists():
        try:
            pdfmetrics.registerFont(TTFont("simsun", str(ttc), subfontIndex=0))
            return "simsun"
        except Exception as e:
            print("[警告] simsun 注册失败, 改用 CID 字体: %s" % e)
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def make_image():
    from PIL import Image, ImageDraw
    ASSETS.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (480, 320), (245, 247, 250))
    d = ImageDraw.Draw(img)
    for x in range(0, 480, 40):
        d.line([(x, 0), (x, 320)], fill=(210, 220, 235), width=2)
    d.ellipse([80, 60, 400, 260], fill=(99, 102, 241), outline=(67, 56, 202), width=6)
    d.rectangle([140, 120, 340, 200], fill=(16, 185, 129))
    p = ASSETS / "demo.png"
    img.save(str(p))
    return p


def draw_body(c, font):
    from reportlab.lib.pagesizes import A4
    w, _ = A4
    c.setFont(font, 18)
    c.drawCentredString(w / 2, 780, BODY_TITLE)
    c.setFont(font, 11)
    y = 740
    for line in BODY_LINES:
        c.drawString(60, y, line)
        y -= 22


def gen_docs(font):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as rl_canvas
    img = make_image()

    # 1) 中文合同 + 图片 + 中英文水印(两页)
    p1 = TEST_DIR / "合同_带水印.pdf"
    c = rl_canvas.Canvas(str(p1), pagesize=A4)
    draw_body(c, font)
    c.drawImage(str(img), 60, 430, width=480, height=320)
    c.saveState()
    c.translate(300, 300)
    c.rotate(45)
    c.setFillGray(0.78)
    c.setFont(font, 42)
    c.drawCentredString(0, 0, "机密文件 禁止外传")
    c.restoreState()
    c.setFont("Helvetica", 8)
    c.setFillGray(0.6)
    c.drawRightString(560, 30, "CONFIDENTIAL")
    c.showPage()
    draw_body(c, font)
    c.drawImage(str(img), 300, 60, width=240, height=160)
    c.saveState()
    c.translate(300, 400)
    c.rotate(30)
    c.setFillGray(0.8)
    c.setFont(font, 30)
    c.drawCentredString(0, 0, "内部资料 请勿外传")
    c.restoreState()
    c.save()

    # 2) 整页扫描图 + SAMPLE 水印
    p2 = TEST_DIR / "扫描样稿_SAMPLE.pdf"
    c = rl_canvas.Canvas(str(p2), pagesize=A4)
    w, h = A4
    c.drawImage(str(img), 0, 0, width=w, height=h)
    c.setFont(font, 12)
    c.setFillGray(0.2)
    c.drawString(60, 780, "产品宣传资料")
    c.drawString(60, 760, "本页为示意内容")
    c.saveState()
    c.translate(w / 2, h / 2)
    c.rotate(45)
    c.setFillGray(0.7)
    c.setFont("Helvetica-Bold", 60)
    c.drawCentredString(0, 0, "SAMPLE")
    c.restoreState()
    c.save()

    # 3) 干净对照文档
    p3 = TEST_DIR / "普通文档_对照.pdf"
    c = rl_canvas.Canvas(str(p3), pagesize=A4)
    draw_body(c, font)
    c.save()

    # 4) 加密文档(应被优雅跳过)
    from pypdf import PdfReader, PdfWriter
    r = PdfReader(str(p3))
    w2 = PdfWriter()
    w2.append(r)
    w2.encrypt("123456")
    with open(TEST_DIR / "带密码的文档.pdf", "wb") as f:
        w2.write(f)
    return [p1, p2, p3, TEST_DIR / "带密码的文档.pdf"]


def run_cleaner():
    cmd = [sys.executable, str(CLEANER), str(TEST_DIR), "-o", str(OUT_DIR)]
    print("$ %s\n" % " ".join(cmd))
    r = subprocess.run(cmd)
    return r.returncode


def count_images(path):
    import pikepdf
    n = 0
    pdf = pikepdf.open(str(path))
    try:
        for page in pdf.pages:
            pd = _page_dict(page)
            res = pd.get("/Resources")
            if res is None:
                continue
            xo = res.get("/XObject")
            if xo is None:
                continue
            for _, v in xo.items():
                if str(v.get("/Subtype", "")) == "/Image":
                    n += 1
    finally:
        pdf.close()
    return n


def _page_dict(page):
    o = getattr(page, "obj", None)
    if o is not None:
        return o
    return page


def all_stream_bytes(path):
    import pikepdf
    data = []
    pdf = pikepdf.open(str(path))
    try:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    data.append(obj.read_bytes())
                except Exception:
                    pass
    finally:
        pdf.close()
    return data


def needles(s):
    """一个字符串在 PDF 流里可能出现的编码形态."""
    out = set()
    for enc in ("utf-16-be", "utf-8", "gb18030"):
        try:
            out.add(s.encode(enc).hex().upper().encode("ascii"))
        except Exception:
            pass
    out.add(s.encode("utf-8"))
    out.add(s.encode("gb18030"))
    return out


def verify():
    from pypdf import PdfReader
    rows = []
    all_pass = True
    for name, markers, wms, has_img in CASES:
        src = TEST_DIR / name
        dst = OUT_DIR / name
        if not dst.exists():
            rows.append((name, "FAIL", "输出文件缺失"))
            all_pass = False
            continue
        r = PdfReader(str(dst))
        text = "\n".join((p.extract_text() or "") for p in r.pages)
        r_src = PdfReader(str(src))
        pages_ok = len(r.pages) == len(r_src.pages)
        img_src = count_images(src)
        img_dst = count_images(dst)
        chunks = all_stream_bytes(dst)
        body_ok = any(m in text for m in markers) or any(
            nd in ch for m in markers for nd in needles(m) for ch in chunks)
        wm_ok = True
        for wm in wms:
            if wm in text or wm.lower() in text.lower():
                wm_ok = False
            if any(nd in ch for nd in needles(wm) for ch in chunks):
                wm_ok = False
        img_ok = (img_dst == 0) if has_img else True
        ok = pages_ok and body_ok and wm_ok and img_ok
        all_pass = all_pass and ok
        rows.append((name,
                     "PASS" if ok else "FAIL",
                     "页数 %d->%d %s | 图片 %d->%d %s | 正文 %s | 水印 %s"
                     % (len(r_src.pages), len(r.pages), "OK" if pages_ok else "NG",
                        img_src, img_dst, "OK" if img_ok else "NG",
                        "OK" if body_ok else "NG",
                        "OK" if wm_ok else ("清除" if not wms else "NG"))))

    enc = OUT_DIR / "带密码的文档.pdf"
    enc_ok = not enc.exists()
    rows.append(("带密码的文档.pdf", "PASS" if enc_ok else "FAIL",
                 "已加密, 预期跳过: %s" % ("符合" if enc_ok else "未跳过, 不符合")))

    print("\n========== 验证结果 ==========")
    for name, status, detail in rows:
        print("%-22s %-5s %s" % (name, status, detail))
    print("==============================")
    print("总体: %s" % ("全部通过" if all_pass and enc_ok else "存在失败项"))
    return 0 if (all_pass and enc_ok) else 1


def main():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    TEST_DIR.mkdir(parents=True)
    font = make_font()
    files = gen_docs(font)
    print("已生成测试文件(字体 %s):" % font)
    for f in files:
        print("  - %s (%d KB)" % (f.name, f.stat().st_size // 1024))
    print()
    rc = run_cleaner()
    print()
    # 注意: 加密文件被跳过时 cleaner 返回 1, 属预期, 以 verify() 结果为准
    rc2 = verify()
    return rc2


if __name__ == "__main__":
    sys.exit(main())
