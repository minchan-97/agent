"""
docedit.py — 문서를 '고친 새 파일'로 만든다. 원본은 절대 덮어쓰지 않는다.

  apply(kind, data, ops, name) → {"data", "ext", "kind", "changes", "notes"}

ops 는 LLM 이 만든 수정 지시 목록이지만, 여기서는 LLM 을 믿지 않는다.
  · 정해진 동작(op)만 받는다. 모르는 동작은 '못 함'으로 기록하고 넘어간다
  · 찾기·바꾸기는 문서에 실제로 있는 글일 때만 한다 (없으면 '못 찾음')
  · 바꾼 곳마다 전/후를 남겨 화면에 보여 준다

형식별로 할 수 있는 것
  xlsx   set(칸 쓰기·수식) · append_row · replace           — 서식·수식 유지
  docx   replace · insert_after · append                    — 글자 모양 유지 (run 단위로 바꿈)
  hwpx   replace · insert_after · append                    — 글자 모양 유지, 바뀐 문단은 줄 배치를 한글이 다시 계산
  pdf    fill(양식 칸) · delete_pages · rotate              — PDF 그대로
         글 고치기는 PDF 안에서 할 수 없어 → 워드(.docx)로 새로 만든다 (서식 없음)
  hwp    읽기 전용 바이너리 → 글 고치기는 워드(.docx)로 새로 만든다 (서식 없음)
  image  rotate · flip · crop · resize · grayscale · brightness · contrast · sharpen · format
"""
import copy
import io
import re
import zipfile

MAX_OPS = 200
XML_DECL = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


class NothingChanged(ValueError):
    pass


# ================================================================ 공통: 여러 조각에 걸친 찾기·바꾸기
class Slot:
    """글 한 조각(run·t·tail). get/set 과 그것이 속한 문단(바뀌면 표시)."""
    __slots__ = ("get", "set", "para")

    def __init__(self, get, set_, para=None):
        self.get, self.set, self.para = get, set_, para


def replace_in_slots(slots: list, find: str, repl: str, dirty: set | None = None) -> int:
    """조각들을 이어 붙인 글에서 find 를 모두 repl 로. 첫 조각의 모양을 따르고 나머지는 비운다.
    뒤에서부터 바꾸므로 앞쪽 위치가 흔들리지 않는다."""
    if not find:
        return 0
    texts = [s.get() or "" for s in slots]
    full = "".join(texts)
    hits = [m.start() for m in re.finditer(re.escape(find), full)]
    for start in reversed(hits):
        end = start + len(find)
        texts = [s.get() or "" for s in slots]
        bounds, pos = [], 0
        for t in texts:
            bounds.append((pos, pos + len(t)))
            pos += len(t)
        i = next(k for k, (a, b) in enumerate(bounds) if a <= start < b)
        j = next(k for k, (a, b) in enumerate(bounds) if a < end <= b)
        si, sj = start - bounds[i][0], end - bounds[j][0]
        if i == j:
            slots[i].set(texts[i][:si] + repl + texts[i][sj:])
        else:
            slots[i].set(texts[i][:si] + repl)
            for k in range(i + 1, j):
                slots[k].set("")
            slots[j].set(texts[j][sj:])
        if dirty is not None:
            for k in range(i, j + 1):
                if slots[k].para is not None:
                    dirty.add(id(slots[k].para))
    return len(hits)


def _ops(ops) -> list:
    return [o for o in (ops or []) if isinstance(o, dict) and o.get("op")][:MAX_OPS]


def _ch(op, ok, where="", before="", after="", why=""):
    return {"op": op, "ok": bool(ok), "where": str(where)[:80], "before": str(before)[:300],
            "after": str(after)[:300], "why": why}


def apply(kind: str, data: bytes, ops, name: str = "문서") -> dict:
    ops = _ops(ops)
    if not ops:
        raise NothingChanged("고칠 내용을 찾지 못했어요")
    fn = {"xlsx": edit_xlsx, "docx": edit_docx, "hwpx": edit_hwpx,
          "pdf": edit_pdf, "hwp": edit_hwp, "image": edit_image}.get(kind)
    if not fn:
        raise NothingChanged("이 형식은 고칠 수 없어요")
    out = fn(data, ops)
    if not any(c["ok"] for c in out["changes"]):
        raise NothingChanged("고칠 곳을 문서에서 찾지 못했어요: " +
                             "; ".join(c["why"] or c["before"] for c in out["changes"])[:300])
    out.setdefault("notes", [])
    return out


# ================================================================ 엑셀
def _coerce(v):
    if isinstance(v, (int, float)) or v is None:
        return v
    s = str(v)
    if s.startswith("="):
        return s
    t = s.replace(",", "").strip()
    if re.fullmatch(r"-?\d+", t) and len(t) < 16 and not (len(t) > 1 and t.lstrip("-").startswith("0")):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    return s


def edit_xlsx(data: bytes, ops: list) -> dict:
    from openpyxl import load_workbook
    from openpyxl.utils.cell import coordinate_from_string
    z = zipfile.ZipFile(io.BytesIO(data))
    is_xlsm = any(n.startswith("xl/vbaProject") for n in z.namelist())
    had_drawings = any(n.startswith(("xl/drawings/", "xl/charts/")) for n in z.namelist())
    wb = load_workbook(io.BytesIO(data), keep_vba=is_xlsm)
    names = {ws.title.lower(): ws for ws in wb.worksheets}

    def sheet(o):
        s = o.get("sheet")
        return names.get(str(s).lower()) if s else wb.worksheets[0]

    changes = []
    for o in ops:
        op = o["op"]
        ws = sheet(o)
        if ws is None:
            changes.append(_ch(op, False, o.get("sheet"), why=f"'{o.get('sheet')}' 시트가 없어요"))
            continue
        if op == "set":
            cell = str(o.get("cell") or "").upper().replace("$", "")
            try:
                coordinate_from_string(cell)
            except Exception:
                changes.append(_ch(op, False, cell, why="칸 주소가 이상해요"))
                continue
            before = ws[cell].value
            ws[cell].value = _coerce(o.get("value"))
            changes.append(_ch(op, True, f"{ws.title}!{cell}", before if before is not None else "(빈 칸)", ws[cell].value))
        elif op == "append_row":
            vals = [_coerce(v) for v in (o.get("values") or [])][:200]
            if not vals:
                changes.append(_ch(op, False, ws.title, why="넣을 값이 없어요"))
                continue
            ws.append(vals)
            changes.append(_ch(op, True, f"{ws.title} {ws.max_row}행", "", " | ".join(map(str, vals))))
        elif op == "replace":
            find, repl = str(o.get("find") or ""), str(o.get("with") or "")
            n = 0
            targets = [ws] if o.get("sheet") else wb.worksheets
            for w in targets:
                for row in w.iter_rows():
                    for c in row:
                        if isinstance(c.value, str) and not c.value.startswith("=") and find and find in c.value:
                            c.value = _coerce(c.value.replace(find, repl))
                            n += 1
            changes.append(_ch(op, n > 0, f"{n}칸", find, repl, "" if n else f"'{find}' 를 못 찾았어요"))
        else:
            changes.append(_ch(op, False, why=f"엑셀에서는 '{op}' 를 할 수 없어요"))
    buf = io.BytesIO()
    wb.save(buf)
    notes = ["원본의 그림·차트는 고친 파일에서 빠질 수 있어요."] if had_drawings else []
    return {"data": buf.getvalue(), "ext": "xlsm" if is_xlsm else "xlsx", "kind": "xlsx",
            "changes": changes, "notes": notes}


# ================================================================ 워드
def _docx_paragraphs(doc):
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    out = [Paragraph(p, doc) for p in doc.element.body.iter(qn("w:p"))]
    for sec in doc.sections:
        for part in (sec.header, sec.footer):
            try:
                out += [Paragraph(p, part) for p in part._element.iter(qn("w:p"))]
            except Exception:
                pass
    return out


def _docx_slots(p) -> list:
    from docx.text.run import Run
    runs = []
    try:
        for item in p.iter_inner_content():
            runs += [item] if isinstance(item, Run) else list(getattr(item, "runs", []))
    except AttributeError:
        runs = list(p.runs)
    return [Slot(lambda r=r: r.text, lambda v, r=r: setattr(r, "text", v), p) for r in runs]


def _docx_insert_after(p, text: str):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    new = OxmlElement("w:p")
    ppr = p._p.find(qn("w:pPr"))
    if ppr is not None:
        new.append(copy.deepcopy(ppr))
    p._p.addnext(new)
    np_ = Paragraph(new, p._parent)
    r = np_.add_run(text)
    first = p.runs[0]._r.find(qn("w:rPr")) if p.runs else None
    if first is not None:
        r._r.insert(0, copy.deepcopy(first))
    return np_


def _text_ops(ops, paragraphs, slots_of, insert_after, append_end) -> list:
    """docx·hwpx 공통: replace / insert_after / append"""
    changes = []
    for o in ops:
        op = o["op"]
        if op in ("replace", "delete"):
            find = str(o.get("find") or "")
            repl = "" if op == "delete" else str(o.get("with") or "")
            n = 0
            for p in paragraphs():
                n += replace_in_slots(slots_of(p), find, repl)
            changes.append(_ch(op, n > 0, f"{n}곳" if n else "", find, repl, "" if n else f"'{find[:40]}' 를 못 찾았어요"))
        elif op == "insert_after":
            anchor, text = str(o.get("find") or ""), str(o.get("text") or "")
            target = next((p for p in paragraphs() if anchor and anchor in "".join(s.get() or "" for s in slots_of(p))), None)
            if target is None or not text:
                changes.append(_ch(op, False, before=anchor, why=f"'{anchor[:40]}' 문단을 못 찾았어요" if text else "넣을 글이 없어요"))
                continue
            for line in reversed(text.split("\n")):
                insert_after(target, line)
            changes.append(_ch(op, True, f"'{anchor[:30]}' 다음", "", text))
        elif op == "append":
            text = str(o.get("text") or "")
            if not text:
                changes.append(_ch(op, False, why="넣을 글이 없어요"))
                continue
            for line in text.split("\n"):
                append_end(line)
            changes.append(_ch(op, True, "문서 끝", "", text))
        else:
            changes.append(_ch(op, False, why=f"'{op}' 는 이 문서에서 할 수 없어요"))
    return changes


def edit_docx(data: bytes, ops: list) -> dict:
    from docx import Document
    doc = Document(io.BytesIO(data))
    body_paras = lambda: [p for p in doc.paragraphs]      # noqa: E731

    def append_end(line):
        last = next((p for p in reversed(body_paras()) if p.text.strip()), None)
        if last is not None:
            _append_like(doc, last, line)
        else:
            doc.add_paragraph(line)

    changes = _text_ops(ops, lambda: _docx_paragraphs(doc), _docx_slots, _docx_insert_after, append_end)
    buf = io.BytesIO()
    doc.save(buf)
    return {"data": buf.getvalue(), "ext": "docx", "kind": "docx", "changes": changes}


def _append_like(doc, like, line):
    """문서 끝에 새 문단. 모양은 마지막 글 문단을 따른다."""
    from docx.oxml.ns import qn
    np_ = doc.add_paragraph(line)
    ppr = like._p.find(qn("w:pPr"))
    if ppr is not None and np_._p.find(qn("w:pPr")) is None:
        np_._p.insert(0, copy.deepcopy(ppr))
    rpr = like.runs[0]._r.find(qn("w:rPr")) if like.runs else None
    if rpr is not None and np_.runs:
        np_.runs[0]._r.insert(0, copy.deepcopy(rpr))


# ================================================================ 한글 HWPX
def edit_hwpx(data: bytes, ops: list) -> dict:
    from lxml import etree
    from .docread import HP, hwpx_sections, hwpx_para_text
    T, RUN, P, LSA = f"{{{HP}}}t", f"{{{HP}}}run", f"{{{HP}}}p", f"{{{HP}}}linesegarray"
    zin = zipfile.ZipFile(io.BytesIO(data))
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
    roots = {s: etree.fromstring(zin.read(s), parser) for s in hwpx_sections(zin)}
    dirty: set = set()
    touched_paras = []

    def paragraphs():
        return [p for r in roots.values() for p in r.iter(P)]

    BR = f"{{{HP}}}lineBreak"

    def set_text(t, v):          # 여러 줄 값은 한글의 줄바꿈 요소로
        parts = (v or "").split("\n")
        t.text = parts[0]
        for part in reversed(parts[1:]):
            br = etree.Element(BR)
            br.tail = part
            t.insert(0, br)

    def set_tail(t, ch, v):
        parts = (v or "").split("\n")
        ch.tail = parts[0]
        at = list(t).index(ch) + 1
        for part in reversed(parts[1:]):
            br = etree.Element(BR)
            br.tail = part
            t.insert(at, br)

    def slots_of(p):
        out = []
        for t in p.findall(f"{RUN}/{T}"):
            kids = list(t)
            out.append(Slot(lambda t=t: t.text, lambda v, t=t: set_text(t, v), p))
            for ch in kids:
                out.append(Slot(lambda c=ch: c.tail, lambda v, t=t, c=ch: set_tail(t, c, v), p))
        return out

    def mark(p):
        touched_paras.append(p)

    def new_para_like(p, text):
        np_ = copy.deepcopy(p)
        for k in ("pageBreak", "columnBreak"):          # 복제한 문단이 쪽을 넘기지 않게
            if np_.get(k) is not None:
                np_.set(k, "0")
        runs = np_.findall(RUN)
        for r in runs[1:]:
            np_.remove(r)
        for child in list(np_):
            if child.tag not in (RUN,):
                np_.remove(child)
        if runs:
            r = runs[0]
            for c in list(r):
                r.remove(c)
            t = etree.SubElement(r, T)
        else:
            r = etree.SubElement(np_, RUN)
            t = etree.SubElement(r, T)
        t.text = text
        return np_

    def insert_after(p, line):
        np_ = new_para_like(p, line)
        p.addnext(np_)
        mark(np_)

    def append_end(line):
        last_root = list(roots.values())[-1]
        tops = [p for p in last_root.findall(P) if hwpx_para_text(p).strip()] or last_root.findall(P)
        if not tops:
            raise NothingChanged("문단이 없는 한글 문서예요")
        insert_after(tops[-1], line)

    # 찾기·바꾸기에서 바뀐 문단 표시
    def slots_marked(p):
        ss = slots_of(p)
        for s in ss:
            orig = s.set
            s.set = (lambda v, orig=orig, p=p: (orig(v), mark(p)))
        return ss

    changes = _text_ops(ops, paragraphs, slots_marked, insert_after, append_end)
    # 바뀐 문단의 줄 배치 캐시를 지운다 → 한글이 열 때 다시 계산 (안 지우면 글자가 겹쳐 보일 수 있음)
    for p in touched_paras:
        for lsa in p.findall(LSA):
            p.remove(lsa)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            if info.filename in roots:
                body = XML_DECL + etree.tostring(roots[info.filename], encoding="UTF-8")
            elif info.filename == "Preview/PrvText.txt":
                body = "\r\n".join(hwpx_para_text(p) for p in paragraphs() if hwpx_para_text(p).strip())[:1024].encode("utf-8")
            else:
                body = zin.read(info.filename)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type          # mimetype 은 압축 없이(STORED) 그대로
            zi.external_attr = info.external_attr
            zout.writestr(zi, body)
    return {"data": out.getvalue(), "ext": "hwpx", "kind": "hwpx", "changes": changes}


# ================================================================ PDF · 한글(HWP) → 글 고치기는 워드로
PDF_OPS = {"fill", "delete_pages", "rotate"}
TEXT_OPS = {"replace", "delete", "insert_after", "append"}


def _text_to_docx(paragraphs: list, ops: list) -> dict:
    from docx import Document
    doc = Document()
    for line in paragraphs:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    out = edit_docx(buf.getvalue(), ops)
    out["notes"] = ["원본 형식 안에서는 글을 고칠 수 없어, 고친 글을 워드(.docx)로 새로 만들었어요. 원래 서식·표 모양은 빠져요."]
    return out


def edit_pdf(data: bytes, ops: list) -> dict:
    import logging
    from pypdf import PdfReader, PdfWriter
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    pdf_ops = [o for o in ops if o["op"] in PDF_OPS]
    text_ops = [o for o in ops if o["op"] in TEXT_OPS]
    if text_ops and not pdf_ops:
        from .docread import read_pdf
        pages = read_pdf(data)["outline"]["pages"]
        lines = [ln for p in pages for ln in (p["text"] or "").split("\n")]
        return _text_to_docx(lines, text_ops)
    r = PdfReader(io.BytesIO(data))
    w = PdfWriter(clone_from=r)
    n = len(w.pages)
    changes, drop = [], set()

    def pages_of(o):
        ps = o.get("pages") or []
        ok = sorted({int(x) for x in ps if str(x).lstrip("-").isdigit() and 1 <= int(x) <= n})
        return ok

    for o in ops:
        op = o["op"]
        if op == "fill":
            field, value = str(o.get("field") or ""), str(o.get("value") or "")
            fields = r.get_fields() or {}
            if field not in fields:
                changes.append(_ch(op, False, field, why=f"'{field}' 칸이 없어요"))
                continue
            before = fields[field].get("/V")
            for pg in w.pages:
                try:
                    w.update_page_form_field_values(pg, {field: value}, auto_regenerate=False)
                except Exception:
                    pass
            changes.append(_ch(op, True, field, before or "(빈 칸)", value))
        elif op == "delete_pages":
            ps = pages_of(o)
            if not ps or len(drop | set(ps)) >= n:
                changes.append(_ch(op, False, why="지울 쪽이 없거나 모든 쪽을 지우게 돼요"))
                continue
            drop |= set(ps)
            changes.append(_ch(op, True, ", ".join(map(str, ps)) + "쪽", "있음", "지움"))
        elif op == "rotate":
            ps, deg = pages_of(o) or list(range(1, n + 1)), int(o.get("deg") or 90)
            if deg % 90:
                changes.append(_ch(op, False, why="90도 단위로만 돌릴 수 있어요"))
                continue
            for pnum in ps:
                w.pages[pnum - 1].rotate(deg)
            changes.append(_ch(op, True, ", ".join(map(str, ps)) + "쪽", "", f"{deg}도 돌림"))
        elif op in TEXT_OPS:
            changes.append(_ch(op, False, why="PDF 안의 글은 쪽 작업과 함께 고칠 수 없어요. 따로 요청해 주세요"))
        else:
            changes.append(_ch(op, False, why=f"PDF 에서는 '{op}' 를 할 수 없어요"))
    for idx in sorted(drop, reverse=True):
        del w.pages[idx - 1]
    if any(c["ok"] and c["op"] == "fill" for c in changes):
        try:
            w.set_need_appearances_writer(True)
        except Exception:
            pass
    buf = io.BytesIO()
    w.write(buf)
    return {"data": buf.getvalue(), "ext": "pdf", "kind": "pdf", "changes": changes}


def edit_hwp(data: bytes, ops: list) -> dict:
    from .docread import read_hwp
    text_ops = [o for o in ops if o["op"] in TEXT_OPS]
    if not text_ops:
        return {"data": b"", "ext": "docx", "kind": "docx",
                "changes": [_ch(o["op"], False, why="한글(.hwp) 파일은 글 고치기만 할 수 있어요") for o in ops]}
    paras = [b["text"] for b in read_hwp(data)["outline"]["blocks"]]
    out = _text_to_docx(paras, text_ops)
    out["notes"] = ["옛 한글(.hwp) 파일은 안을 직접 고칠 수 없어, 고친 글을 워드(.docx)로 만들었어요. "
                    "한글에서 '다른 이름으로 저장 → .hwpx' 로 바꿔 올리면 서식을 그대로 두고 고칠 수 있어요."]
    return out


# ================================================================ 사진
IMG_FORMATS = {"jpeg": ("JPEG", "jpg"), "jpg": ("JPEG", "jpg"), "png": ("PNG", "png"), "webp": ("WEBP", "webp")}


def edit_image(data: bytes, ops: list) -> dict:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    src_fmt = (Image.open(io.BytesIO(data)).format or "PNG").lower()
    fmt = IMG_FORMATS.get(src_fmt, ("PNG", "png"))
    changes = []
    for o in ops:
        op = o["op"]
        try:
            if op == "rotate":
                deg = int(o.get("deg") or 90)
                im = im.rotate(-deg, expand=True)               # 양수 = 시계 방향
                changes.append(_ch(op, True, "", "", f"시계 방향 {deg}도"))
            elif op == "flip":
                horizontal = (o.get("dir") or "h") in ("h", "horizontal", "좌우")
                im = ImageOps.mirror(im) if horizontal else ImageOps.flip(im)
                changes.append(_ch(op, True, "", "", "좌우 뒤집기" if horizontal else "상하 뒤집기"))
            elif op == "crop":
                b = [float(x) for x in (o.get("box") or [])]
                if len(b) != 4 or not (0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1):
                    changes.append(_ch(op, False, why="자를 범위가 이상해요"))
                    continue
                W, H = im.size
                im = im.crop((round(b[0] * W), round(b[1] * H), round(b[2] * W), round(b[3] * H)))
                changes.append(_ch(op, True, "", f"{W}×{H}", f"{im.width}×{im.height}"))
            elif op == "resize":
                W, H = im.size
                if o.get("width"):
                    nw = max(8, min(8000, int(o["width"])))
                elif o.get("scale"):
                    nw = max(8, min(8000, round(W * float(o["scale"]))))
                else:
                    changes.append(_ch(op, False, why="크기가 없어요"))
                    continue
                im = im.resize((nw, max(1, round(H * nw / W))), Image.LANCZOS)
                changes.append(_ch(op, True, "", f"{W}×{H}", f"{im.width}×{im.height}"))
            elif op == "grayscale":
                im = ImageOps.grayscale(im)
                changes.append(_ch(op, True, "", "", "흑백"))
            elif op in ("brightness", "contrast"):
                f = max(0.1, min(3.0, float(o.get("factor") or 1.2)))
                base = im.convert("RGB") if im.mode not in ("RGB", "L") else im
                im = (ImageEnhance.Brightness if op == "brightness" else ImageEnhance.Contrast)(base).enhance(f)
                changes.append(_ch(op, True, "", "", f"{'밝기' if op == 'brightness' else '대비'} ×{f:g}"))
            elif op == "sharpen":
                im = im.filter(ImageFilter.SHARPEN)
                changes.append(_ch(op, True, "", "", "선명하게"))
            elif op == "format":
                to = str(o.get("to") or "").lower()
                if to not in IMG_FORMATS:
                    changes.append(_ch(op, False, why="png·jpg·webp 로만 바꿀 수 있어요"))
                    continue
                fmt = IMG_FORMATS[to]
                changes.append(_ch(op, True, "", src_fmt, to))
            else:
                changes.append(_ch(op, False, why=f"사진에서는 '{op}' 를 할 수 없어요"))
        except (TypeError, ValueError) as e:
            changes.append(_ch(op, False, why=f"값이 이상해요 ({e})"))
    if fmt[0] == "JPEG" and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, fmt[0], **({"quality": 90} if fmt[0] in ("JPEG", "WEBP") else {}))
    return {"data": buf.getvalue(), "ext": fmt[1], "kind": "image", "changes": changes}
