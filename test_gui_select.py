#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_gui_select.py - 驱动 GUI 选择器, 验证缩略图渲染与勾选作用域."""
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pdf_cleaner as pc
import pdf_cleaner_gui as gui
import test_pdf_cleaner as T


def main():
    if not T.TEST_DIR.exists():
        T.TEST_DIR.mkdir(parents=True)
        T.gen_docs(T.make_font())

    files = sorted(f for f in T.TEST_DIR.glob("*.pdf"))
    root = tk.Tk()
    root.withdraw()
    sel = gui.Selector(root, [(str(f), f.name) for f in files], pc.DEFAULT_PATTERNS)
    state = {"n": 0}

    def check():
        state["n"] += 1
        if len(sel.img_items) + len(sel.text_items) < 10:
            if state["n"] > 40:
                print("超时: 图片 %d, 文字 %d" % (len(sel.img_items), len(sel.text_items)))
                root.destroy()
                return
            root.after(400, check)
            return

        print("扫描: 图片 %d 张, 文字 %d 块" % (len(sel.img_items), len(sel.text_items)))
        ok = True
        checks = []

        # 1) 缩略图解码
        decoded = 0
        for path, e in sel.img_items:
            img = sel._thumb(path, e)
            if img is not None:
                decoded += 1
        print("缩略图解码成功: %d / %d" % (decoded, len(sel.img_items)))
        checks.append(("图片缩略图全部可解码", decoded == len(sel.img_items) and decoded > 0))

        # 2) Canvas 上确实画出了缩略图
        sel.tab = "image"
        sel._render_images()
        n_img_items = len([i for i in sel.canvas.find_all()
                           if sel.canvas.type(i) == "image"])
        print("Canvas 图片元素: %d" % n_img_items)
        checks.append(("画布已渲染缩略图", n_img_items == decoded and n_img_items > 0))

        # 3) 图片页签全选 -> 只选图片
        sel.selected.clear()
        sel.tab = "image"
        sel._set_all(True)
        r = sel.result()
        t = sum(len(v["texts"]) for v in r.values())
        i = sum(len(v["images"]) for v in r.values())
        print("[图片页签] 全选 -> 文字 %d, 图片 %d" % (t, i))
        checks.append(("图片页签全选只含图片", t == 0 and i == len(sel.img_items)))

        # 4) 文字页签全选 -> 只选文字
        sel.selected.clear()
        sel.tab = "text"
        sel._set_all(True)
        r = sel.result()
        t2 = sum(len(v["texts"]) for v in r.values())
        i2 = sum(len(v["images"]) for v in r.values())
        print("[文字页签] 全选 -> 文字 %d, 图片 %d" % (t2, i2))
        checks.append(("文字页签全选只含文字", i2 == 0 and t2 == len(sel.text_items)))

        # 5) 预选只影响文字
        sel.selected.clear()
        sel._preselect_text()
        r = sel.result()
        t3 = sum(len(v["texts"]) for v in r.values())
        i3 = sum(len(v["images"]) for v in r.values())
        print("[预选] -> 文字 %d, 图片 %d" % (t3, i3))
        checks.append(("预选只勾文字水印", i3 == 0 and t3 > 0))

        # 6) 点击缩略图可切换勾选
        sel.selected.clear()
        sel.tab = "image"
        sel._render_images()
        if sel._hits:
            x, y, w, h, path, ident = sel._hits[0]
            sel._on_canvas_click(type("E", (), {"x": x + w // 2, "y": y + h // 2})())
            picked = (path, ident) in sel.selected
            print("点击缩略图后选中: %s" % picked)
            checks.append(("点击缩略图可勾选", picked))
        else:
            checks.append(("点击缩略图可勾选", False))

        # 7) 文字列表渲染
        sel.tab = "text"
        sel._render_texts()
        nlines = sel.listbox.size()
        print("文字列表行数: %d" % nlines)
        checks.append(("文字列表已渲染", nlines > 5))

        # 8) 重复图片联动: 选一张 -> 同组全选
        sel.selected.clear()
        sel.tab = "image"
        sel._render_images()
        groups = {f: g for f, g in sel._img_groups.items() if len(g) > 1}
        print("\n图片分组: %d 组, 其中重复组 %d 个" % (len(sel._img_groups), len(groups)))
        for f, g in list(groups.items())[:3]:
            print("   重复组 %s... 共 %d 张" % (f[:8], len(g)))
        checks.append(("识别出重复图片组", len(groups) >= 1))
        if groups:
            g = list(groups.values())[0]
            path, ident = g[0]
            # 用组内第一张的命中矩形点击
            for (x, y, w, h, p2, id2) in sel._hits:
                if (p2, id2) == (path, ident):
                    sel._on_canvas_click(type("E", (), {"x": x + 2, "y": y + 2})())
                    break
            r = sel.result()
            n_img = sum(len(v["images"]) for v in r.values())
            print("点击组内一张后选中图片数: %d (该组 %d 张)" % (n_img, len(g)))
            checks.append(("选一张联动选中整组", n_img == len(g)))
            # 再点一次应整组取消
            for (x, y, w, h, p2, id2) in sel._hits:
                if (p2, id2) == (path, ident):
                    sel._on_canvas_click(type("E", (), {"x": x + 2, "y": y + 2})())
                    break
            r = sel.result()
            n_img2 = sum(len(v["images"]) for v in r.values())
            print("再次点击后选中图片数: %d" % n_img2)
            checks.append(("再点一次整组取消", n_img2 == 0))

        for name, cond in checks:
            print("%-26s %s" % (name, "PASS" if cond else "FAIL"))
            ok = ok and cond
        print("总体: %s" % ("全部通过" if ok else "存在失败"))
        root.destroy()
        return 0 if ok else 1

    root.after(500, check)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
