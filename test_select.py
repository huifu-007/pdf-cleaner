#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_select.py - 验证"扫描条目 + 按勾选精确删除"链路."""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pdf_cleaner as pc
import test_pdf_cleaner as T
from pypdf import PdfReader

OUT = ROOT / "test_select_out"


def img_count(path):
    import pikepdf
    pdf = pikepdf.open(str(path))
    n = 0
    try:
        for page in pdf.pages:
            res = page.obj.get("/Resources")
            xo = res.get("/XObject") if res else None
            if xo is None:
                continue
            for _, v in xo.items():
                if str(v.get("/Subtype", "")) == "/Image":
                    n += 1
    finally:
        pdf.close()
    return n


def text_of(path):
    r = PdfReader(str(path))
    return "\n".join((p.extract_text() or "") for p in r.pages)


def main():
    if T.TEST_DIR.exists():
        shutil.rmtree(T.TEST_DIR)
    if OUT.exists():
        shutil.rmtree(OUT)
    T.TEST_DIR.mkdir(parents=True)
    font = T.make_font()
    T.gen_docs(font)
    src = T.TEST_DIR / "合同_带水印.pdf"

    # 1) 扫描
    entries = pc.scan_pdf(src)
    imgs = [e for e in entries if e["kind"] == "image"]
    texts = [e for e in entries if e["kind"] == "text"]
    print("扫描结果: 共 %d 条 (图片 %d, 文字 %d)" % (len(entries), len(imgs), len(texts)))
    for e in entries:
        if e["kind"] == "image":
            print("  [图片] %s  页%d 位置(%.0f,%.0f) 像素%dx%d 显示%.0fx%.0f 角%.0f"
                  % (e["ident"], e["page"] + 1, e["x"], e["y"], e["pw"], e["ph"],
                     e["dw"], e["dh"], e["angle"]))
    for e in texts:
        t = e["text"].replace("\n", " ")[:34]
        if t.strip():
            print("  [文字] %s  页%d (%.0f,%.0f) %.0fpt %.0f° 色%s  %r"
                  % (e["ident"], e["page"] + 1, e["x"], e["y"], e["size"],
                     e["angle"], tuple(round(c, 2) for c in e["color"]), t))

    ok = True
    checks = []
    checks.append(("图片条目数=2", len(imgs) == 2))
    wm_texts = [e for e in texts if "机密" in e["text"]]
    checks.append(("识别出中文水印文字", len(wm_texts) >= 1))
    checks.append(("识别出 CONFIDENTIAL", any("CONFIDENTIAL" in e["text"] for e in texts)))
    checks.append(("识别出内部资料", any("内部资料" in e["text"] for e in texts)))
    checks.append(("正文被识别", any("技术服务合同" in e["text"] for e in texts)))
    checks.append(("水印角度被识别", any(abs(e["angle"] - 45) < 2 for e in texts)))

    # 2) 勾选: 只删"机密文件 禁止外传"这一条 + 第一张图片
    target_text = None
    for e in texts:
        if "机密" in e["text"]:
            target_text = e
            break
    if target_text is None:
        print("!! 未找到中文水印条目, 无法继续")
        return 1
    sel = {"texts": {target_text["ident"]}, "images": {imgs[0]["ident"]}}
    dst = OUT / "部分删除.pdf"
    st = pc.clean_pdf_by_ids(src, dst, pc.Config(pc.DEFAULT_PATTERNS, True, True, True, 60), sel)
    txt = text_of(dst)
    print("\n按勾选删除: stats=%s" % st)
    print("  删除后文字含'机密': %s | 含'CONFIDENTIAL': %s | 含正文: %s | 图片数: %d"
          % ("机密" in txt, "CONFIDENTIAL" in txt, "技术服务合同" in txt, img_count(dst)))

    checks.append(("选中的水印已删除", "机密" not in txt))
    checks.append(("未选中的水印保留", "CONFIDENTIAL" in txt))
    checks.append(("正文保留", "技术服务合同" in txt))
    checks.append(("只删了1张图片", img_count(dst) == 1))
    checks.append(("删除计数正确", st["watermark_ops"] == 1 and st["image_refs"] == 1))

    print("\n========== 结果 ==========")
    for name, res in checks:
        print("%-24s %s" % (name, "PASS" if res else "FAIL"))
        ok = ok and res
    print("总体: %s" % ("全部通过" if ok else "存在失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
