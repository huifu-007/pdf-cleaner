#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pdf_cleaner.py - 批量删除 PDF 中的图片与水印.

用法:
    python pdf_cleaner.py <文件或目录> [更多路径...] [-o 输出目录] [选项]

选项:
    -o, --output DIR     输出目录(默认: 输入同级 _cleaned 文件夹)
    --patterns "词1,词2" 追加水印匹配词, 支持中英文, 逗号/空格分隔
    --keep-images        只删文字水印, 保留图片
    --keep-watermarks    只删图片, 保留文字水印
    --keep-stamps        保留印章批注(默认删除 Stamp 批注)
    --max-run-len N      单条文字长度超过 N 视为正文不删(默认 60)

说明:
    - 无损编辑: 保留矢量文字与版面, 删除图片引用 + 水印文字指令
    - 文字水印按显示内容匹配(Tj/TJ), 支持:
        字面量/十六进制字符串, UTF-16BE, GBK, 以及带 ToUnicode 的自定义编码字体
    - 图片(含图片水印/内联图片)从内容流与资源字典中移除
    - 默认另存到 _cleaned 目录, 绝不覆盖原文件
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

def _import_with_retry(name, retries=8, delay=1.2):
    """带重试的模块导入.

    360 等安全软件会短暂独占锁定刚写出的 DLL(单文件版解压出的 .pyd 尤其容易撞上),
    表现为 "DLL load failed"; 这里重试若干次, 等锁释放后自动恢复, 免得重开程序.
    """
    import importlib
    import time
    last = None
    for i in range(retries):
        try:
            return importlib.import_module(name)
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(delay)
    raise last


pikepdf = _import_with_retry("pikepdf")
from pikepdf import parse_content_stream, unparse_content_stream

DEFAULT_PATTERNS = [
    # 中文水印词
    "机密", "绝密", "秘密", "内部资料", "内部文件", "内部使用", "仅供内部",
    "受控文件", "受控", "样本", "样品", "样稿", "草稿", "审阅稿", "讨论稿",
    "禁止外传", "禁止外泄", "禁止复印", "严禁外传", "严禁外泄", "请勿外传",
    "不得外传", "仅供参考", "阅后即焚", "水印", "影印", "翻版", "盗版必究",
    # 英文水印词(忽略大小写)
    "confidential", "do not copy", "do not distribute", "internal use only",
    "not for distribution", "watermark", "sample", "draft", "specimen",
]

TEXT_OPS = ("Tj", "TJ", "'", '"')


# ---------------------------------------------------------------- 基础工具
# 注意: pikepdf 中间接引用取出的值是 Object 包装, isinstance(Dictionary) 会误判为 False,
# 因此一律用 try/except 鸭子类型访问.

def _get(o, key, default=None):
    try:
        return o.get(key, default)
    except Exception:
        return default


def _subtype(o):
    return str(_get(o, "/Subtype", ""))


def _items(o):
    try:
        return list(o.items())
    except Exception:
        return []


_OPNAME_CACHE = {}


def _opname(op) -> str:
    """pikepdf Operator -> 名字(如 'Tj'). 注意: 不能访问 .op 属性, 会抛 ValueError."""
    try:
        key = op
        cached = _OPNAME_CACHE.get(key)
        if cached is not None:
            return cached
    except Exception:
        key = None
    try:
        s = str(op)
    except Exception:
        s = repr(op)
    if key is not None:
        try:
            _OPNAME_CACHE[key] = s
        except Exception:
            pass
    return s


def _norm_name(obj) -> str:
    return str(obj).lstrip("/")


def _objkey(obj):
    try:
        return ("og", tuple(obj.objgen))
    except Exception:
        return ("id", id(obj))


def _page_dict(page):
    o = getattr(page, "obj", None)
    return o if o is not None else page


def _kb(n: int) -> str:
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1048576)
    return "%.0f KB" % (n / 1024)


# ---------------------------------------------------------------- 字符串解码

_ESC = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
        "(": "(", ")": ")", "\\": "\\"}


def _decode_literal(s: str) -> str:
    """解码 PDF 字面量字符串 (...) 的转义."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in _ESC:
                out.append(_ESC[nxt])
                i += 2
                continue
            m = re.match(r"[0-7]{1,3}", s[i + 1:i + 4])
            if m:
                out.append(chr(int(m.group(0), 8)))
                i += 1 + len(m.group(0))
                continue
            if nxt == "\n":
                i += 2
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _string_raws(obj):
    """pikepdf.String -> 候选原始字节列表(第 1 个为原始字节, 最可靠)."""
    try:
        b = bytes(obj)
        if isinstance(b, (bytes, bytearray)):
            return [bytes(b)]
    except Exception:
        pass
    s = str(obj)
    if len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        return [_decode_literal(s[1:-1]).encode("latin-1", "replace")]
    if len(s) >= 2 and s[0] == "<" and s[-1] == ">":
        h = re.sub(r"\s+", "", s[1:-1])
        try:
            return [bytes.fromhex(h)]
        except ValueError:
            return []
    # str() 返回的是值形态: 同时给出 latin-1 还原字节与 utf-8 字节两种候选
    raws = []
    try:
        raws.append(s.encode("latin-1"))
    except Exception:
        pass
    raws.append(s.encode("utf-8", "replace"))
    return raws


def _candidates(raw: bytes):
    """同一段字节的多种解码候选(UTF-16BE/UTF-8/GBK/latin-1)."""
    cands = []
    if len(raw) >= 2 and len(raw) % 2 == 0:
        try:
            cands.append(raw.decode("utf-16-be"))
        except Exception:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            cands.append(raw.decode(enc))
        except Exception:
            pass
    cands.append(raw.decode("latin-1", "replace"))
    return cands


def _collect_raw(operands):
    """每个字符串返回一组候选字节(组内第 1 个最接近原始字节, 避免重复解码)."""
    groups = []
    for o in operands:
        if isinstance(o, pikepdf.String):
            r = _string_raws(o)
            if r:
                groups.append(r)
        elif isinstance(o, pikepdf.Array):
            for item in o:
                if isinstance(item, pikepdf.String):
                    r = _string_raws(item)
                    if r:
                        groups.append(r)
    return groups


def _match_candidates(cands, patterns, max_len):
    if not cands:
        return False
    if max(len(c) for c in cands) > max_len:
        return False  # 长文本视为正文, 不删
    low = [c.lower() for c in cands]
    for p in patterns:
        pl = p.lower()
        for c in low:
            if pl in c:
                return True
    return False


# ---------------------------------------------------------------- ToUnicode 解析

def _parse_tounicode(data: bytes):
    """解析 ToUnicode CMap 流, 返回 {字符码: unicode}."""
    text = data.decode("latin-1", "replace")
    cmap = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for sm, dm in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                cmap[int(sm, 16)] = bytes.fromhex(dm).decode("utf-16-be", "replace")
            except Exception:
                pass
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for m in re.finditer(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]+>|\[[^\]]*\])", block):
            lo, hi, dst = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
            try:
                if dst.startswith("<"):
                    base = int(dst[1:-1], 16)
                    for c in range(lo, hi + 1):
                        if base + (c - lo) > 0xFFFF:
                            break
                        cmap[c] = chr(base + (c - lo))
                else:
                    for i, it in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", dst)):
                        if lo + i > hi:
                            break
                        cmap[lo + i] = bytes.fromhex(it).decode("utf-16-be", "replace")
            except Exception:
                pass
    return cmap


def _decode_with_map(raw: bytes, cmap, two_byte):
    out = []
    if two_byte:
        if len(raw) % 2:
            raw = raw + b"\x00"
        for i in range(0, len(raw), 2):
            code = (raw[i] << 8) | raw[i + 1]
            out.append(cmap.get(code, ""))
    else:
        for b in raw:
            out.append(cmap.get(b, ""))
    return "".join(out)


# ---------------------------------------------------------------- 图形状态

def _mat_mul(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _mat_angle(m):
    import math
    return math.degrees(math.atan2(m[1], m[0]))


class GState:
    """内容流图形状态: 变换矩阵 / 字体 / 字号 / 填充色."""
    __slots__ = ("ctm", "tm", "tlm", "font", "size", "fill", "leading", "stack")

    def __init__(self):
        self.ctm = (1, 0, 0, 1, 0, 0)
        self.tm = (1, 0, 0, 1, 0, 0)
        self.tlm = (1, 0, 0, 1, 0, 0)
        self.font = None
        self.size = 0.0
        self.fill = (0.0, 0.0, 0.0)
        self.leading = 0.0
        self.stack = []

    def push(self):
        self.stack.append((self.ctm, self.tm, self.tlm, self.font,
                           self.size, self.fill, self.leading))

    def pop(self):
        if self.stack:
            (self.ctm, self.tm, self.tlm, self.font,
             self.size, self.fill, self.leading) = self.stack.pop()

    def copy(self):
        """状态快照(消费者拿到的是副本, 避免物化后被后续指令改写)."""
        s = GState()
        s.ctm = self.ctm
        s.tm = self.tm
        s.tlm = self.tlm
        s.font = self.font
        s.size = self.size
        s.fill = self.fill
        s.leading = self.leading
        return s


# ---------------------------------------------------------------- 清理引擎

class Config:
    def __init__(self, patterns, remove_images, remove_watermarks,
                 drop_stamps, max_run_len):
        self.patterns = patterns
        self.remove_images = remove_images
        self.remove_watermarks = remove_watermarks
        self.drop_stamps = drop_stamps
        self.max_run_len = max_run_len


class Cleaner:
    def __init__(self, pdf, cfg, select=None, collect=False):
        self.pdf = pdf
        self.cfg = cfg
        # select: {"texts": set(block_key), "images": set(ident)} 精确按界面勾选删除
        self.select = select
        # collect: True 时只扫描内容、不做任何修改(供界面列出可勾选条目)
        self.collect = collect
        self.entries = []         # 对外暴露的条目(文字块 + 图片)
        self.raw_entries = []     # 细粒度原始条目(每条绘制指令)
        self.blocks = []          # 文字块(同页同样式且绘制相邻的连续文字)
        self.ident_block = {}     # ident -> block_key
        self.wm_idents = set()    # 命中水印的原始条目 ident
        self.stats = {"image_refs": 0, "watermark_ops": 0, "stamp_annots": 0,
                      "inline_images": 0, "image_resources": 0,
                      "parse_errors": 0, "full_page_images": 0}
        self.res_dicts = {}
        self.seen_forms = set()
        self.font_cache = {}
        self._font_obj_cache = {}
        self._scope_seq = 0
        self._page_idx = 0
        self._page_w = self._page_h = 0.0
        self._collecting = False

    # ---- 入口
    def run(self):
        if self.collect:
            self._collect_pass()
            self.blocks = self._build_blocks(self.raw_entries)
            self.entries = self._public_entries()
            return
        # 第一遍: 收集条目并分块(一键模式据此判断水印, 选择模式据此映射块)
        self._collecting = True
        self._collect_pass()
        self._collecting = False
        self.blocks = self._build_blocks(self.raw_entries)
        for b in self.blocks:
            for ident in b["idents"]:
                self.ident_block[ident] = b["ident"]
        if self.select is None:
            for b in self.blocks:
                if self._block_is_watermark(b):
                    self.wm_idents.update(b["idents"])
        # 第二遍: 执行删除
        for page_idx, page in enumerate(self.pdf.pages):
            self.seen_forms = set()
            self._scope_seq = 0
            self._page_idx = page_idx
            self.process_page(page, page_idx)
        if self.select is not None:
            self.finalize_resources()
        elif self.cfg.remove_images:
            for res in list(self.res_dicts.values()):
                self.cleanup_resources(res)

    def _collect_pass(self):
        for page_idx, page in enumerate(self.pdf.pages):
            self.seen_forms = set()
            self._scope_seq = 0
            self._page_idx = page_idx
            self.process_page(page, page_idx)

    # ---- 文字块聚合: 同一页、同样式(字号/角度/颜色)、且绘制位置相邻的连续文字
    def _build_blocks(self, raw_entries):
        blocks = []
        cur = None
        for e in raw_entries:
            if e["kind"] != "text":
                continue
            if cur is not None and self._same_style(cur, e) and self._near(cur, e):
                cur["idents"].append(e["ident"])
                cur["text"] += e["text"]
                cur["last_x"], cur["last_y"] = e["x"], e["y"]
                cur["count"] = len(cur["idents"])
            else:
                cur = {
                    "ident": "b%d.%d" % (e["page"], len(blocks)),
                    "kind": "text", "page": e["page"], "text": e["text"],
                    "idents": [e["ident"]], "count": 1,
                    "x": e["x"], "y": e["y"], "last_x": e["x"], "last_y": e["y"],
                    "size": e["size"], "angle": e["angle"], "color": e["color"],
                }
                blocks.append(cur)
        return blocks

    @staticmethod
    def _same_style(a, b):
        return (a["page"] == b["page"]
                and abs(a["size"] - b["size"]) < 0.01
                and abs(a["angle"] - b["angle"]) < 0.01
                and all(abs(x - y) < 0.01 for x, y in zip(a["color"], b["color"])))

    @staticmethod
    def _near(a, b):
        # 相邻绘制字符的间距不应超过字号量级, 否则视为不同文本块
        limit = max(a["size"], 1.0) * 1.5
        return ((b["x"] - a["last_x"]) ** 2 + (b["y"] - a["last_y"]) ** 2) ** 0.5 <= limit

    def _block_is_watermark(self, block):
        t = (block["text"] or "").strip()
        if not t:
            return False
        if len(t) > self.cfg.max_run_len:
            return False  # 长文本视为正文, 不删
        low = t.lower()
        for p in self.cfg.patterns:
            if p.lower() in low:
                return True
        return False

    def _public_entries(self):
        """对外条目: 文字块(整块可选) + 图片(单张可选)."""
        out = list(self.blocks)
        for e in self.raw_entries:
            if e["kind"] == "image":
                out.append(e)
        return out


    def process_page(self, page, page_idx):
        pd = _page_dict(page)
        try:
            mb = page.mediabox
            self._page_w, self._page_h = float(mb.width), float(mb.height)
        except Exception:
            self._page_w = self._page_h = 0.0
        page_res = _get(pd, "/Resources")
        chain = [page_res] if page_res is not None else []
        for r in chain:
            self._register_res(r)
        self.walk_contents(pd, _get(pd, "/Contents"), chain, page_idx, "p")
        xo = _get(page_res, "/XObject")
        if xo is not None:
            self.walk_forms(xo, chain, page_idx)
        self.handle_annots(pd, chain, page_idx)

    def _register_res(self, res):
        if res is None:
            return
        try:
            self.res_dicts[_objkey(res)] = res
        except Exception:
            pass

    def walk_forms(self, xo, chain, page_idx):
        for _, x in _items(xo):
            if _subtype(x) == "/Form":
                self.walk_form(x, chain, page_idx)

    def walk_form(self, form, chain, page_idx):
        try:
            k = _objkey(form)
        except Exception:
            k = ("id", id(form))
        if k in self.seen_forms:
            return
        self.seen_forms.add(k)
        fres = _get(form, "/Resources")
        chain2 = ([fres] + chain) if fres is not None else chain
        for r in chain2:
            self._register_res(r)
        self._scope_seq += 1
        self.walk_contents(form, _get(form, "/Contents"), chain2,
                           page_idx, "f%d" % self._scope_seq)
        xo = _get(fres, "/XObject")
        if xo is not None:
            self.walk_forms(xo, chain2, page_idx)

    def handle_annots(self, pd, chain, page_idx):
        annots = _get(pd, "/Annots")
        if annots is None:
            return
        try:
            n = len(annots)
        except Exception:
            return
        if self.cfg.drop_stamps:
            for i in range(n - 1, -1, -1):
                try:
                    if _subtype(annots[i]) == "/Stamp":
                        del annots[i]
                        self.stats["stamp_annots"] += 1
                except Exception:
                    continue
        try:
            annot_list = list(annots)
        except Exception:
            return
        for a in annot_list:
            ap = _get(a, "/AP")
            if ap is None:
                continue
            for state in ("/N", "/R", "/D"):
                node = _get(ap, state)
                if node is None:
                    continue
                if _subtype(node) == "/Form":
                    self.walk_form(node, chain, page_idx)
                else:
                    for _, sub in _items(node):
                        if _subtype(sub) == "/Form":
                            self.walk_form(sub, chain, page_idx)

    # ---- 资源/字体查询
    def _image_names(self, chain):
        names = set()
        for res in chain:
            xo = _get(res, "/XObject")
            for k, v in _items(xo):
                if _subtype(v) == "/Image":
                    names.add(_norm_name(k))
        return names

    def _resolve_font(self, name, chain):
        # 同一份 PDF 里字体对象是共享的, 按名字缓存即可
        if name in self._font_obj_cache:
            return self._font_obj_cache[name]
        font = None
        for res in chain:
            fonts = _get(res, "/Font")
            if fonts is None:
                continue
            for key in (name, "/" + name):
                try:
                    if key in fonts:
                        font = fonts[key]
                        break
                except Exception:
                    continue
            if font is not None:
                break
        self._font_obj_cache[name] = font
        return font

    def _font_info(self, font):
        """(cmap, two_byte) 带缓存."""
        key = _objkey(font)
        if key in self.font_cache:
            return self.font_cache[key]
        cmap, two_byte = None, False
        try:
            enc = _get(font, "/Encoding")
            if enc is not None and str(enc) in ("/Identity-H", "/Identity-V"):
                two_byte = True
            elif _subtype(font) == "/Type0":
                two_byte = True
            tu = _get(font, "/ToUnicode")
            if tu is not None:
                cmap = _parse_tounicode(tu.read_bytes()) or None
        except Exception:
            pass
        self.font_cache[key] = (cmap, two_byte)
        return cmap, two_byte

    def _decode_with_font(self, groups, font_name, chain):
        if font_name is None or not groups:
            return ""
        font = self._resolve_font(_norm_name(font_name), chain)
        if font is None:
            return ""
        cmap, two_byte = self._font_info(font)
        if not cmap:
            return ""
        return "".join(_decode_with_map(g[0], cmap, two_byte) for g in groups)

    def _best_text(self, groups, font_name, chain):
        """取最像正文的一份解码结果(优先字体 ToUnicode)."""
        dec = self._decode_with_font(groups, font_name, chain)
        if dec and dec.strip():
            return dec
        best, best_score = "", -1.0
        for g in groups:
            for cand in _candidates(g[0]):
                if not cand.strip():
                    continue
                printable = sum(1 for ch in cand if ch.isprintable())
                score = printable / max(len(cand), 1)
                if score > 0.8 and (score > best_score or len(cand) > len(best)):
                    best, best_score = cand, score
        return best

    # ---- 内容流处理
    def _iter_content(self, contents):
        if contents is None:
            return
        try:
            instrs = list(parse_content_stream(contents))
        except Exception:
            instrs = None
        if instrs is not None:
            for x in instrs:
                yield x
            return
        # 退路: 多段内容流数组但类型包装不同
        try:
            subs = list(contents)
        except Exception:
            return
        for sub in subs:
            yield from self._iter_content(sub)

    # 会影响图形状态的指令(其余指令跳过状态更新, 省掉大量函数调用)
    _STATE_OPS = frozenset((
        "q", "Q", "cm", "BT", "Tf", "TL", "Tm", "Td", "TD", "T*",
        "rg", "g", "k", "sc", "scn", "cs",
    ))
    # 会产生可勾选条目的指令(只有这些指令才需要图形状态快照)
    _ENTRY_OPS = frozenset(("Tj", "TJ", "'", '"', "Do"))

    def _iter_ops(self, contents, need_state=True):
        """产出 (指令序号, 操作数, 操作符, 图形状态).

        扫描与删除共用此遍历, 指令序号完全一致.
        need_state=False 时(删除阶段)不维护也不快照图形状态, 显著提速.
        """
        state = GState()
        idx = 0
        for operands, op in self._iter_content(contents):
            name = _opname(op)
            if need_state and name in self._ENTRY_OPS:
                yield idx, operands, op, state.copy()
            else:
                yield idx, operands, op, None
            idx += 1
            if name in self._STATE_OPS:
                self._apply_state(operands, name, state)

    def _apply_state(self, operands, name, state):
        try:
            if name == "q":
                state.push()
            elif name == "Q":
                state.pop()
            elif name == "cm" and len(operands) >= 6:
                state.ctm = _mat_mul(tuple(float(o) for o in operands[:6]), state.ctm)
            elif name == "BT":
                state.tm = state.tlm = (1, 0, 0, 1, 0, 0)
            elif name == "Tf" and len(operands) >= 2:
                state.font = operands[0]
                state.size = float(operands[1])
            elif name == "TL" and operands:
                state.leading = float(operands[0])
            elif name == "Tm" and len(operands) >= 6:
                state.tm = state.tlm = tuple(float(o) for o in operands[:6])
            elif name in ("Td", "TD") and len(operands) >= 2:
                tx, ty = float(operands[0]), float(operands[1])
                if name == "TD":
                    state.leading = -ty
                state.tlm = _mat_mul((1, 0, 0, 1, tx, ty), state.tlm)
                state.tm = state.tlm
            elif name == "T*":
                state.tlm = _mat_mul((1, 0, 0, 1, 0, -state.leading), state.tlm)
                state.tm = state.tlm
            elif name in ("rg", "sc", "scn"):
                vals = [float(o) for o in operands]
                if len(vals) == 1:
                    state.fill = (vals[0], vals[0], vals[0])
                elif len(vals) >= 3:
                    state.fill = (vals[0], vals[1], vals[2])
            elif name == "g" and operands:
                v = float(operands[0])
                state.fill = (v, v, v)
            elif name == "k" and len(operands) >= 4:
                c, m_, y_, k_ = [float(o) for o in operands[:4]]
                state.fill = ((1 - c) * (1 - k_), (1 - m_) * (1 - k_), (1 - y_) * (1 - k_))
        except Exception:
            pass

    def _find_xobject(self, name, chain):
        for res in chain:
            xo = _get(res, "/XObject")
            for k, v in _items(xo):
                if _norm_name(k) == name:
                    return v
        return None

    def _collect_entry(self, ident, operands, name, state, chain):
        if name == "Do" and operands:
            xname = _norm_name(operands[0])
            xobj = self._find_xobject(xname, chain)
            if xobj is None or _subtype(xobj) != "/Image":
                return
            try:
                w = int(_get(xobj, "/Width", 0) or 0)
                h = int(_get(xobj, "/Height", 0) or 0)
            except Exception:
                w = h = 0
            ctm = state.ctm
            # 图像绘制时 CTM 缩放已是用户空间(点)值, 显示尺寸直接取 a/d 分量
            dw, dh = abs(ctm[0]), abs(ctm[3])
            cover = 0.0
            if self._page_w > 0 and self._page_h > 0:
                cover = (dw * dh) / (self._page_w * self._page_h)
            if cover > 0.6:
                self.stats["full_page_images"] += 1
            self.raw_entries.append({
                "ident": ident, "kind": "image", "page": self._page_idx,
                "text": "", "x": round(ctm[4], 1), "y": round(ctm[5], 1),
                "pw": w, "ph": h, "dw": round(dw, 1), "dh": round(dh, 1),
                "angle": round(_mat_angle(ctm), 1), "color": state.fill,
                "size": 0.0, "xname": xname, "cover": round(cover, 3),
                "count": 1, "idents": [ident],
            })
        elif name in TEXT_OPS:
            raws = _collect_raw(operands)
            if not raws:
                return
            text = self._best_text(raws, state.font, chain)
            full = _mat_mul(state.tm, state.ctm)
            self.raw_entries.append({
                "ident": ident, "kind": "text", "page": self._page_idx,
                "text": text, "x": round(full[4], 1), "y": round(full[5], 1),
                "pw": 0, "ph": 0, "dw": 0.0, "dh": 0.0,
                "angle": round(_mat_angle(state.tm) + _mat_angle(state.ctm), 1),
                "color": state.fill,
                "size": round(state.size * abs(state.ctm[0]), 1), "xname": "",
                "cover": 0.0, "count": 1, "idents": [ident],
            })

    def walk_contents(self, owner, contents, chain, page_idx, scope):
        if contents is None:
            return
        need_state = self.collect or self._collecting
        img_names = self._image_names(chain) if (self.cfg.remove_images or self.select) else set()
        out, changed, skip_inline = [], False, False
        state = GState() if need_state else None
        idx = 0
        try:
            for operands, op in self._iter_content(contents):
                name = _opname(op)
                ident = "%d.%s.%d" % (page_idx, scope, idx)
                idx += 1
                if skip_inline:
                    if name == "EI":
                        skip_inline = False
                    continue
                if name == "BI":
                    skip_inline = True
                    changed = True
                    self.stats["inline_images"] += 1
                    continue
                if need_state:
                    st = state.copy() if name in self._ENTRY_OPS else None
                    self._collect_entry(ident, operands, name, st, chain)
                    out.append((operands, op))
                    if name in self._STATE_OPS:
                        self._apply_state(operands, name, state)
                    continue
                drop = False
                if self.select is not None:
                    # 手动选择: 文字按块勾选, 图片按单张勾选
                    if name == "Do" and operands:
                        if ident in self.select.get("images", ()):
                            drop = True
                            self.stats["image_refs"] += 1
                    elif name in TEXT_OPS:
                        bkey = self.ident_block.get(ident)
                        if bkey is not None and bkey in self.select.get("texts", ()):
                            drop = True
                            self.stats["watermark_ops"] += 1
                else:
                    # 一键模式: 文字按块判定(水印可能逐字绘制), 图片按开关全删
                    if name == "Do" and operands:
                        if _norm_name(operands[0]) in img_names:
                            drop = True
                            self.stats["image_refs"] += 1
                    elif self.cfg.remove_watermarks and name in TEXT_OPS:
                        if ident in self.wm_idents:
                            drop = True
                            self.stats["watermark_ops"] += 1
                if drop:
                    changed = True
                else:
                    out.append((operands, op))
        except Exception:
            self.stats["parse_errors"] += 1
            return
        if not need_state and not changed:
            return
        try:
            data = unparse_content_stream(out)
            owner["/Contents"] = self.pdf.make_stream(data)
        except Exception:
            self.stats["parse_errors"] += 1

    def cleanup_resources(self, res):
        xo = _get(res, "/XObject")
        for k, v in _items(xo):
            if _subtype(v) == "/Image":
                try:
                    del xo[k]
                    self.stats["image_resources"] += 1
                except Exception:
                    pass

    def finalize_resources(self):
        """选择模式: 只移除"已无任何引用"的图片资源条目(保留仍在使用的图片)."""
        live = set()
        for page in self.pdf.pages:
            pd = _page_dict(page)
            self._collect_live_refs(pd, _get(pd, "/Contents"),
                                    [_get(pd, "/Resources")], live, set())
        for res in list(self.res_dicts.values()):
            xo = _get(res, "/XObject")
            for k, v in _items(xo):
                if _subtype(v) == "/Image" and _norm_name(k) not in live:
                    try:
                        del xo[k]
                        self.stats["image_resources"] += 1
                    except Exception:
                        pass

    def _collect_live_refs(self, owner, contents, chain, live, seen):
        if contents is None:
            return
        try:
            for _, operands, op, _st in self._iter_ops(contents):
                if _opname(op) == "Do" and operands:
                    live.add(_norm_name(operands[0]))
        except Exception:
            pass
        res = _get(owner, "/Resources")
        chain2 = ([res] + list(chain)) if res is not None else list(chain)
        xo = _get(res, "/XObject")
        for _, x in _items(xo):
            if _subtype(x) == "/Form":
                try:
                    k = _objkey(x)
                except Exception:
                    continue
                if k in seen:
                    continue
                seen.add(k)
                self._collect_live_refs(x, _get(x, "/Contents"), chain2, live, seen)


# ---------------------------------------------------------------- 驱动

def clean_pdf(src: Path, dst: Path, cfg: Config) -> dict:
    pdf = pikepdf.open(str(src))
    try:
        cleaner = Cleaner(pdf, cfg)
        cleaner.run()
        try:
            pdf.remove_unreferenced_resources()
        except Exception:
            pass
        dst.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(str(dst))
        return cleaner.stats
    finally:
        pdf.close()


def scan_pdf(src, cfg: Config = None) -> list:
    """扫描 PDF, 返回可勾选条目列表.

    文字按"块"聚合(同页同样式且绘制相邻的连续文字, 逐字绘制的水印会合成一块),
    每块带唯一 ident(块 key)与块内 idents; 图片每张一条.
    条目含位置、字号、角度、颜色、图片占页比例等, 供界面展示与勾选.
    """
    pdf = pikepdf.open(str(src))
    try:
        if cfg is None:
            cfg = Config(DEFAULT_PATTERNS, True, True, True, 60)
        cleaner = Cleaner(pdf, cfg, collect=True)
        cleaner.run()
        return cleaner.entries
    finally:
        pdf.close()


def clean_pdf_by_ids(src: Path, dst: Path, cfg: Config, sel: dict) -> dict:
    """按界面勾选精确删除.

    sel = {"texts": set(block_key), "images": set(ident)}
    文字按块勾选(块 key 来自 scan_pdf 返回的 ident), 图片按单张勾选.
    """
    pdf = pikepdf.open(str(src))
    try:
        cleaner = Cleaner(pdf, cfg, select=sel)
        cleaner.run()
        try:
            pdf.remove_unreferenced_resources()
        except Exception:
            pass
        dst.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(str(dst))
        return cleaner.stats
    finally:
        pdf.close()


def build_jobs(inputs, output):
    jobs = []
    for raw in inputs:
        p = Path(raw)
        if not p.exists():
            print("[跳过] 路径不存在: %s" % raw)
            continue
        p = p.resolve()
        if p.is_dir():
            out_base = Path(output).resolve() if output else p / "_cleaned"
            for f in sorted(p.rglob("*")):
                if not f.is_file() or f.suffix.lower() != ".pdf":
                    continue
                rel = f.relative_to(p)
                if "_cleaned" in rel.parts:
                    continue
                if out_base in f.parents:
                    continue
                jobs.append((f, out_base / rel))
        else:
            if p.suffix.lower() != ".pdf":
                print("[跳过] 非 PDF 文件: %s" % raw)
                continue
            out_base = Path(output).resolve() if output else p.parent / "_cleaned"
            jobs.append((p, out_base / p.name))
    seen, uniq = set(), []
    for s, d in jobs:
        key = str(s).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append((s, d))
    return uniq


def _pause():
    """双击/拖拽启动时等待按键, 避免窗口一闪即退."""
    try:
        input("\n按回车键退出...")
    except Exception:
        pass


QUICK_GUIDE = """==================================================
  PDF 批量清理工具 - 去图片 / 去水印
==================================================
【用法一】把 PDF 文件或文件夹直接拖到本程序图标上
【用法二】命令行运行:
    pdf_cleaner <文件或目录> [-o 输出目录] [选项]

常用选项:
    --patterns "水印词"  追加自定义水印匹配词
    --keep-images        只删水印, 保留图片
    --keep-watermarks    只删图片, 保留水印
    --keep-stamps        保留印章批注

输出默认放到 <输入>/_cleaned 文件夹, 不会覆盖原文件。
详细帮助: pdf_cleaner --help
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_help()
        print("\n[参数错误] %s" % message)
        _pause()
        sys.exit(2)


def main(argv=None):
    # 打包成的 exe 不继承 PYTHONUTF8/PYTHONIOENCODING 环境变量,
    # 重定向或管道输出时统一按 UTF-8 写出; 真实控制台不干预(WriteConsoleW 正常显示中文)
    for _s in (sys.stdout, sys.stderr):
        try:
            if not _s.isatty():
                _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if not argv and len(sys.argv) <= 1:
        # 无参数启动 = 双击或资源管理器启动, 显示快速指南而不是闪退
        print(QUICK_GUIDE)
        _pause()
        return 1
    ap = _Parser(
        prog=Path(sys.argv[0]).name or "pdf_cleaner",
        description="批量删除 PDF 中的图片与水印(文字/图片水印), 无损编辑, 不覆盖原文件")
    ap.add_argument("inputs", nargs="+", help="PDF 文件或目录(可多个)")
    ap.add_argument("-o", "--output", help="输出目录(默认: <输入>/_cleaned)")
    ap.add_argument("--patterns", default="", help="追加水印匹配词, 逗号/空格分隔")
    ap.add_argument("--keep-images", action="store_true", help="只删文字水印, 保留图片")
    ap.add_argument("--keep-watermarks", action="store_true", help="只删图片, 保留文字水印")
    ap.add_argument("--keep-stamps", action="store_true", help="保留印章批注")
    ap.add_argument("--max-run-len", type=int, default=60,
                    help="单条文字超过此长度视为正文(默认 60)")
    args = ap.parse_args(argv)

    patterns = list(DEFAULT_PATTERNS)
    if args.patterns.strip():
        patterns += [w for w in re.split(r"[,，、;；\s]+", args.patterns.strip()) if w]
    cfg = Config(patterns=patterns,
                 remove_images=not args.keep_images,
                 remove_watermarks=not args.keep_watermarks,
                 drop_stamps=not args.keep_stamps,
                 max_run_len=args.max_run_len)

    jobs = build_jobs(args.inputs, args.output)
    if not jobs:
        print("没有找到可处理的 PDF 文件")
        _pause()
        return 1

    ok = fail = 0
    tot = {"image_refs": 0, "watermark_ops": 0, "stamp_annots": 0, "inline_images": 0}
    for i, (src, dst) in enumerate(jobs, 1):
        if dst.resolve() == src.resolve():
            dst = dst.with_name(dst.stem + "_clean" + dst.suffix)
        print("[%d/%d] %s" % (i, len(jobs), src))
        try:
            st = clean_pdf(src, dst, cfg)
        except pikepdf.PasswordError:
            print("      [跳过] 文件已加密, 需要密码")
            fail += 1
            continue
        except Exception as e:
            print("      [失败] %s: %s" % (type(e).__name__, e))
            fail += 1
            continue
        ok += 1
        for k in tot:
            tot[k] += st.get(k, 0)
        s_in = src.stat().st_size
        s_out = dst.stat().st_size if dst.exists() else 0
        line = ("      图片引用 %d 处, 水印文字 %d 处, 印章批注 %d 个"
                % (st["image_refs"], st["watermark_ops"], st["stamp_annots"]))
        if st["inline_images"]:
            line += ", 内联图片 %d 个" % st["inline_images"]
        if st["parse_errors"]:
            line += ", 解析异常 %d 处" % st["parse_errors"]
        print(line)
        if st.get("full_page_images"):
            print("      [提示] %d 张图片几乎铺满整页(疑似扫描件正文), 删图会连正文一起丢失; "
                  "只想去水印请加 --keep-images" % st["full_page_images"])
        print("      -> %s  (%s -> %s)" % (dst, _kb(s_in), _kb(s_out)))

    print("\n完成: 成功 %d 个, 失败/跳过 %d 个" % (ok, fail))
    print("合计: 图片引用 %d 处, 水印文字 %d 处, 印章批注 %d 个"
          % (tot["image_refs"], tot["watermark_ops"], tot["stamp_annots"]))
    if ok == 0:
        _pause()  # 全部失败/跳过时也停住, 免得拖拽误操作后窗口秒关
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
