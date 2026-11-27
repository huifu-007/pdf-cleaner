#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pdf_cleaner_gui.py - pdf_cleaner 图形界面版(支持逐项勾选删除).

两种处理模式:
    一键清理   - 按内置水印词表自动识别并删除(适合常规批量)
    手动选择   - 先扫描出每个 PDF 的图片与文字, 逐项勾选后再删除(适合精挑)
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import hashlib
import pickle
import shutil
import tempfile
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pikepdf

import pdf_cleaner as pc

# 复用核心的字典访问助手(间接引用取出的值是 Object 包装, 不能用 isinstance 判断)
_get = pc._get
_items = pc._items
_norm_name = pc._norm_name
_subtype = pc._subtype
_objkey = pc._objkey

APP_TITLE = "PDF 批量清理工具  —  去图片 / 去水印"
SELFTEST_FILE = "selftest_result.txt"


def _fmt_color(c):
    try:
        r, g, b = [max(0.0, min(1.0, float(v))) for v in list(c)[:3]]
    except Exception:
        return ""
    if abs(r - g) < 0.06 and abs(g - b) < 0.06:
        return "灰" if r > 0.75 else ("深灰" if r > 0.4 else "黑")
    return "彩色"


def _short(s, n=38):
    s = (s or "").replace("\n", " ").replace("\r", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def _pdf_filter_chain(stream):
    """图像 XObject 的 filter 链."""
    filt = _get(stream, "/Filter")
    if filt is None:
        return []
    try:
        if isinstance(filt, pikepdf.Name):
            return [str(filt)]
        return [str(f) for f in filt]
    except Exception:
        return []


def _pdf_raw_data(stream, filters):
    """按 filter 链逐级解码, 遇图像专用 filter(DCT/JPX)即停.

    返回 (数据, 图像filter或None); 不支持时返回 (None, None).
    """
    import base64
    import zlib
    try:
        data = stream.read_raw_bytes()
    except Exception:
        try:
            data = stream.read_bytes()
        except Exception:
            return None, None
    for f in filters:
        try:
            if f == "/ASCII85Decode":
                data = base64.a85decode(data.strip(), adobe=True)
            elif f == "/FlateDecode":
                data = zlib.decompress(data)
            elif f == "/RunLengthDecode":
                out = bytearray()
                i = 0
                while i < len(data):
                    n = data[i]
                    i += 1
                    if n == 128:
                        break
                    if n < 128:
                        out += data[i:i + n + 1]
                        i += n + 1
                    else:
                        out += bytes([data[i]]) * (257 - n)
                        i += 1
                data = bytes(out)
            elif f in ("/DCTDecode", "/JPXDecode"):
                return data, f
            else:
                return None, None        # CCITTFax/JBIG2 等暂不支持
        except Exception:
            return None, None
    return data, None


_PIL_IMAGE = None
_PIL_IMAGETK = None


def _pil_image():
    """PIL.Image (带重试, 避免 DLL 短暂被锁时直接崩溃)."""
    global _PIL_IMAGE
    if _PIL_IMAGE is None:
        _PIL_IMAGE = pc._import_with_retry("PIL.Image")
    return _PIL_IMAGE


def _pil_imagetk():
    global _PIL_IMAGETK
    if _PIL_IMAGETK is None:
        _PIL_IMAGETK = pc._import_with_retry("PIL.ImageTk")
    return _PIL_IMAGETK


def _decode_pdf_image(stream):
    """把 PDF 图像 XObject 解码为 PIL Image; 不支持时返回 None."""
    Image = _pil_image()
    import io
    try:
        filters = _pdf_filter_chain(stream)
        raw, imgfilter = _pdf_raw_data(stream, filters)
        if raw is None:
            return None
        if imgfilter in ("/DCTDecode", "/JPXDecode"):
            return Image.open(io.BytesIO(raw)).convert("RGB")
        w = int(_get(stream, "/Width", 0) or 0)
        h = int(_get(stream, "/Height", 0) or 0)
        bpc = int(_get(stream, "/BitsPerComponent", 8) or 8)
        cs = _get(stream, "/ColorSpace")
        if bpc == 8 and w and h:
            if cs == "/DeviceRGB":
                return Image.frombytes("RGB", (w, h), raw)
            if cs == "/DeviceGray":
                return Image.frombytes("L", (w, h), raw)
            if cs == "/DeviceCMYK":
                return Image.frombytes("CMYK", (w, h), raw).convert("RGB")
            try:
                if isinstance(cs, pikepdf.Array) and str(cs[0]) == "/Indexed":
                    base = str(cs[1]) if len(cs) > 2 else "/DeviceRGB"
                    hival = int(cs[2]) if len(cs) > 3 else 255
                    lookup = cs[3] if len(cs) > 3 else cs[2]
                    pal = (lookup.read_bytes() if isinstance(lookup, pikepdf.Stream)
                           else bytes(lookup))
                    ncomp = 4 if base == "/DeviceCMYK" else 3
                    flat = []
                    for i in range(hival + 1):
                        flat.extend(pal[i * ncomp:(i + 1) * ncomp])
                    flat = flat + [0] * (768 - len(flat))
                    img = Image.frombytes("P", (w, h), raw[:w * h])
                    img.putpalette(flat)
                    return img.convert("RGB")
            except Exception:
                return None
        if bpc == 1 and cs == "/DeviceGray" and w and h:
            stride = (w + 7) // 8
            out = bytearray(w * h)
            for y in range(h):
                row = raw[y * stride:(y + 1) * stride]
                for x in range(w):
                    bit = (row[x >> 3] >> (7 - (x & 7))) & 1
                    out[y * w + x] = 0 if bit else 255
            return Image.frombytes("L", (w, h), bytes(out))
        return None
    except Exception:
        return None


class Selector(tk.Toplevel):
    """条目勾选窗口: 图片页签显示缩略图可直接点选, 文字页签列出文本块."""

    TH = 132          # 缩略图边长

    def __init__(self, master, files, patterns, on_done=None):
        super().__init__(master)
        self.title("选择要删除的内容")
        self.geometry("1020x700")
        self.minsize(820, 520)
        self.files = list(files)
        self.patterns = patterns
        self.on_done = on_done
        self.text_items = []      # [(path, entry)] 文字块
        self.img_items = []       # [(path, entry)] 图片
        self.selected = set()     # {(path, ident)}
        self.tab = "image"
        self.q = queue.Queue()
        self._pdfs = {}
        self._thumbs = {}
        self._photos = []
        self._hits = []
        self._img_items = []
        self._img_fpr = {}        # (path, ident) -> 内容指纹
        self._img_groups = {}     # 指纹 -> [(path, ident)...]
        self._text_view = []
        self._build()
        self.after(100, self._poll)
        threading.Thread(target=self._scan, daemon=True).start()

    # ---------------------------------------------------------------- UI
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(8, 4))
        self.status = ttk.Label(top, text="正在扫描 PDF…")
        self.status.pack(side="left")
        self.prog = ttk.Progressbar(top, mode="determinate", length=220)
        self.prog.pack(side="right")

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=4)
        ttk.Label(bar, text="过滤:").pack(side="left")
        self.filter_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.filter_var, width=24)
        ent.pack(side="left", padx=4)
        ent.bind("<KeyRelease>", lambda e: self._refresh())
        ttk.Button(bar, text="全选(可见)", command=lambda: self._set_all(True)).pack(side="left", padx=2)
        ttk.Button(bar, text="全不选(可见)", command=lambda: self._set_all(False)).pack(side="left", padx=2)
        ttk.Button(bar, text="反选(可见)", command=self._invert).pack(side="left", padx=2)
        ttk.Button(bar, text="按水印词预选", command=self._preselect).pack(side="left", padx=2)
        ttk.Button(bar, text="清空全部勾选", command=self._clear_all).pack(side="left", padx=2)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=4)
        self.nb = nb

        self.tab_img = ttk.Frame(nb)
        nb.add(self.tab_img, text="图片")
        cvf = ttk.Frame(self.tab_img)
        cvf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cvf, background="#f4f5f7", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        csb = ttk.Scrollbar(cvf, orient="vertical", command=self.canvas.yview)
        csb.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=csb.set)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

        self.tab_txt = ttk.Frame(nb)
        nb.add(self.tab_txt, text="文字")
        self.listbox = tk.Listbox(self.tab_txt, selectmode="browse", activestyle="none",
                                  font=("Consolas", 9))
        self.listbox.pack(side="left", fill="both", expand=True)
        tsb = ttk.Scrollbar(self.tab_txt, orient="vertical", command=self.listbox.yview)
        tsb.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=tsb.set)
        self.listbox.bind("<Button-1>", self._on_click)
        nb.bind("<<NotebookTabChanged>>", self._on_tab)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=(4, 8))
        self.count = ttk.Label(bottom, text="已选 0 项")
        self.count.pack(side="left")
        ttk.Button(bottom, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(bottom, text="确定(只删除所选)", command=self._confirm).pack(side="right", padx=4)

    # ---------------------------------------------------------------- 扫描
    def _cache_file(self, path):
        """扫描结果缓存文件(按 路径+大小+修改时间 定位)."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = "%s|%d|%d" % (os.path.abspath(path), st.st_size, int(st.st_mtime))
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:20]
        d = os.path.join(tempfile.gettempdir(), "pdf_cleaner_cache")
        return os.path.join(d, h + ".pkl")

    def _load_cache(self, path):
        f = self._cache_file(path)
        if not f or not os.path.exists(f):
            return None
        try:
            with open(f, "rb") as fh:
                data = pickle.load(fh)
            return data if isinstance(data, list) else None
        except Exception:
            return None

    def _save_cache(self, path, entries):
        f = self._cache_file(path)
        if not f:
            return
        try:
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "wb") as fh:
                pickle.dump(entries, fh, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            pass

    def _scan(self):
        """多文件并行扫描; 单文件命中缓存则直接读缓存."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        total = len(self.files)
        self.q.put(("progress", (0, total, "准备中")))

        def scan_one(item):
            i, (path, name) = item
            cached = self._load_cache(path)
            if cached is not None:
                return i, path, name, cached, True
            try:
                entries = pc.scan_pdf(path)
            except Exception as e:
                entries = []
                self.q.put(("log", "扫描失败 %s: %s" % (name, e)))
            self._save_cache(path, entries)
            return i, path, name, entries, False

        workers = min(4, max(1, total))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(scan_one, (i, f)) for i, f in enumerate(self.files)]
            for fut in as_completed(futs):
                i, path, name, entries, cached = fut.result()
                done += 1
                self.q.put(("file", (path, name, entries)))
                self.q.put(("progress", (done, total, name + (" (缓存)" if cached else ""))))
        self.q.put(("done", None))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "progress":
                    i, total, name = payload
                    self.prog["value"] = i
                    self.status.configure(text="正在扫描 %d/%d  %s" % (i, total, name))
                elif kind == "file":
                    path, name, entries = payload
                    for e in entries:
                        if e["kind"] == "image":
                            self.img_items.append((path, e))
                        else:
                            self.text_items.append((path, e))
                    self._refresh()
                elif kind == "log":
                    self.status.configure(text=payload)
                elif kind == "done":
                    self.prog["value"] = len(self.files)
                    self.status.configure(text="扫描完成: 图片 %d 张, 文字 %d 块"
                                          % (len(self.img_items), len(self.text_items)))
                    self._preselect_text()
        except queue.Empty:
            pass
        self.after(120, self._poll)

    # ---------------------------------------------------------------- 渲染
    def _match(self, path, e):
        f = self.filter_var.get().strip().lower()
        if not f:
            return True
        hay = ("%s %s" % (os.path.basename(path), e.get("text", ""))).lower()
        return f in hay

    def _visible_items(self):
        src = self.img_items if self.tab == "image" else self.text_items
        return [(p, e) for (p, e) in src if self._match(p, e)]

    def _refresh(self):
        self._render_images()
        self._render_texts()
        self._update_count()

    def _open_pdf(self, path):
        doc = self._pdfs.get(path)
        if doc is None:
            doc = pikepdf.open(path)
            self._pdfs[path] = doc
        return doc

    def _search_xobj(self, res, xname, seen=None, depth=0):
        """在资源树里递归查找 XObject(图片常被包在 Form XObject 里)."""
        if res is None or depth > 6:
            return None
        if seen is None:
            seen = set()
        xo = _get(res, "/XObject")
        if xo is None:
            return None
        for k, v in _items(xo):
            if _norm_name(k) == xname:
                return v
        for _, v in _items(xo):
            if _subtype(v) == "/Form":
                try:
                    key = _objkey(v)
                except Exception:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                got = self._search_xobj(_get(v, "/Resources"), xname, seen, depth + 1)
                if got is not None:
                    return got
        return None

    def _thumb(self, path, e):
        # 同一 PDF 内同名 XObject 即同一张图, 按 xname 缓存 -> 219 页同一个 logo 只解码一次
        xname = e.get("xname", "")
        tkey = (path, xname)
        if tkey in self._thumbs:
            origin = self._thumbs[tkey]
        else:
            origin = None
            try:
                doc = self._open_pdf(path)
                page = doc.pages[e["page"]]
                res = _get(page.obj, "/Resources")
                xobj = self._search_xobj(res, xname)
                if xobj is not None:
                    origin = _decode_pdf_image(xobj)
            except Exception:
                origin = None
            self._thumbs[tkey] = origin
        if origin is None:
            return None
        # 内容指纹(基于原始像素): 同一对象复用 / 各页内嵌同一张图, 指纹都相同
        fkey = (path, e["ident"])
        if fkey not in self._img_fpr:
            self._img_fpr[fkey] = self._fingerprint(origin)
        try:
            small = origin.copy()
            small.thumbnail((self.TH - 12, self.TH - 12))
            return small
        except Exception:
            return origin

    @staticmethod
    def _fingerprint(img):
        """图片内容指纹: 相同像素的图片得到相同指纹."""
        try:
            h = hashlib.md5()
            h.update(img.mode.encode("ascii", "ignore"))
            h.update(("%dx%d" % img.size).encode("ascii"))
            h.update(img.tobytes())
            return h.hexdigest()
        except Exception:
            return None

    def _build_img_groups(self, items):
        """按内容指纹把相同图片归为一组(跨页/跨文件都算重复)."""
        groups = {}
        for path, e in items:
            key = (path, e["ident"])
            fpr = self._img_fpr.get(key)
            if fpr is None:
                continue
            groups.setdefault(fpr, []).append(key)
        self._img_groups = groups

    def _group_of(self, path, ident):
        fpr = self._img_fpr.get((path, ident))
        return self._img_groups.get(fpr) or [(path, ident)]

    def _render_images(self):
        self.canvas.delete("all")
        self._photos = []
        self._hits = []
        items = [(p, e) for (p, e) in self.img_items if self._match(p, e)]
        if not items:
            self.canvas.create_text(20, 20, text="(没有图片条目)", fill="#8a8f99", anchor="nw")
            return
        self._img_items = items
        self._build_img_groups(items)
        self._render_img_batch(0)

    def _render_img_batch(self, start):
        """分批渲染缩略图, 批次之间让出事件循环, 避免界面卡住."""
        ImageTk = _pil_imagetk()
        items = self._img_items
        th = self.TH
        cw = max(self.canvas.winfo_width(), 640)
        cols = max(1, (cw - 24) // (th + 16))
        batch = 24
        end = min(start + batch, len(items))
        for i in range(start, end):
            path, e = items[i]
            col, row = i % cols, i // cols
            x = 12 + col * (th + 16)
            y = 12 + row * (th + 34)
            img = self._thumb(path, e)
            if img is not None:
                photo = ImageTk.PhotoImage(img)
                self._photos.append(photo)
                self.canvas.create_image(x, y, image=photo, anchor="nw")
            else:
                self.canvas.create_rectangle(x, y, x + th, y + th, outline="#c9ccd1", fill="#e7e9ee")
                self.canvas.create_text(x + th / 2, y + th / 2, text="无法预览", fill="#8a8f99")
            picked = (path, e["ident"]) in self.selected
            self.canvas.create_rectangle(x - 3, y - 3, x + th + 3, y + th + 3,
                                         outline="#e5484d" if picked else "#c9ccd1",
                                         width=3 if picked else 1)
            grp = self._group_of(path, e["ident"])
            tag = (" ×%d" % len(grp)) if len(grp) > 1 else ""
            self.canvas.create_text(x + th / 2, y + th + 11,
                                    text="P%d  %dx%d px%s" % (e["page"] + 1, e["pw"], e["ph"], tag),
                                    fill="#333")
            self._hits.append((x, y, th, th, path, e["ident"]))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if end < len(items):
            self.after(1, lambda: self._render_img_batch(end))

    def _render_texts(self):
        self.listbox.delete(0, "end")
        self._text_view = []
        lines = []
        last_file = None
        for path, e in [(p, e) for (p, e) in self.text_items if self._match(p, e)]:
            if path != last_file:
                last_file = path
                head = "── " + os.path.basename(path) + " "
                lines.append(head + "─" * max(4, 58 - len(head)))
                self._text_view.append(None)
            mark = "[x]" if (path, e["ident"]) in self.selected else "[ ]"
            lines.append("  %s 文字 P%-3d (%5.0f,%5.0f) %5.1fpt %3.0f° %-3s  %s (%d字)"
                         % (mark, e["page"] + 1, e["x"], e["y"], e["size"], e["angle"],
                            _fmt_color(e["color"]), _short(e.get("text", ""), 44),
                            e.get("count", 1)))
            self._text_view.append((path, e["ident"]))
        if lines:
            self.listbox.insert("end", *lines)

    def _on_canvas_click(self, event):
        for (x, y, w, h, path, ident) in self._hits:
            if x <= event.x <= x + w and y <= event.y <= y + h:
                key = (path, ident)
                group = self._group_of(path, ident)
                picked = key in self.selected
                for k in group:
                    if picked:
                        self.selected.discard(k)
                    else:
                        self.selected.add(k)
                self._render_images()
                self._update_count()
                self.status.configure(text="%s %d 张相同图片%s" % (
                    "取消选中" if picked else "已选中", len(group),
                    "（重复图片已联动选中）" if len(group) > 1 else ""))
                return

    def _on_click(self, event):
        idx = self.listbox.nearest(event.y)
        if idx < 0 or idx >= len(self._text_view):
            return
        item = self._text_view[idx]
        if item is None:
            return
        path, ident = item
        key = (path, ident)
        if key in self.selected:
            self.selected.discard(key)
        else:
            self.selected.add(key)
        self._render_texts()
        self._update_count()

    def _on_wheel(self, event):
        self.canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def _on_tab(self, event=None):
        try:
            self.tab = "image" if self.nb.index(self.nb.select()) == 0 else "text"
        except Exception:
            self.tab = "image"
        self._update_count()

    # ---------------------------------------------------------------- 批量操作
    def _set_all(self, flag):
        for path, e in self._visible_items():
            key = (path, e["ident"])
            if flag:
                self.selected.add(key)
            else:
                self.selected.discard(key)
        self._refresh()

    def _invert(self):
        # 图片按"相同图片组"反转, 保证同组状态一致
        if self.tab == "image":
            done = set()
            for path, e in self._visible_items():
                key = (path, e["ident"])
                if key in done:
                    continue
                group = self._group_of(path, ident=e["ident"])
                for k in group:
                    done.add(k)
                picked = key in self.selected
                for k in group:
                    if picked:
                        self.selected.discard(k)
                    else:
                        self.selected.add(k)
            self._refresh()
            return
        for path, e in self._visible_items():
            key = (path, e["ident"])
            if key in self.selected:
                self.selected.discard(key)
            else:
                self.selected.add(key)
        self._refresh()

    def _clear_all(self):
        self.selected.clear()
        self._refresh()

    def _preselect(self):
        if self.tab != "text":
            messagebox.showinfo("提示", "按水印词预选请切换到「文字」页签", parent=self)
            return
        self._preselect_text()
        self._refresh()

    def _preselect_text(self):
        """按水印词表自动勾选文字块."""
        for path, e in self.text_items:
            t = (e.get("text") or "").lower()
            if not t.strip():
                continue
            for p in self.patterns:
                if p.lower() in t:
                    self.selected.add((path, e["ident"]))
                    break

    def _update_count(self):
        vis = len(self._visible_items())
        extra = ""
        if self.tab == "image":
            dup = sum(1 for g in self._img_groups.values() if len(g) > 1)
            if dup:
                extra = "；%d 组重复图片(选中一张即全选)" % dup
        self.count.configure(text="已选 %d 项 (当前页签可见 %d 项; 全选/反选仅作用于可见项)%s"
                             % (len(self.selected), vis, extra))

    # ---------------------------------------------------------------- 结果
    def result(self):
        out = {}
        for path, e in self.text_items + self.img_items:
            if (path, e["ident"]) in self.selected:
                bucket = out.setdefault(path, {"texts": set(), "images": set()})
                bucket["texts" if e["kind"] == "text" else "images"].add(e["ident"])
        return out

    def _confirm(self):
        res = self.result()
        n_text = sum(len(v["texts"]) for v in res.values())
        n_img = sum(len(v["images"]) for v in res.values())
        if n_text or n_img:
            msg = ("将删除: 文字块 %d 个, 图片 %d 张\n\n"
                   "提示: 若勾选的文字块中含正文, 正文会一并删除。" % (n_text, n_img))
            if not messagebox.askyesno("确认删除", msg, parent=self):
                return
        if self.on_done:
            self.on_done(res)
        self.destroy()

    def _cancel(self):
        if self.on_done:
            self.on_done(None)
        self.destroy()


class OverwriteDialog(tk.Toplevel):
    """输出文件已存在时的确认: 覆盖 / 跳过 / 取消, 可选"本次不再询问"."""

    def __init__(self, master, files):
        super().__init__(master)
        self.title("确认覆盖")
        self.action = "cancel"          # overwrite / skip / cancel
        self.remember = False
        self.resizable(False, False)
        self.transient(master)
        body = tk.Frame(self, padx=16, pady=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="以下输出文件已存在，处理时将被覆盖：").pack(anchor="w")
        lst = tk.Listbox(body, height=min(8, max(1, len(files))), width=68,
                         font=("Consolas", 9))
        for f in files[:20]:
            lst.insert("end", f)
        lst.pack(fill="x", pady=6)
        ttk.Label(body, text="提示：改选输出目录可保留原文件。").pack(anchor="w")
        self.chk = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="本次运行不再询问（直接覆盖）",
                        variable=self.chk).pack(anchor="w", pady=(8, 0))
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="覆盖并继续",
                   command=lambda: self._done("overwrite")).pack(side="right", padx=4)
        ttk.Button(bar, text="跳过这些文件",
                   command=lambda: self._done("skip")).pack(side="right", padx=4)
        ttk.Button(bar, text="取消本次处理",
                   command=lambda: self._done("cancel")).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", lambda: self._done("cancel"))
        self.bind("<Escape>", lambda e: self._done("cancel"))
        self.grab_set()

    def _done(self, action):
        self.action = action
        self.remember = bool(self.chk.get())
        self.destroy()


class App:
    def __init__(self, root, selftest=False):
        self.root = root
        self.selftest = selftest
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.entries = []          # 用户添加的路径(文件/目录)
        self.last_out = None
        self.worker = None
        self.selection = None      # {file_path: {"texts":set,"images":set}}
        self.mode = tk.StringVar(value="auto")
        self.always_overwrite = False
        self._skip_existing = set()
        self._build_ui()
        self.root.after(100, self._poll)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        root = self.root
        root.title(APP_TITLE)
        root.geometry(APP_TITLE and "780x720")
        root.minsize(700, 620)
        try:
            style = ttk.Style()
            if os.name == "nt" and "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:
            pass

        frm = ttk.LabelFrame(root, text="待处理文件 / 文件夹")
        frm.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.listbox = tk.Listbox(frm, height=7, selectmode="extended", activestyle="none")
        self.listbox.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.listbox.yview)
        sb.pack(side="right", fill="y", padx=(0, 6), pady=6)
        self.listbox.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=10)
        ttk.Button(btns, text="添加文件", command=self.add_files).pack(side="left", padx=2)
        ttk.Button(btns, text="添加文件夹", command=self.add_folder).pack(side="left", padx=2)
        ttk.Button(btns, text="移除选中", command=self.remove_selected).pack(side="left", padx=2)
        ttk.Button(btns, text="清空列表", command=self.clear_files).pack(side="left", padx=2)

        # 处理模式
        modef = ttk.LabelFrame(root, text="处理模式")
        modef.pack(fill="x", padx=10, pady=6)
        ttk.Radiobutton(modef, text="一键清理(按水印词表自动识别)",
                        variable=self.mode, value="auto").grid(row=0, column=0, sticky="w", padx=8, pady=3)
        ttk.Radiobutton(modef, text="手动选择(逐项勾选要删的图片/水印)",
                        variable=self.mode, value="manual").grid(row=0, column=1, sticky="w", padx=8, pady=3)
        ttk.Button(modef, text="选择要删除的内容…", command=self.open_selector
                   ).grid(row=0, column=2, sticky="e", padx=8, pady=3)
        modef.columnconfigure(1, weight=1)
        self.sel_info = ttk.Label(modef, text="未选择内容(手动模式下按当前勾选处理)")
        self.sel_info.grid(row=1, column=0, columnspan=3, sticky="w", padx=8)

        # 选项
        opt = ttk.LabelFrame(root, text="处理选项")
        opt.pack(fill="x", padx=10, pady=6)
        ttk.Label(opt, text="输出目录(留空 = 放到源目录的 _cleaned 文件夹):"
                ).grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.out_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.out_var).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(opt, text="选择…", command=self.choose_out).grid(row=0, column=2, padx=4)
        ttk.Label(opt, text="追加水印词(逗号分隔):").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.pat_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.pat_var).grid(row=1, column=1, sticky="we", padx=4)
        self.keep_img = tk.BooleanVar(value=True)
        self.keep_wm = tk.BooleanVar(value=False)
        self.keep_stamp = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="删除所有图片(扫描件正文会一起丢失)", variable=self.keep_img
                        ).grid(row=2, column=0, sticky="w", padx=6)
        ttk.Checkbutton(opt, text="只删图片, 保留水印", variable=self.keep_wm
                        ).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Checkbutton(opt, text="保留印章批注", variable=self.keep_stamp
                        ).grid(row=2, column=2, sticky="w", padx=6)
        opt.columnconfigure(1, weight=1)

        act = ttk.Frame(root)
        act.pack(fill="x", padx=10, pady=4)
        self.start_btn = ttk.Button(act, text="开始处理", command=self.start)
        self.start_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(act, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=2)
        self.open_btn = ttk.Button(act, text="打开输出目录", command=self.open_out, state="disabled")
        self.open_btn.pack(side="left", padx=2)
        self.prog = ttk.Progressbar(act, mode="determinate", length=220)
        self.prog.pack(side="right", padx=6)
        self.status = ttk.Label(act, text="就绪")
        self.status.pack(side="right", padx=6)

        logf = ttk.LabelFrame(root, text="运行日志")
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log = tk.Text(logf, height=8, state="disabled", wrap="none")
        self.log.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        lsb.pack(side="right", fill="y", padx=(0, 6), pady=6)
        self.log.configure(yscrollcommand=lsb.set)

    # ---------------------------------------------------------------- 文件列表
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 PDF 文件",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        self.add_paths(list(paths))

    def add_folder(self):
        d = filedialog.askdirectory(title="选择文件夹")
        if d:
            self.add_paths([d])

    def add_paths(self, paths):
        for p in paths:
            if p and p not in self.entries:
                self.entries.append(p)
                tag = "[目录]" if os.path.isdir(p) else ""
                self.listbox.insert("end", "%s %s" % (tag, p))
        if paths and not self.selftest:
            self._append_log("已载入 %d 个路径" % len(paths))

    def remove_selected(self):
        for idx in sorted(self.listbox.curselection(), reverse=True):
            self.listbox.delete(idx)
            if 0 <= idx < len(self.entries):
                self.entries.pop(idx)

    def clear_files(self):
        self.entries = []
        self.listbox.delete(0, "end")
        self.selection = None
        self.sel_info.configure(text="未选择内容(手动模式下按当前勾选处理)")

    def choose_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_var.set(d)

    # ---------------------------------------------------------------- 选择器
    def _pdf_files(self):
        """展开待处理路径为具体 PDF 列表."""
        out = []
        for e in self.entries:
            p = Path(e)
            if p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file() and f.suffix.lower() == ".pdf" and "_cleaned" not in f.parts:
                        out.append(f)
            elif p.is_file() and p.suffix.lower() == ".pdf":
                out.append(p)
        return out

    def open_selector(self):
        files = self._pdf_files()
        if not files:
            messagebox.showwarning("提示", "请先添加 PDF 文件或文件夹")
            return
        patterns = list(pc.DEFAULT_PATTERNS)
        extra = self.pat_var.get().strip()
        if extra:
            patterns += [w for w in re.split(r"[,，、;；\s]+", extra) if w]
        self.selector = Selector(self.root, [(str(f), f.name) for f in files], patterns,
                                 on_done=self._on_selected)

    def _on_selected(self, result):
        if result is None:
            return
        self.selection = result
        n_text = sum(len(v["texts"]) for v in result.values())
        n_img = sum(len(v["images"]) for v in result.values())
        self.sel_info.configure(text="已选 %d 项(文字 %d / 图片 %d), 涉及 %d 个文件"
                                % (n_text + n_img, n_text, n_img, len(result)))
        self.mode.set("manual")
        self._append_log("已选择: 文字 %d 项, 图片 %d 项" % (n_text, n_img))

    # ---------------------------------------------------------------- 处理
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.entries:
            messagebox.showwarning("提示", "请先添加 PDF 文件或文件夹")
            return
        manual = self.mode.get() == "manual"
        if manual and not self.selection:
            messagebox.showwarning("提示", "手动模式需要先点击「选择要删除的内容…」")
            return
        patterns = list(pc.DEFAULT_PATTERNS)
        extra = self.pat_var.get().strip()
        if extra:
            patterns += [w for w in re.split(r"[,，、;；\s]+", extra) if w]
        cfg = pc.Config(patterns=patterns,
                        remove_images=not self.keep_img.get(),
                        remove_watermarks=not self.keep_wm.get(),
                        drop_stamps=not self.keep_stamp.get(),
                        max_run_len=60)
        out_dir = self.out_var.get().strip() or None
        # 防呆: 目标文件已存在时先确认, 避免误覆盖原文件
        self._skip_existing = set()
        try:
            preview = self._collect_jobs(out_dir)
            exists = [str(d) for _, d in preview if d.exists()]
        except Exception:
            exists = []
        if exists and not self.always_overwrite:
            dlg = OverwriteDialog(self.root, exists)
            if hasattr(dlg, "_w"):          # 真实对话框才需要等待关闭
                self.root.wait_window(dlg)
            act = dlg.action
            if act == "cancel":
                return
            if act == "skip":
                self._skip_existing = set(exists)
            if dlg.remember:
                self.always_overwrite = True
                self._append_log("已选择: 本次运行不再询问覆盖确认")
        self.stop_flag.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.prog["value"] = 0
        self._append_log("=" * 46)
        self._append_log("模式: %s" % ("手动选择" if manual else "一键清理"))
        self.worker = threading.Thread(target=self._worker,
                                       args=(out_dir, cfg, manual), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag.set()
        self._append_log("正在停止…")

    def _collect_jobs(self, out_dir):
        jobs = []
        for e in list(self.entries):
            p = Path(e)
            if p.is_dir():
                for f in sorted(p.rglob("*")):
                    if not f.is_file() or f.suffix.lower() != ".pdf":
                        continue
                    if "_cleaned" in f.parts:
                        continue
                    rel = f.relative_to(p)
                    dst = (Path(out_dir) / rel) if out_dir else (f.parent / "_cleaned" / f.name)
                    jobs.append((f, dst))
            elif p.is_file():
                if p.suffix.lower() != ".pdf":
                    self.q.put(("log", "[跳过] 非 PDF 文件: %s" % p))
                    continue
                dst = (Path(out_dir) / p.name) if out_dir else (p.parent / "_cleaned" / p.name)
                jobs.append((p, dst))
            else:
                self.q.put(("log", "[跳过] 路径不存在: %s" % p))
        used, final = set(), []
        for src, dst in jobs:
            try:
                if dst.resolve() == src.resolve():
                    dst = dst.with_name(dst.stem + "_clean" + dst.suffix)
            except Exception:
                pass
            d, i = dst, 1
            while str(d).lower() in used:
                d = dst.with_name("%s_%d%s" % (dst.stem, i, dst.suffix))
                i += 1
            used.add(str(d).lower())
            final.append((src, d))
        return final

    def _worker(self, out_dir, cfg, manual):
        jobs = self._collect_jobs(out_dir)
        if not jobs:
            self.q.put(("log", "没有找到 PDF 文件"))
            self.q.put(("done", {"ok": 0, "fail": 0, "skip": 0, "image_refs": 0,
                                 "watermark_ops": 0, "stamp_annots": 0, "total": 0}))
            return
        total = len(jobs)
        self.q.put(("log", "共 %d 个 PDF 待处理" % total))
        agg = {"ok": 0, "fail": 0, "skip": 0, "image_refs": 0,
               "watermark_ops": 0, "stamp_annots": 0, "total": total}
        for i, (src, dst) in enumerate(jobs, 1):
            if self.stop_flag.is_set():
                self.q.put(("log", "已被用户停止"))
                break
            sel = None
            if manual:
                sel = (self.selection or {}).get(str(src)) or {"texts": set(), "images": set()}
                if not sel["texts"] and not sel["images"]:
                    self.q.put(("log", "[跳过] %s - 未勾选任何内容" % src.name))
                    agg["skip"] += 1
                    self.q.put(("progress", (i, total, src.name)))
                    continue
            if str(dst) in self._skip_existing:
                self.q.put(("log", "[跳过] %s - 输出已存在，按选择跳过" % src.name))
                agg["skip"] += 1
                self.q.put(("progress", (i, total, src.name)))
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if manual:
                    st = pc.clean_pdf_by_ids(src, dst, cfg, sel)
                else:
                    st = pc.clean_pdf(src, dst, cfg)
            except pc.pikepdf.PasswordError:
                agg["skip"] += 1
                self.q.put(("log", "[跳过] %s - 已加密, 需要密码" % src.name))
            except Exception as e:
                agg["fail"] += 1
                self.q.put(("log", "[失败] %s - %s: %s" % (src.name, type(e).__name__, e)))
            else:
                agg["ok"] += 1
                for k in ("image_refs", "watermark_ops", "stamp_annots"):
                    agg[k] += st.get(k, 0)
                self.q.put(("log", "[%d/%d] %s -> 图片 %d 处, 水印 %d 处, 印章 %d 个  |  %s"
                           % (i, total, src.name, st["image_refs"], st["watermark_ops"],
                              st["stamp_annots"], dst)))
                self.q.put(("out", str(dst.parent)))
            self.q.put(("progress", (i, total, src.name)))
        self.q.put(("done", agg))

    # ---------------------------------------------------------------- 队列
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    idx, total, name = payload
                    self.prog["value"] = idx
                    self.status.configure(text="%d/%d  %s" % (idx, total, name))
                elif kind == "out":
                    self.last_out = payload
                elif kind == "done":
                    self._on_done(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_done(self, agg):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.prog["value"] = agg.get("total", 0)
        self.status.configure(text="完成")
        if self.last_out:
            self.open_btn.configure(state="normal")
        summary = ("成功 %d 个, 失败 %d 个, 跳过 %d 个 | 图片引用 %d 处, 水印文字 %d 处, 印章批注 %d 个"
                   % (agg.get("ok", 0), agg.get("fail", 0), agg.get("skip", 0),
                      agg.get("image_refs", 0), agg.get("watermark_ops", 0),
                      agg.get("stamp_annots", 0)))
        self._append_log("=" * 46)
        self._append_log(summary)
        if self.selftest:
            self._write_selftest(agg, summary)
            self.root.destroy()
            return
        if agg.get("ok"):
            messagebox.showinfo("完成", summary + "\n\n输出目录: %s" % (self.last_out or ""))
        else:
            messagebox.showwarning("完成", summary)

    def _write_selftest(self, agg, summary):
        try:
            log_text = self.log.get("1.0", "end")
            with open(SELFTEST_FILE, "w", encoding="utf-8") as f:
                f.write("SELFTEST OK\n")
                f.write(summary + "\n")
                f.write(log_text)
        except Exception:
            pass

    def open_out(self):
        if not self.last_out:
            return
        try:
            os.startfile(self.last_out)
        except Exception as e:
            messagebox.showerror("错误", "无法打开目录: %s" % e)

    # ---------------------------------------------------------------- 演示/自检
    def run_demo(self):
        """--demo: 载入示例目录并直接打开选择器(预览用)."""
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        sample = base / "test_pdfs"
        if not sample.exists():
            sample = Path(__file__).resolve().parent / "test_pdfs"
        if sample.exists():
            self.add_paths([str(sample)])
        self.root.after(400, self.open_selector)

    def run_selftest(self):
        """自检: 用独立输出目录并先清空, 避免触发覆盖确认, 也不碰用户目录."""
        self.root.withdraw()
        if not self.entries:
            self.add_paths([str(Path(__file__).resolve().parent / "test_pdfs")])
        out = Path(__file__).resolve().parent / "test_pdfs" / "_selftest_out"
        shutil.rmtree(out, ignore_errors=True)
        self.out_var.set(str(out))
        self.always_overwrite = True
        self.start()


def _set_window_icon(root):
    """给窗口/任务栏装上程序图标(与 exe 图标一致)."""
    try:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        for cand in (base / "icon" / "ui_icon.png", Path(__file__).resolve().parent / "icon" / "ui_icon.png"):
            if cand.exists():
                img = tk.PhotoImage(file=str(cand))
                root.iconphoto(True, img)
                root._icon_ref = img      # 保持引用, 否则会被回收
                return True
    except Exception:
        pass
    return False


def _write_started_flag():
    """写启动成功标记(供外部启动器检测"是否真的起来了")."""
    try:
        base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent)
        (base / "~pc_started.flag").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def main():
    argv = list(sys.argv[1:])
    selftest = "--selftest" in argv
    demo = "--demo" in argv
    argv = [a for a in argv if a not in ("--selftest", "--demo")]
    _write_started_flag()
    root = tk.Tk()
    _set_window_icon(root)
    app = App(root, selftest=selftest)
    if argv:
        app.add_paths(argv)
    if selftest:
        root.after(300, app.run_selftest)
    elif demo:
        root.after(300, app.run_demo)
    elif argv:
        root.after(200, app.start)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
