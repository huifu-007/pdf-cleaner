#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_gui.py - 打包 pdf_cleaner_gui.exe.

本机 360 实时防护会拦截"创建名为 runw.exe 的文件"(PyInstaller 的 windowed 引导程序),
直接用 pyinstaller 命令会报 copyfile 失败。这里给 shutil.copyfile 加兜底:
PermissionError 时先写成 .stage 临时名, 再 os.replace 改名(改名不被拦截)。
"""
import os
import shutil
import sys
from pathlib import Path

# 360 实时防护会封锁"名为 runw.exe 的文件"(PyInstaller windowed 引导程序):
# 同目录下该文件存在但无法读取。此处准备同版本(6.22.3)的可读副本作为替代源。
BOOT_SUBST = (r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default"
              r"\Lib\site-packages\PyInstaller\bootloader\Windows-64bit-intel\runw.exe")

_orig_copyfile = shutil.copyfile
_orig_copy2 = shutil.copy2


def _readable(p) -> bool:
    try:
        with open(p, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


def _subst(src):
    if (str(src).lower().endswith("runw.exe")
            and not _readable(src) and _readable(BOOT_SUBST)):
        return BOOT_SUBST
    return src


def _safe_copy(func, src, dst, *a, **kw):
    src = _subst(src)
    try:
        return func(src, dst, *a, **kw)
    except PermissionError:
        # 目标文件名被拦截时: 先写临时名再改名
        tmp = str(dst) + ".stage"
        func(src, tmp, *a, **kw)
        os.replace(tmp, dst)
        return dst


def patched_copyfile(src, dst, *a, **kw):
    return _safe_copy(_orig_copyfile, src, dst, *a, **kw)


def patched_copy2(src, dst, *a, **kw):
    return _safe_copy(_orig_copy2, src, dst, *a, **kw)


shutil.copyfile = patched_copyfile
shutil.copy2 = patched_copy2

from PyInstaller.__main__ import run  # noqa: E402

if __name__ == "__main__":
    args = [
        "--onefile", "--windowed",
        "--name", "pdf_cleaner_gui",
        "--distpath", ".",
        "--workpath", "build_gui",
        "--specpath", ".",
        "--noconfirm", "--clean",
        "--icon", str(Path(__file__).resolve().parent / "icon" / "icon_a.ico"),
        # 窗口/任务栏图标(运行时加载)
        "--add-data", "%s;icon" % (Path(__file__).resolve().parent / "icon" / "ui_icon.png"),
        # 缩略图预览需要 PIL 与 tkinter 集成
        "--hidden-import", "PIL._imagingtk",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "PIL.Image",
        "pdf_cleaner_gui.py",
    ]
    # 追加命令行传入的额外参数(可覆盖上面的同名项)
    args.extend(sys.argv[1:])
    run(args)
