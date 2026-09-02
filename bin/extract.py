import base64
import copy
import io
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zlib
from shutil import which
from urllib.parse import unquote_to_bytes

JPEG_EXTENSIONS = (".jpg", ".jpeg")

_SVG_STRIP_ELEMENTS = {
    "script", "foreignObject", "animate", "animateTransform", "animateMotion", "set", "handler",
}
_SVG_CONCEAL_PROPS = {
    "opacity", "fill-opacity", "stroke-opacity", "display", "visibility", "clip-path", "mask", "filter",
}
_DATA_URI_RE = re.compile(
    rb"data:image/[a-zA-Z0-9.+-]+((?:;[^,\"'<>\s]*)*),([^\"'<>)]{4,20000000})",
    re.IGNORECASE,
)
_HDR_GAINMAP_MARKERS = (b"urn:iso:std:iso:ts:21496", b"GContainer", b"HDRGainMap", b"GainMap")

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass


class UnsupportedFormat(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, max_frames=64, max_pixels=400_000_000, max_seconds=60):
        self.max_frames = max_frames
        self.max_pixels = max_pixels
        self.max_seconds = max_seconds
        self.frames_spent = 0
        self.pixels_spent = 0
        self.deadline = time.monotonic() + max_seconds

    def check(self):
        if time.monotonic() > self.deadline:
            raise BudgetExceeded(f"time budget exceeded ({self.max_seconds}s)")

    def spend(self, width, height):
        self.check()
        self.frames_spent += 1
        if self.frames_spent > self.max_frames:
            raise BudgetExceeded(f"frame budget exceeded ({self.max_frames})")
        self.pixels_spent += max(int(width), 0) * max(int(height), 0)
        if self.pixels_spent > self.max_pixels:
            raise BudgetExceeded(f"pixel budget exceeded ({self.max_pixels})")
        if time.monotonic() > self.deadline:
            raise BudgetExceeded(f"time budget exceeded ({self.max_seconds}s)")


class Frame:
    def __init__(self, view, image):
        self.view = view
        self.image = image


def canonicalize(image_path):
    ext = os.path.splitext(image_path)[1].lower()
    fd, canonical_path = tempfile.mkstemp(suffix=ext or ".png")
    os.close(fd)

    magick = "magick" if which("magick") else "convert"
    cmd = [magick, image_path, "-strip", "-colorspace", "sRGB"]
    if ext in JPEG_EXTENSIONS:
        cmd += ["-quality", "92"]
    cmd.append(canonical_path)

    try:
        _run_limited(cmd, timeout=20)
        return canonical_path
    except Exception:
        os.unlink(canonical_path)
        raise


def detect_format(path):
    with open(path, "rb") as f:
        head = f.read(4096)

    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "raster"
    if head[:3] == b"\xff\xd8\xff":
        return "raster"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "raster"
    if head[:2] == b"BM":
        return "raster"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "raster"
    if head[:4] == b"\x00\x00\x01\x00":
        return "raster"
    if head[:4] == b"RIFF" and len(head) >= 12:
        subtype = head[8:12]
        if subtype == b"WEBP":
            return "raster"
        if subtype == b"AVI ":
            return "video"
        raise UnsupportedFormat(f"unrecognized RIFF sub-type: {subtype!r}")
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis", b"mif1", b"msf1", b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"):
            return "raster"
        if brand in (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"M4V ", b"M4A ",
                      b"qt  ", b"3gp4", b"3gp5", b"3g2a", b"dash"):
            return "video"
        raise UnsupportedFormat(f"unrecognized ISOBMFF brand: {brand!r}")
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "office"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "office-legacy"
    if head[:5] == b"%PDF-":
        return "pdf"
    if head[:4] == b"AT&T" and head[4:8] == b"FORM" and head[12:16] in (b"DJVU", b"DJVM", b"PM44", b"BM44"):
        return "djvu"

    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped[:5].lower() == b"<?xml":
        idx = stripped.find(b"?>")
        if idx != -1:
            stripped = stripped[idx + 2:].lstrip(b" \t\r\n")
    for _ in range(4):
        if stripped[:4] == b"<!--":
            idx = stripped.find(b"-->")
            if idx == -1:
                break
            stripped = stripped[idx + 3:].lstrip(b" \t\r\n")
        elif stripped[:9].lower() == b"<!doctype":
            idx = stripped.find(b">")
            if idx == -1:
                break
            stripped = stripped[idx + 1:].lstrip(b" \t\r\n")
        else:
            break
    if stripped[:4].lower() == b"<svg":
        return "svg"

    raise UnsupportedFormat("unrecognized file signature")


def _local_name(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _svg_strip_dangerous(root):
    for parent in list(root.iter()):
        for child in list(parent):
            if _local_name(child.tag) in _SVG_STRIP_ELEMENTS:
                parent.remove(child)
    for el in root.iter():
        for attr in list(el.attrib):
            local = _local_name(attr)
            if local.lower().startswith("on"):
                del el.attrib[attr]
                continue
            if local in ("href", "src"):
                value = el.attrib[attr].strip()
                if not (value.startswith("#") or value.startswith("data:")):
                    del el.attrib[attr]
    return root


_CSS_DECL_RE = re.compile(r"([-a-zA-Z]+)\s*:\s*[^;{}]*;?")


def _strip_css_concealment(css):
    def repl(match):
        if match.group(1).strip().lower() in _SVG_CONCEAL_PROPS:
            return ""
        return match.group(0)

    return _CSS_DECL_RE.sub(repl, css)


def _svg_strip_concealment(root):
    for el in root.iter():
        if _local_name(el.tag).lower() == "style" and el.text:
            el.text = _strip_css_concealment(el.text)
        for attr in list(el.attrib):
            if _local_name(attr) in _SVG_CONCEAL_PROPS:
                del el.attrib[attr]
        style = el.attrib.get("style")
        if style:
            kept = []
            for decl in style.split(";"):
                if ":" not in decl:
                    continue
                prop, _, value = decl.partition(":")
                if prop.strip().lower() not in _SVG_CONCEAL_PROPS:
                    kept.append(decl)
            if kept:
                el.attrib["style"] = ";".join(kept)
            else:
                el.attrib.pop("style", None)
    return root


_MEDIA_DIR_RE = re.compile(r"(^|/)(media|images?|pictures)/", re.IGNORECASE)
_MEDIA_SUFFIXES = (
    ".wmf", ".emf", ".emz", ".wmz", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".tif", ".tiff", ".webp", ".ico", ".svg", ".svgz", ".heic", ".avif",
)


def _looks_like_media_entry(name, payload):
    lower = name.lower()
    if _MEDIA_DIR_RE.search(lower) or lower.endswith(_MEDIA_SUFFIXES):
        return True
    if payload[:4] in (b"\xd7\xcd\xc6\x9a", b"\x01\x00\x09\x00"):
        return True
    return payload[:4] == b"\x01\x00\x00\x00" and payload[40:44] == b" EMF"


def _looks_like_svg(payload):
    head = payload[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in payload[:4096].lower())


def _decode_data_uri_body(params, body):
    if b"base64" in params.lower():
        compact = re.sub(rb"\s+", b"", body)
        compact = compact[: len(compact) - (len(compact) % 4) or None]
        try:
            return base64.b64decode(compact + b"=" * (-len(compact) % 4), validate=False)
        except Exception:
            return None
    try:
        return unquote_to_bytes(bytes(body))
    except Exception:
        return None


def _extract_data_uri_images(raw, budget):
    from PIL import Image

    seen = set()
    for match in _DATA_URI_RE.finditer(raw):
        budget.check()
        params, body = match.group(1) or b"", match.group(2)
        key = hash(bytes(body[:4096]))
        if key in seen:
            continue
        seen.add(key)
        payload = _decode_data_uri_body(params, body)
        if payload is None:
            continue
        try:
            with Image.open(io.BytesIO(payload)) as im:
                im.load()
                budget.spend(*im.size)
                yield im.convert("RGB").copy()
                continue
        except Exception:
            pass
        if _looks_like_svg(payload):
            try:
                nested = _rasterize_svg(payload)
                budget.spend(*nested.size)
                yield nested
            except Exception:
                continue


def _which_svg_rasterizer():
    if which("resvg"):
        return "resvg"
    return None


def _run_limited(cmd, timeout, max_bytes=1_500_000_000, max_file_bytes=2_000_000_000):
    import resource

    def _limits():
        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}
    result = subprocess.run(
        cmd, timeout=timeout, capture_output=True, preexec_fn=_limits, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed (exit {result.returncode}): {result.stderr[:500]!r}")
    return result


def _rasterize_svg(svg_bytes, max_dim=2048, timeout=15):
    from PIL import Image

    tool = _which_svg_rasterizer()
    if tool is None:
        raise UnsupportedFormat("no SVG rasterizer available (resvg required; rsvg-convert is not used, its URL handling has a weaker security history)")

    fd, svg_path = tempfile.mkstemp(suffix=".svg")
    with os.fdopen(fd, "wb") as f:
        f.write(svg_bytes)
    fd2, png_path = tempfile.mkstemp(suffix=".png")
    os.close(fd2)

    try:
        cmd = ["resvg", "--width", str(max_dim), svg_path, png_path]
        _run_limited(cmd, timeout=timeout)
        with Image.open(png_path) as im:
            im.load()
            return im.convert("RGB").copy()
    finally:
        os.unlink(svg_path)
        if os.path.exists(png_path):
            os.unlink(png_path)


def _jpeg_true_end(data):
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    n = len(data)
    while pos + 1 < n:
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        pos += 2
        if marker == 0xD8 or marker == 0x01 or (0xD0 <= marker <= 0xD7):
            continue
        if marker == 0xD9:
            return pos
        if pos + 2 > n:
            return None
        seg_len = int.from_bytes(data[pos:pos + 2], "big")
        if seg_len < 2:
            return None
        if marker == 0xDA:
            pos += seg_len
            while pos < n:
                if data[pos] == 0xFF:
                    if pos + 1 >= n:
                        return None
                    nxt = data[pos + 1]
                    if nxt == 0x00 or (0xD0 <= nxt <= 0xD7):
                        pos += 2
                        continue
                    break
                pos += 1
            continue
        pos += seg_len
    return None


def _png_true_end(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        end = pos + 8 + length + 4
        if end > n:
            return None
        if ctype == b"IEND":
            return end
        pos = end
    return None


def _gif_true_end(data):
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    n = len(data)
    if n < 13:
        return None
    packed = data[10]
    pos = 13
    if packed & 0x80:
        pos += 3 * (2 ** ((packed & 0x07) + 1))
    while pos < n:
        b = data[pos]
        if b == 0x3B:
            return pos + 1
        if b == 0x21:
            pos += 2
            if pos > n:
                return None
            while pos < n:
                sub_len = data[pos]
                pos += 1
                if sub_len == 0:
                    break
                pos += sub_len
                if pos > n:
                    return None
            continue
        if b == 0x2C:
            if pos + 10 > n:
                return None
            local_packed = data[pos + 9]
            pos += 10
            if local_packed & 0x80:
                pos += 3 * (2 ** ((local_packed & 0x07) + 1))
            if pos >= n:
                return None
            pos += 1
            while pos < n:
                sub_len = data[pos]
                pos += 1
                if sub_len == 0:
                    break
                pos += sub_len
                if pos > n:
                    return None
            continue
        return None
    return None


def _riff_true_end(data):
    if data[:4] != b"RIFF" or len(data) < 8:
        return None
    declared = int.from_bytes(data[4:8], "little")
    return 8 + declared


def _bmp_true_end(data):
    if data[:2] != b"BM" or len(data) < 6:
        return None
    return int.from_bytes(data[2:6], "little")


_TRAILING_SIGNATURES = (
    b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF", b"BM",
)


class _SliceReader(io.RawIOBase):
    """File-like view starting at an offset."""

    def __init__(self, view, offset):
        self._view = view
        self._start = offset
        self._pos = offset

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos - self._start

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self._pos = self._start + offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = len(self._view) + offset
        self._pos = max(self._start, min(self._pos, len(self._view)))
        return self._pos - self._start

    def readinto(self, target):
        available = len(self._view) - self._pos
        count = min(len(target), max(available, 0))
        if count <= 0:
            return 0
        target[:count] = self._view[self._pos:self._pos + count]
        self._pos += count
        return count


def _decode_appended_images(data, start, max_images=4, max_attempts=32, budget=None):
    from PIL import Image

    offsets = set()
    for sig in _TRAILING_SIGNATURES:
        pos = start
        while len(offsets) < max_attempts:
            found = data.find(sig, pos)
            if found == -1:
                break
            offsets.add(found)
            pos = found + 1

    out = []
    attempts = 0
    view = memoryview(data)
    for offset in sorted(offsets):
        if len(out) >= max_images or attempts >= max_attempts:
            break
        attempts += 1
        if budget is not None:
            budget.check()
        try:
            with Image.open(io.BufferedReader(_SliceReader(view, offset))) as im:
                im.load()
                out.append(im.convert("RGB").copy())
        except Exception:
            continue
    return out


def _check_raster_trailing_data(data, flags):
    n = len(data)
    end = None
    if data[:2] == b"\xff\xd8":
        end = _jpeg_true_end(data)
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        end = _png_true_end(data)
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        end = _gif_true_end(data)
    elif data[:4] == b"RIFF":
        end = _riff_true_end(data)
    elif data[:2] == b"BM":
        end = _bmp_true_end(data)
    if end is not None and 0 < end < n:
        flags["trailing_data"] = True
        flags["trailing_data_bytes"] = n - end
        return end
    return None


def _icc_looks_crafted(icc_bytes):
    try:
        if len(icc_bytes) < 132:
            return False
        tag_count = int.from_bytes(icc_bytes[128:132], "big")
        pos = 132
        tags = {}
        for _ in range(tag_count):
            if pos + 12 > len(icc_bytes):
                break
            sig = icc_bytes[pos:pos + 4]
            offset = int.from_bytes(icc_bytes[pos + 4:pos + 8], "big")
            size = int.from_bytes(icc_bytes[pos + 8:pos + 12], "big")
            tags[sig] = (offset, size)
            pos += 12

        for sig in (b"rTRC", b"gTRC", b"bTRC", b"kTRC"):
            if sig not in tags:
                continue
            offset, _size = tags[sig]
            if offset + 12 > len(icc_bytes):
                continue
            tag_type = icc_bytes[offset:offset + 4]
            if tag_type != b"curv":
                continue
            count = int.from_bytes(icc_bytes[offset + 8:offset + 12], "big")
            if count <= 1:
                continue
            entries_start = offset + 12
            entries_end = entries_start + count * 2
            if entries_end > len(icc_bytes):
                continue
            values = struct.unpack(f">{count}H", icc_bytes[entries_start:entries_end])
            if any(values[i] > values[i + 1] for i in range(len(values) - 1)):
                return True
        return False
    except Exception:
        return False


def _alpha_views(im):
    from PIL import Image

    if im.mode not in ("RGBA", "LA", "PA") and "transparency" not in im.info:
        return []
    rgba = im.convert("RGBA")
    alpha = rgba.split()[3]
    if alpha.getextrema() == (255, 255):
        return []
    white_bg = Image.new("RGB", rgba.size, (255, 255, 255))
    white_bg.paste(rgba, mask=alpha)
    black_bg = Image.new("RGB", rgba.size, (0, 0, 0))
    black_bg.paste(rgba, mask=alpha)
    alpha_plane = alpha.convert("RGB")
    return [("alpha-white", white_bg), ("alpha-black", black_bg), ("alpha-plane", alpha_plane)]


def _png_ancillary_payload(ctype, chunk_data):
    if ctype in (b"zTXt", b"iCCP"):
        idx = chunk_data.find(b"\x00")
        if idx == -1 or idx + 2 > len(chunk_data):
            return None
        return chunk_data[idx + 2:]
    if ctype == b"iTXt":
        idx = chunk_data.find(b"\x00")
        if idx == -1 or idx + 2 > len(chunk_data):
            return None
        compression_flag = chunk_data[idx + 1]
        if compression_flag != 1:
            return None
        pos = idx + 3
        idx2 = chunk_data.find(b"\x00", pos)
        if idx2 == -1:
            return None
        pos = idx2 + 1
        idx3 = chunk_data.find(b"\x00", pos)
        if idx3 == -1:
            return None
        return chunk_data[idx3 + 1:]
    return None


def _decompress_exceeds(compressed, cap_bytes):
    d = zlib.decompressobj()
    total = 0
    try:
        out = d.decompress(compressed, cap_bytes + 1)
        total += len(out)
        while d.unconsumed_tail and total <= cap_bytes:
            out = d.decompress(d.unconsumed_tail, cap_bytes + 1 - total)
            total += len(out)
            if not out:
                break
        return total > cap_bytes
    except Exception:
        return True


def _png_ancillary_bomb_check(data, cap_bytes=8 * 1024 * 1024):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + length]
        if ctype == b"IEND":
            break
        if ctype in (b"zTXt", b"iTXt", b"iCCP"):
            payload = _png_ancillary_payload(ctype, chunk_data)
            if payload is not None and _decompress_exceeds(payload, cap_bytes):
                return True
        pos += 8 + length + 4
    return False


def _detect_app14_cmyk(head):
    return b"Adobe" in head and b"\xff\xee" in head


def _detect_hdr_gainmap(head):
    return any(marker in head for marker in _HDR_GAINMAP_MARKERS)


def _cmyk_naive_view(path):
    from PIL import Image

    try:
        with Image.open(path) as im:
            if im.mode != "CMYK":
                return None
            im.load()
            return im.convert("RGB").copy()
    except Exception:
        return None


class Extraction:
    def __init__(self, path, budget=None):
        self.path = path
        self.budget = budget or Budget()
        self.flags = {}
        self.format = detect_format(path)

    def iter_frames(self):
        dispatch = {
            "raster": self._iter_raster,
            "svg": self._iter_svg,
            "pdf": self._iter_pdf,
            "djvu": self._iter_djvu,
            "video": self._iter_video,
            "ogg": self._iter_ogg,
            "office": self._iter_office,
            "office-legacy": self._iter_office_legacy,
        }
        handler = dispatch.get(self.format)
        if handler is None:
            raise UnsupportedFormat(self.format)
        yield from handler()

    def _iter_raster(self):
        from PIL import Image, ImageCms, ImageOps, ImageSequence

        with open(self.path, "rb") as f:
            data = f.read()
        head = data[:65536]

        trailing_end = _check_raster_trailing_data(data, self.flags)

        if head[:8] == b"\x89PNG\r\n\x1a\n" and _png_ancillary_bomb_check(data):
            self.flags["png_ancillary_bomb"] = True
            raise BudgetExceeded("PNG ancillary chunk decompression exceeds cap")

        if head[:2] == b"\xff\xd8":
            if _detect_app14_cmyk(head):
                self.flags["adobe_app14_cmyk"] = True
            if _detect_hdr_gainmap(head):
                self.flags["hdr_gainmap_detected"] = True

        with Image.open(self.path) as probe:
            src_format = (probe.format or "").upper()
            n_frames = getattr(probe, "n_frames", 1)
            icc_bytes = probe.info.get("icc_profile")

        if icc_bytes and _icc_looks_crafted(icc_bytes):
            self.flags["icc_looks_crafted"] = True

        is_single_jpeg = src_format == "JPEG" and n_frames == 1

        with Image.open(self.path) as im:
            for index, raw in enumerate(ImageSequence.Iterator(im)):
                raw = ImageOps.exif_transpose(raw) or raw
                self.budget.spend(*raw.size)

                frame_ext = ".jpg" if is_single_jpeg else ".png"
                fd, tmp_in = tempfile.mkstemp(suffix=frame_ext)
                os.close(fd)
                try:
                    raw.convert("RGB" if frame_ext == ".jpg" else "RGBA").save(tmp_in)
                    canonical_path = canonicalize(tmp_in)
                finally:
                    os.unlink(tmp_in)

                try:
                    with Image.open(canonical_path) as canon:
                        naive = canon.convert("RGB").copy()
                finally:
                    os.unlink(canonical_path)

                yield Frame(f"frame:{index}", naive)

                if icc_bytes:
                    try:
                        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
                        dst_profile = ImageCms.createProfile("sRGB")
                        icc_view = ImageCms.profileToProfile(
                            raw.convert("RGB"), src_profile, dst_profile, outputMode="RGB",
                        )
                        yield Frame(f"frame:{index}:icc-applied", icc_view)
                    except Exception:
                        pass

                for name, img in _alpha_views(raw):
                    yield Frame(f"frame:{index}:{name}", img)

                if is_single_jpeg and self.flags.get("adobe_app14_cmyk"):
                    cmyk_view = _cmyk_naive_view(self.path)
                    if cmyk_view is not None:
                        self.budget.spend(*cmyk_view.size)
                        yield Frame(f"frame:{index}:cmyk-naive", cmyk_view)

        if trailing_end is not None:
            for offset, appended in enumerate(
                _decode_appended_images(data, trailing_end, budget=self.budget)
            ):
                self.budget.spend(*appended.size)
                yield Frame(f"appended:{offset}", appended)


    def _iter_svg(self):
        from defusedxml.ElementTree import parse as defused_parse

        with open(self.path, "rb") as f:
            raw = f.read()

        try:
            tree = defused_parse(self.path)
        except Exception as exc:
            raise UnsupportedFormat(f"unparsable/unsafe SVG: {exc}") from exc

        root = tree.getroot()

        authored_root = copy.deepcopy(root)
        _svg_strip_dangerous(authored_root)
        authored_bytes = ET.tostring(authored_root)
        self.budget.spend(1024, 1024)
        yield Frame("svg:authored", _rasterize_svg(authored_bytes))

        reveal_root = copy.deepcopy(authored_root)
        _svg_strip_concealment(reveal_root)
        reveal_bytes = ET.tostring(reveal_root)
        self.budget.spend(1024, 1024)
        yield Frame("svg:reveal", _rasterize_svg(reveal_bytes))

        for index, image in enumerate(_extract_data_uri_images(raw, self.budget)):
            yield Frame(f"svg:data-uri:{index}", image)

    def _iter_pdf(self):
        try:
            yield from self._iter_pdf_pdfium()
        except ImportError:
            yield from self._iter_pdf_poppler()

    def _iter_pdf_pdfium(self):
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(self.path)
        try:
            page_count = len(pdf)
            for page_index in range(page_count):
                if self.budget.frames_spent >= self.budget.max_frames:
                    break
                page = pdf.get_page(page_index)
                try:
                    width = page.get_width()
                    height = page.get_height()
                    self.budget.spend(width, height)
                    scale = min(2.0, 1600 / max(width, height, 1))
                    bitmap = page.render(scale=max(scale, 0.1))
                    try:
                        yield Frame(f"pdf:page{page_index}", bitmap.to_pil().convert("RGB"))
                    finally:
                        bitmap.close()

                    for obj_index, obj in enumerate(
                        page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,))
                    ):
                        try:
                            img_bitmap = obj.get_bitmap(render=False)
                            try:
                                img = img_bitmap.to_pil().convert("RGB")
                            finally:
                                img_bitmap.close()
                            self.budget.spend(*img.size)
                            yield Frame(f"pdf:page{page_index}:image{obj_index}", img)
                        except BudgetExceeded:
                            raise
                        except Exception:
                            continue
                finally:
                    page.close()
        finally:
            pdf.close()

    def _iter_pdf_poppler(self):
        from PIL import Image

        if which("pdftoppm") is None:
            raise UnsupportedFormat("no PDF renderer available (pypdfium2/pdftoppm)")

        out_dir = tempfile.mkdtemp(prefix="imgguard-pdf-")
        try:
            prefix = os.path.join(out_dir, "page")
            _run_limited(["pdftoppm", "-png", "-r", "100", self.path, prefix], timeout=20)
            for name in sorted(os.listdir(out_dir)):
                if self.budget.frames_spent >= self.budget.max_frames:
                    break
                path = os.path.join(out_dir, name)
                with Image.open(path) as im:
                    self.budget.spend(*im.size)
                    yield Frame(f"pdf:{name}", im.convert("RGB").copy())

            if which("pdfimages") is not None:
                img_prefix = os.path.join(out_dir, "img")
                try:
                    _run_limited(["pdfimages", "-png", self.path, img_prefix], timeout=20)
                except Exception:
                    return
                for name in sorted(os.listdir(out_dir)):
                    if not name.startswith("img-"):
                        continue
                    if self.budget.frames_spent >= self.budget.max_frames:
                        break
                    path = os.path.join(out_dir, name)
                    try:
                        with Image.open(path) as im:
                            self.budget.spend(*im.size)
                            yield Frame(f"pdf:{name}", im.convert("RGB").copy())
                    except Exception:
                        continue
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def _probe_video_duration(self):
        if which("ffprobe") is None:
            return None
        try:
            result = _run_limited(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", self.path],
                timeout=15,
            )
            return float(result.stdout.decode().strip())
        except Exception:
            return None

    def _duration_is_trustworthy(self, duration):
        fd, out_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        os.unlink(out_path)
        try:
            check_ts = duration + 2.0
            cmd = ["ffmpeg", "-y", "-ss", f"{check_ts:.3f}", "-i", self.path, "-frames:v", "1", out_path]
            _run_limited(cmd, timeout=15, max_bytes=4_000_000_000)
            found_content_past_claimed_end = os.path.exists(out_path) and os.path.getsize(out_path) > 0
            return not found_content_past_claimed_end
        except Exception:
            return False
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)

    def _djvu_page_count(self):
        if which("djvused"):
            try:
                result = _run_limited(["djvused", "-e", "n", self.path], timeout=15)
                return max(1, min(10000, int(result.stdout.decode().strip())))
            except Exception:
                pass
        return 1

    def _iter_djvu(self):
        from PIL import Image

        if which("ddjvu") is None:
            raise UnsupportedFormat("ddjvu not available for DjVu extraction")

        page_count = self._djvu_page_count()
        for page in range(1, page_count + 1):
            if self.budget.frames_spent >= self.budget.max_frames:
                break
            fd, tiff_path = tempfile.mkstemp(suffix=".tiff")
            os.close(fd)
            try:
                _run_limited(
                    ["ddjvu", "-format=tiff", f"-page={page}", self.path, tiff_path], timeout=30,
                )
                with Image.open(tiff_path) as im:
                    self.budget.spend(*im.size)
                    yield Frame(f"djvu:page{page}", im.convert("RGB").copy())
            finally:
                if os.path.exists(tiff_path):
                    os.unlink(tiff_path)

    def _iter_ogg(self):
        if not self._probe_video_streams():
            return
        yield from self._iter_video()

    def _iter_office(self):
        import zipfile
        from PIL import Image

        max_entries = 512
        max_entry_bytes = 64 * 1024 * 1024
        total_bytes_cap = 256 * 1024 * 1024
        read_total = 0
        examined = 0
        undecodable = []

        try:
            archive = zipfile.ZipFile(self.path)
        except Exception as exc:
            raise UnsupportedFormat(f"unreadable office container: {exc}") from exc

        with archive:
            for info in archive.infolist():
                if examined >= max_entries or read_total >= total_bytes_cap:
                    break
                if info.is_dir() or info.file_size == 0:
                    continue
                if info.file_size > max_entry_bytes:
                    self.flags["office_oversized_entry"] = True
                    raise BudgetExceeded(
                        f"office entry too large to examine: {info.filename} ({info.file_size} bytes)"
                    )
                examined += 1
                self.budget.check()
                try:
                    with archive.open(info) as handle:
                        payload = handle.read(max_entry_bytes + 1)
                except Exception:
                    continue
                if len(payload) > max_entry_bytes:
                    self.flags["office_oversized_entry"] = True
                    raise BudgetExceeded(f"office entry exceeded its declared size: {info.filename}")
                read_total += len(payload)

                try:
                    with Image.open(io.BytesIO(payload)) as im:
                        im.load()
                        self.budget.spend(*im.size)
                        yield Frame(f"office:{info.filename}", im.convert("RGB").copy())
                        continue
                except BudgetExceeded:
                    raise
                except Exception:
                    pass

                if _looks_like_svg(payload):
                    try:
                        nested = _rasterize_svg(payload)
                    except Exception:
                        undecodable.append(info.filename)
                        continue
                    self.budget.spend(*nested.size)
                    yield Frame(f"office:{info.filename}", nested)
                    continue

                if _looks_like_media_entry(info.filename, payload):
                    undecodable.append(info.filename)

        if undecodable:
            self.flags["office_undecodable_media"] = True
            raise BudgetExceeded(
                "office media entries could not be decoded: " + ", ".join(undecodable[:5])
            )

    def _iter_office_legacy(self):
        with open(self.path, "rb") as f:
            data = f.read()

        found = 0
        for index, image in enumerate(
            _decode_appended_images(data, 0, max_images=16, budget=self.budget)
        ):
            found += 1
            self.budget.spend(*image.size)
            yield Frame(f"office-legacy:{index}", image)

        if b"\xd7\xcd\xc6\x9a" in data or b" EMF" in data:
            self.flags["office_undecodable_media"] = True
            raise BudgetExceeded("legacy office document contains undecodable vector media")

    def _probe_video_streams(self):
        if which("ffprobe") is None:
            return [None]
        try:
            result = _run_limited(
                ["ffprobe", "-v", "error", "-select_streams", "v",
                 "-show_entries", "stream=index", "-of", "csv=p=0", self.path],
                timeout=15,
            )
            return [int(x) for x in result.stdout.decode().split() if x.strip().lstrip("-").isdigit()]
        except Exception:
            return [None]

    def _iter_video(self):
        from PIL import Image

        if which("ffmpeg") is None:
            raise UnsupportedFormat("ffmpeg not available for video extraction")

        remaining = self.budget.max_frames - self.budget.frames_spent
        if remaining <= 0:
            return
        duration = self._probe_video_duration()
        if duration and duration > 0 and not self._duration_is_trustworthy(duration):
            self.flags["video_duration_mismatch"] = True
            raise BudgetExceeded("video duration metadata does not match actual content")

        streams = self._probe_video_streams() or [None]
        if len(streams) > 1:
            self.flags["video_multiple_streams"] = True
        budget_total = min(remaining, 32)
        per_stream = max(2, budget_total // len(streams))

        out_dir = tempfile.mkdtemp(prefix="imgguard-video-")
        try:
            for stream in streams:
                label = "" if stream is None else f"s{stream}:"
                mapping = [] if stream is None else ["-map", f"0:{stream}"]

                if duration and duration > 0:
                    for index in range(per_stream):
                        if self.budget.frames_spent >= self.budget.max_frames:
                            return
                        self.budget.check()
                        ts = duration * (index + 0.5) / per_stream
                        out_path = os.path.join(out_dir, f"{label.replace(':','_')}frame-{index:04d}.png")
                        cmd = [
                            "ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", self.path,
                            *mapping, "-frames:v", "1", out_path,
                        ]
                        try:
                            _run_limited(cmd, timeout=15, max_bytes=4_000_000_000)
                        except Exception:
                            continue
                        if not os.path.exists(out_path):
                            continue
                        with Image.open(out_path) as im:
                            self.budget.spend(*im.size)
                            yield Frame(f"video:{label}t{ts:.1f}", im.convert("RGB").copy())
                    continue

                stream_dir = tempfile.mkdtemp(prefix="imgguard-vseq-", dir=out_dir)
                pattern = os.path.join(stream_dir, "frame-%04d.png")
                cmd = [
                    "ffmpeg", "-y", "-i", self.path, *mapping,
                    "-vf", "fps=1", "-frames:v", str(per_stream),
                    pattern,
                ]
                try:
                    _run_limited(cmd, timeout=20, max_bytes=4_000_000_000)
                except Exception:
                    continue
                for name in sorted(os.listdir(stream_dir)):
                    if self.budget.frames_spent >= self.budget.max_frames:
                        return
                    with Image.open(os.path.join(stream_dir, name)) as im:
                        self.budget.spend(*im.size)
                        yield Frame(f"video:{label}{name}", im.convert("RGB").copy())
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
