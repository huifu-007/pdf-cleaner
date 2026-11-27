#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_form_image.py - 构造"图片包在 Form XObject 内"的 PDF, 验证扫描与缩略图."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pikepdf

import pdf_cleaner as pc
import pdf_cleaner_gui as gui

OUT = ROOT / "test_pdfs" / "form_image.pdf"


def build():
    """页面 Do 一个 Form, 图片藏在 Form 的资源里."""
    pdf = pikepdf.new()
    # 4x2 红色渐变图片
    px = bytearray()
    for y in range(2):
        for x in range(4):
            px += bytes((255 - x * 40, 40 + y * 60, 120))
    img = pikepdf.Stream(pdf, bytes(px))
    img.Type = pikepdf.Name.Image
    img.Subtype = pikepdf.Name.Image
    img.Width = 4
    img.Height = 2
    img.ColorSpace = pikepdf.Name.DeviceRGB
    img.BitsPerComponent = 8
    # Form 内容流里引用该图片(Form XObject 是字典, 内容在 /Contents)
    form = pikepdf.Dictionary(
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=pikepdf.Array([0, 0, 120, 80]),
        Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=img)),
    )
    form["/Contents"] = pikepdf.Stream(pdf, b"q 80 0 0 40 20 20 cm /Im0 Do Q")
    # 页面引用 Form
    page = pdf.add_blank_page(page_size=(200, 200))
    page.obj["/Resources"] = pikepdf.Dictionary(
        XObject=pikepdf.Dictionary(Fm0=form))
    page.obj["/Contents"] = pikepdf.Stream(pdf, b"q 1 0 0 1 0 0 cm /Fm0 Do Q")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(str(OUT))
    pdf.close()
    print("已生成", OUT.name)


def main():
    build()
    ents = pc.scan_pdf(OUT)
    imgs = [e for e in ents if e["kind"] == "image"]
    print("扫描: 图片 %d, 文字 %d" % (len(imgs), len([e for e in ents if e["kind"] == "text"])))
    ok = True
    checks = [("扫到 Form 内图片", len(imgs) == 1)]
    for name, cond in checks:
        print("%-22s %s" % (name, "PASS" if cond else "FAIL"))
        ok = ok and cond
    if not imgs:
        return 1

    e = imgs[0]
    print("图片条目: xname=%s 页=%d 像素 %dx%d" % (e.get("xname"), e["page"], e["pw"], e["ph"]))

    # 缩略图: 递归进 Form 查找
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    sel = gui.Selector(root, [(str(OUT), OUT.name)], pc.DEFAULT_PATTERNS)
    state = {"n": 0}

    def check():
        state["n"] += 1
        if not sel.img_items and state["n"] < 30:
            root.after(300, check)
            return
        img = None
        if sel.img_items:
            img = sel._thumb(sel.img_items[0][0], sel.img_items[0][1])
        print("缩略图解码 ->", img)
        results = []
        for name, cond in [
            ("Form 内图片缩略图可解码", img is not None),
            ("尺寸正确", img is not None and img.size == (4, 2)),
        ]:
            print("%-22s %s" % (name, "PASS" if cond else "FAIL"))
            results.append(cond)
        allok = all(results) and ok
        print("总体: %s" % ("全部通过" if allok else "存在失败"))
        root.destroy()
        return 0 if allok else 1

    root.after(400, check)
    root.mainloop()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
