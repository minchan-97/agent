"""
cfb.py — 한글(HWP 5.0) 파일을 열기 위한 작은 OLE(Compound File Binary) 리더.

HWP 5.0 은 MS 의 복합 문서 형식(CFB) 안에 'FileHeader', 'BodyText/Section0' 같은
스트림을 담는다. olefile 같은 외부 패키지 없이 읽기만 한다 (쓰기는 하지 않음).

규격: [MS-CFB] — 헤더 512바이트, FAT/DIFAT/미니 FAT, 128바이트 디렉터리 항목.
깨진 파일에서 무한 반복하지 않도록 사슬 길이를 섹터 수로 묶어 둔다.
"""
import struct

SIG = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
ENDOFCHAIN, FREESECT, FATSECT, DIFSECT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF


class CFBError(ValueError):
    pass


class CFB:
    def __init__(self, data: bytes):
        if len(data) < 512 or data[:8] != SIG:
            raise CFBError("OLE 복합 문서가 아니에요")
        self.d = data
        self.ssize = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        self.mssize = 1 << struct.unpack_from("<H", data, 0x20)[0]
        if self.ssize not in (512, 4096) or self.mssize != 64:
            raise CFBError("지원하지 않는 섹터 크기")
        (n_fat, first_dir, _, cutoff, first_mfat, n_mfat, first_difat, n_difat) = \
            struct.unpack_from("<IIIIIIII", data, 0x2C)
        self.cutoff = cutoff
        self.nsect = max(0, (len(data) - self.ssize) // self.ssize)
        # FAT 섹터 목록: 헤더의 109개 + DIFAT 사슬
        fat_secs = [s for s in struct.unpack_from("<109I", data, 0x4C) if s < 0xFFFFFFFA]
        per = self.ssize // 4 - 1
        s, guard = first_difat, 0
        while s < 0xFFFFFFFA and guard < n_difat + 1 and guard <= self.nsect:
            vals = struct.unpack_from(f"<{per + 1}I", self._sector(s))
            fat_secs += [v for v in vals[:per] if v < 0xFFFFFFFA]
            s, guard = vals[per], guard + 1
        fat = []
        for fs in fat_secs[:max(n_fat, len(fat_secs))]:
            fat += struct.unpack_from(f"<{self.ssize // 4}I", self._sector(fs))
        self.fat = fat
        self.dirs = self._read_dirs(first_dir)
        root = self.dirs[0]
        self.ministream = self._chain_bytes(root["start"], root["size"]) if root["size"] else b""
        self.minifat = []
        if n_mfat and first_mfat < 0xFFFFFFFA:
            raw = self._chain_bytes(first_mfat, None)
            self.minifat = list(struct.unpack_from(f"<{len(raw) // 4}I", raw))

    # ---------- 섹터와 사슬 ----------
    def _sector(self, n: int) -> bytes:
        off = (n + 1) * self.ssize
        if n >= 0xFFFFFFFA or off + self.ssize > len(self.d) + self.ssize:
            raise CFBError("섹터 번호가 파일 밖이에요")
        return self.d[off: off + self.ssize].ljust(self.ssize, b"\0")

    def _chain(self, start: int, table: list, limit: int):
        out, s = [], start
        while s < 0xFFFFFFFA:
            if len(out) > limit or s >= len(table):
                raise CFBError("섹터 사슬이 깨졌어요")
            out.append(s)
            s = table[s]
        return out

    def _chain_bytes(self, start: int, size):
        if start >= 0xFFFFFFFA:
            return b""
        buf = b"".join(self._sector(s) for s in self._chain(start, self.fat, self.nsect + 1))
        return buf if size is None else buf[:size]

    def _mini_bytes(self, start: int, size: int) -> bytes:
        chain = self._chain(start, self.minifat, len(self.minifat) + 1)
        buf = b"".join(self.ministream[s * 64:(s + 1) * 64] for s in chain)
        return buf[:size]

    # ---------- 디렉터리 ----------
    def _read_dirs(self, first: int) -> list:
        raw = self._chain_bytes(first, None)
        out = []
        for i in range(len(raw) // 128):
            e = raw[i * 128:(i + 1) * 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            name = e[:max(0, min(nlen, 64) - 2)].decode("utf-16-le", "replace")
            typ = e[66]
            left, right, child = struct.unpack_from("<III", e, 68)
            start, size = struct.unpack_from("<IQ", e, 116)
            if self.ssize == 512:
                size &= 0xFFFFFFFF
            out.append({"name": name, "type": typ, "left": left, "right": right,
                        "child": child, "start": start, "size": size})
        if not out or out[0]["type"] != 5:
            raise CFBError("루트 항목이 없어요")
        return out

    def _children(self, idx: int) -> list:
        """한 저장소(폴더)의 자식들. 형제는 이진 트리(left/right)로 이어져 있다."""
        out, stack, seen = [], [self.dirs[idx]["child"]], set()
        while stack:
            i = stack.pop()
            if i == NOSTREAM or i >= len(self.dirs) or i in seen:
                continue
            seen.add(i)
            e = self.dirs[i]
            out.append(i)
            stack += [e["left"], e["right"]]
        return out

    def _find(self, path: str):
        idx = 0
        for part in [p for p in path.split("/") if p]:
            nxt = [c for c in self._children(idx) if self.dirs[c]["name"].lower() == part.lower()]
            if not nxt:
                return None
            idx = nxt[0]
        return idx

    def exists(self, path: str) -> bool:
        return self._find(path) is not None

    def listdir(self, path: str = "") -> list:
        idx = self._find(path) if path else 0
        if idx is None:
            return []
        return sorted(self.dirs[c]["name"] for c in self._children(idx))

    def read(self, path: str) -> bytes:
        idx = self._find(path)
        if idx is None or self.dirs[idx]["type"] != 2:
            raise CFBError(f"스트림이 없어요: {path}")
        e = self.dirs[idx]
        if e["size"] < self.cutoff:
            return self._mini_bytes(e["start"], e["size"])
        return self._chain_bytes(e["start"], e["size"])
