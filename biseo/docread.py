"""
docread.py — 올린 문서에서 글과 구조를 뽑는다.

  kind_of(name, mime)  → 'xlsx' | 'docx' | 'pdf' | 'hwp' | 'hwpx' | 'image' | None
  read(kind, data)     → {"text": 비서가 읽을 글, "outline": 화면 미리보기용 구조, "pages": 쪽 수, "note": 알림}

비서가 읽는 글(text)에는 위치 표시를 단다. 고치기를 할 때 "어디를"을 정확히 가리키게 하려고.
  xlsx  시트별로 'B3: 값'          docx·hwpx·hwp  '¶12 문단'      pdf  '--- 3쪽 ---'

외부 패키지: openpyxl, python-docx, pypdf, lxml, Pillow (HWP 5.0 은 cfb.py 로 직접 읽는다)
"""
import io
import struct
import zipfile
import zlib

MAX_TEXT = 200_000          # DB 에 담는 글 상한 (글자)
PREVIEW_ROWS, PREVIEW_COLS = 60, 14

EXT = {
    "xlsx": "xlsx", "xlsm": "xlsx",
    "docx": "docx",
    "pdf": "pdf",
    "hwp": "hwp",
    "hwpx": "hwpx",
    "png": "image", "jpg": "image", "jpeg": "image", "webp": "image", "gif": "image", "bmp": "image",
}
OLD = {"xls": "엑셀 97-2003(.xls)", "doc": "워드 97-2003(.doc)", "ppt": "파워포인트", "pptx": "파워포인트",
       "heic": "HEIC 사진", "heif": "HEIC 사진"}


class Unsupported(ValueError):
    pass


def kind_of(name: str, mime: str = "") -> str | None:
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    if ext in EXT:
        return EXT[ext]
    if (mime or "").startswith("image/") and "heic" not in mime:
        return "image"
    return None


def why_unsupported(name: str) -> str:
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    if ext in OLD:
        return f"{OLD[ext]} 파일은 아직 못 읽어요. 새 형식(.xlsx·.docx 등)으로 저장해서 올려 주세요."
    return "엑셀(.xlsx), PDF, 워드(.docx), 한글(.hwp·.hwpx), 사진 파일을 올릴 수 있어요."


def read(kind: str, data: bytes) -> dict:
    fn = {"xlsx": read_xlsx, "docx": read_docx, "pdf": read_pdf,
          "hwp": read_hwp, "hwpx": read_hwpx, "image": read_image}.get(kind)
    if not fn:
        raise Unsupported("지원하지 않는 형식")
    out = fn(data)
    out["text"] = (out.get("text") or "")[:MAX_TEXT]
    return out


# ---------------------------------------------------------------- 엑셀
def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ") if hasattr(v, "hour") else v.isoformat()
    return str(v)


def read_xlsx(data: bytes) -> dict:
    from openpyxl import load_workbook
    try:
        wf = load_workbook(io.BytesIO(data), data_only=False)      # 수식
        wv = load_workbook(io.BytesIO(data), data_only=True)       # 마지막으로 계산된 값
    except Exception as e:
        raise Unsupported(f"엑셀 파일을 열 수 없어요 ({type(e).__name__})")
    lines, sheets = [], []
    for ws in wf.worksheets:
        vs = wv[ws.title]
        lines.append(f"## 시트: {ws.title} (범위 {ws.dimensions})")
        grid = []
        for r_i, row in enumerate(ws.iter_rows(), start=1):
            cells, prev = [], []
            for c in row:
                v = c.value
                if v is None:
                    if r_i <= PREVIEW_ROWS and c.column <= PREVIEW_COLS:
                        prev.append("")
                    continue
                shown = _cell_str(vs[c.coordinate].value)
                if isinstance(v, str) and v.startswith("="):
                    cells.append(f"{c.coordinate}: {v}" + (f" (→ {shown})" if shown else ""))
                else:
                    cells.append(f"{c.coordinate}: {_cell_str(v)}")
                if r_i <= PREVIEW_ROWS and c.column <= PREVIEW_COLS:
                    prev.append(shown if shown else (_cell_str(v)))
            if cells:
                lines.append(" | ".join(cells))
            if r_i <= PREVIEW_ROWS:
                grid.append(prev[:PREVIEW_COLS])
        while grid and not any(grid[-1]):
            grid.pop()
        sheets.append({"name": ws.title, "dims": ws.dimensions, "rows": grid,
                       "max_row": ws.max_row, "max_col": ws.max_column})
    return {"text": "\n".join(lines), "outline": {"sheets": sheets}, "pages": len(sheets)}


# ---------------------------------------------------------------- 워드
def docx_blocks(doc):
    """본문 순서대로 (종류, 객체). 표 안 문단도 순서대로 따라간다."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield "p", Paragraph(child, doc)
        elif tag == "tbl":
            yield "t", Table(child, doc)


def read_docx(data: bytes) -> dict:
    from docx import Document
    try:
        doc = Document(io.BytesIO(data))
    except Exception as e:
        raise Unsupported(f"워드 파일을 열 수 없어요 ({type(e).__name__})")
    lines, paras, n, t_n = [], [], 0, 0
    for kind, obj in docx_blocks(doc):
        if kind == "p":
            n += 1
            txt = obj.text
            if txt.strip():
                style = (obj.style.name if obj.style is not None else "") or ""
                head = style.lower().startswith(("heading", "title", "제목"))
                lines.append(f"¶{n} " + ("# " if head else "") + txt)
                paras.append({"n": n, "text": txt[:600], "head": head})
        else:
            t_n += 1
            lines.append(f"[표{t_n}]")
            rows = []
            for r_i, row in enumerate(obj.rows, start=1):
                vals = []
                for c in row.cells:
                    v = c.text.strip()
                    if not vals or vals[-1] != v:      # 병합된 칸이 되풀이되는 것 줄이기
                        vals.append(v)
                lines.append(f"  {r_i}행: " + " | ".join(vals))
                if r_i <= 30:
                    rows.append(vals[:10])
            paras.append({"table": t_n, "rows": rows})
    for sec in doc.sections:
        for p in sec.header.paragraphs:
            if p.text.strip():
                lines.insert(0, f"(머리글) {p.text}")
    return {"text": "\n".join(lines), "outline": {"blocks": paras[:400]}, "pages": None}


# ---------------------------------------------------------------- PDF
def read_pdf(data: bytes) -> dict:
    from pypdf import PdfReader
    try:
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            try:
                ok = r.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                raise Unsupported("암호가 걸린 PDF 는 읽을 수 없어요")
    except Unsupported:
        raise
    except Exception as e:
        raise Unsupported(f"PDF 를 열 수 없어요 ({type(e).__name__})")
    lines, pages, chars = [], [], 0
    for i, pg in enumerate(r.pages, start=1):
        try:
            t = pg.extract_text() or ""
        except Exception:
            t = ""
        t = t.strip()
        chars += len(t)
        lines.append(f"--- {i}쪽 ---\n{t}")
        pages.append({"n": i, "text": t[:1500]})
    fields = {}
    try:
        for k, f in (r.get_fields() or {}).items():
            v = f.get("/V")
            fields[k] = "" if v is None else str(v)
    except Exception:
        pass
    note = None
    if len(r.pages) and chars < 20 * len(r.pages):
        note = "scanned"                         # 글자가 거의 없음 → 사진으로 읽기 (docs.py)
    out = {"text": "\n".join(lines), "outline": {"pages": pages[:200], "fields": fields},
           "pages": len(r.pages), "note": note}
    if fields:
        out["text"] = "## 채울 칸(양식)\n" + "\n".join(f"{k} = {v}" for k, v in fields.items()) + "\n\n" + out["text"]
    return out


def pdf_page_images(data: bytes, pages: int = 4, width: int = 1400) -> list:
    """스캔 PDF 를 비서가 '볼' 수 있게 앞쪽 몇 쪽을 JPEG 로 그린다 (pypdfium2)."""
    try:
        import pypdfium2 as pdfium
    except Exception:
        return []
    out = []
    try:
        doc = pdfium.PdfDocument(data)
        for i in range(min(pages, len(doc))):
            pg = doc[i]
            scale = width / max(1.0, pg.get_width())
            img = pg.render(scale=scale).to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82)
            out.append(buf.getvalue())
    except Exception:
        return out
    return out


# ---------------------------------------------------------------- 한글 HWP 5.0 (바이너리)
HWPTAG_PARA_TEXT = 16 + 51            # 67
_ONE_WCHAR = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}


def hwp_records(buf: bytes):
    """레코드 머리(32비트) = 태그 10비트 | 수준 10비트 | 크기 12비트 (0xFFF 이면 다음 4바이트가 크기)."""
    i, n = 0, len(buf)
    while i + 4 <= n:
        h = struct.unpack_from("<I", buf, i)[0]
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, (h >> 20) & 0xFFF
        i += 4
        if size == 0xFFF:
            if i + 4 > n:
                break
            size = struct.unpack_from("<I", buf, i)[0]
            i += 4
        if i + size > n:
            break
        yield tag, level, buf[i:i + size]
        i += size


def hwp_para_text(b: bytes) -> str:
    """PARA_TEXT: UTF-16LE. 32 미만 코드는 조종 문자 — 1칸짜리와 8칸(16바이트)짜리가 있다."""
    out, run, i = [], bytearray(), 0
    while i + 1 < len(b):
        c = b[i] | (b[i + 1] << 8)
        if c >= 32:
            run += b[i:i + 2]
            i += 2
            continue
        if run:
            out.append(run.decode("utf-16-le", "replace"))
            run = bytearray()
        if c in (10, 13):
            out.append("\n")
            i += 2
        elif c == 9:
            out.append("\t")
            i += 16
        elif c in _ONE_WCHAR:
            i += 2
        else:
            i += 16
    if run:
        out.append(run.decode("utf-16-le", "replace"))
    return "".join(out)


def read_hwp(data: bytes) -> dict:
    from .cfb import CFB, CFBError
    try:
        f = CFB(data)
        head = f.read("FileHeader")
    except CFBError as e:
        raise Unsupported(f"한글 파일을 열 수 없어요 ({e})")
    if not head.startswith(b"HWP Document File"):
        raise Unsupported("한글 5.0 형식이 아니에요 (아주 옛 한글 파일일 수 있어요)")
    props = struct.unpack_from("<I", head, 36)[0] if len(head) >= 40 else 0
    compressed, locked, distributed = bool(props & 1), bool(props & 2), bool(props & 4)
    if locked:
        raise Unsupported("암호가 걸린 한글 파일은 읽을 수 없어요")
    paras, note = [], None
    if distributed:
        note = "distributed"           # 배포용 문서: 본문이 암호화돼 있어 미리보기 글만 읽힌다
    else:
        names = sorted((n for n in f.listdir("BodyText") if n.lower().startswith("section")),
                       key=lambda s: int("".join(ch for ch in s if ch.isdigit()) or 0))
        for name in names:
            raw = f.read(f"BodyText/{name}")
            if compressed:
                try:
                    raw = zlib.decompress(raw, -15)
                except zlib.error:
                    raise Unsupported("한글 본문 압축을 풀 수 없어요")
            for tag, _lvl, payload in hwp_records(raw):
                if tag == HWPTAG_PARA_TEXT:
                    t = hwp_para_text(payload).strip("\n")
                    for line in t.split("\n"):
                        paras.append(line)
    if not any(p.strip() for p in paras) and f.exists("PrvText"):
        prv = f.read("PrvText").decode("utf-16-le", "replace").replace("\r", "")
        paras = prv.split("\n")
        note = note or "preview"
    paras = [p for p in paras if p.strip()]
    lines = [f"¶{i} {p}" for i, p in enumerate(paras, start=1)]
    return {"text": "\n".join(lines), "outline": {"blocks": [{"n": i, "text": p[:600]} for i, p in enumerate(paras[:400], 1)]},
            "pages": None, "note": note}


# ---------------------------------------------------------------- 한글 HWPX (OWPML, zip+XML)
HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
_TAB, _BR = f"{{{HP}}}tab", f"{{{HP}}}lineBreak"


def hwpx_sections(z: zipfile.ZipFile) -> list:
    names = [n for n in z.namelist() if n.startswith("Contents/section") and n.endswith(".xml")]
    return sorted(names, key=lambda s: int("".join(ch for ch in s.rsplit("/", 1)[-1] if ch.isdigit()) or 0))


def hwpx_t_text(t) -> str:
    """<hp:t> 안 글. 탭·줄바꿈이 자식 요소로 들어가고 그 뒤 글은 tail 에 있다."""
    parts = [t.text or ""]
    for ch in t:
        parts.append("\t" if ch.tag == _TAB else ("\n" if ch.tag == _BR else ""))
        parts.append(ch.tail or "")
    return "".join(parts)


def hwpx_para_text(p) -> str:
    # 바로 아래 run 의 t 만 (표가 든 문단은 칸 안 문단이 따로 나오므로 겹치지 않게)
    return "".join(hwpx_t_text(t) for t in p.findall(f"{{{HP}}}run/{{{HP}}}t"))


def read_hwpx(data: bytes) -> dict:
    from lxml import etree
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Unsupported("한글(.hwpx) 파일이 깨졌어요")
    secs = hwpx_sections(z)
    if not secs:
        raise Unsupported("한글(.hwpx) 본문을 찾지 못했어요")
    paras = []
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    for s in secs:
        root = etree.fromstring(z.read(s), parser)
        for p in root.iter(f"{{{HP}}}p"):
            t = hwpx_para_text(p)
            if t.strip():
                paras.append(t)
    lines = [f"¶{i} {p}" for i, p in enumerate(paras, start=1)]
    return {"text": "\n".join(lines), "outline": {"blocks": [{"n": i, "text": p[:600]} for i, p in enumerate(paras[:400], 1)]},
            "pages": None}


# ---------------------------------------------------------------- 사진
def read_image(data: bytes) -> dict:
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or "").lower()
        im = ImageOps.exif_transpose(im)
        w, h = im.width, im.height
    except Exception as e:
        raise Unsupported(f"사진을 열 수 없어요 ({type(e).__name__})")
    # 글(사진 속 글자·설명)은 docs.py 가 비전 모델로 채운다
    return {"text": "", "outline": {"w": w, "h": h, "format": fmt}, "pages": 1}


def image_for_vision(data: bytes, max_side: int = 1600) -> bytes:
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        try:
            bg.paste(im, mask=im.convert("RGBA").split()[-1])
        except Exception:
            bg.paste(im.convert("RGB"))
        im = bg
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()
