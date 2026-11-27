#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_overwrite.py - 覆盖确认对话框三态: 覆盖 / 跳过 / 取消."""
import shutil
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pdf_cleaner_gui as gui

OUT = ROOT / "test_pdfs" / "_ovr_out"


def make_app():
    root = tk.Tk()
    root.withdraw()
    app = gui.App(root)
    app.add_paths([str(ROOT / "test_pdfs" / "合同_带水印.pdf")])
    app.out_var.set(str(OUT))
    return root, app


def fake_dialog(action, remember=False):
    class FakeDlg:
        def __init__(self, master, files):
            self.action = action
            self.remember = remember
            self.files = files

        def destroy(self):
            pass
    return FakeDlg


def main():
    ok = True
    checks = []
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "合同_带水印.pdf").write_bytes(b"x")   # 预置已存在文件

    # --- 分支1: 取消 -> 不启动处理 ---
    root, app = make_app()
    gui.OverwriteDialog = fake_dialog("cancel")
    app.start()
    started = app.worker is not None and app.worker.is_alive()
    checks.append(("选择取消时不启动处理", not started))
    root.destroy()

    # --- 分支2: 跳过 -> 记录待跳过文件, 处理照常启动 ---
    root, app = make_app()
    gui.OverwriteDialog = fake_dialog("skip")
    app.start()
    checks.append(("选择跳过时记录待跳过文件", len(app._skip_existing) == 1))
    checks.append(("选择跳过时仍启动处理", app.worker is not None))
    root.destroy()

    # --- 分支3: 覆盖 -> 不跳过, 且勾"不再询问"后状态生效 ---
    root, app = make_app()
    gui.OverwriteDialog = fake_dialog("overwrite", remember=True)
    app.start()
    checks.append(("选择覆盖时不跳过任何文件", len(app._skip_existing) == 0))
    checks.append(("勾选后记住不再询问", app.always_overwrite is True))
    root.destroy()

    # --- 分支4: always_overwrite=True 时不再弹对话框 ---
    root, app = make_app()
    called = {"n": 0}

    def counting(action, remember=False):
        called["n"] += 1
        return fake_dialog(action, remember)

    gui.OverwriteDialog = counting
    app.always_overwrite = True
    app.start()
    checks.append(("已记住时不再弹确认框", called["n"] == 0))
    root.destroy()

    shutil.rmtree(OUT, ignore_errors=True)
    for name, cond in checks:
        print("%-26s %s" % (name, "PASS" if cond else "FAIL"))
        ok = ok and cond
    print("总体: %s" % ("全部通过" if ok else "存在失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
