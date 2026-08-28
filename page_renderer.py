"""
Renders a turath.io page (fetched via api.turath.io) into a page-styled
PNG image — Arabic-aware, with a header (book title) and footer (page
number).

Uses Pillow's native libraqm text shaping (direction='rtl', language='ar')
for correct Arabic letter joining and RTL layout — no manual reshaping
library needed. Requires Pillow built with libraqm support; check with:
    from PIL import features; features.check('raqm')
If that prints False, `pip install --upgrade pillow` or install libraqm
on the system (most prebuilt Pillow wheels from PyPI already include it).

Usage:
    from page_renderer_v2 import fetch_and_render_page
    fetch_and_render_page(book_id=147927, page_number=1, output_path="page.png")
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import pymupdf as fitz  # PyMuPDF (new import name; fitz alias kept for readability below)
import requests
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("scanpages.page_renderer")


def _request_with_retry(url: str, retries: int = 3, backoff: float = 1.0,
                        **kwargs) -> requests.Response:
    """GET with exponential-backoff retry on transient HTTP / network errors."""
    kwargs.setdefault("timeout", 15)
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = float(resp.headers.get("Retry-After", backoff * (2 ** attempt)))
                logger.warning("HTTP %d from %s — retrying in %.1fs (attempt %d/%d)",
                               resp.status_code, url, retry_after, attempt + 1, retries)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            wait = backoff * (2 ** attempt)
            logger.warning("Network error on %s: %s — retrying in %.1fs (attempt %d/%d)",
                           url, exc, wait, attempt + 1, retries)
            time.sleep(wait)
    raise last_exc

# ---- Config ---------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent
FONT_PATH = str(_BASE_DIR / "amiri_font" / "Amiri-1.001" / "Amiri-Regular.ttf")
FONT_PATH_BOLD = str(_BASE_DIR / "amiri_font" / "Amiri-1.001" / "Amiri-Bold.ttf")

PAGE_WIDTH = 1000
MARGIN = 70
BODY_FONT_SIZE = 30
HEADER_FONT_SIZE = 22
FOOTER_FONT_SIZE = 20
LINE_SPACING = 16

BG_COLOR = (250, 244, 229)       # aged-paper cream
TEXT_COLOR = (35, 25, 15)
RULE_COLOR = (180, 160, 130)


def _check_raqm() -> bool:
    try:
        from PIL import features
        return bool(features.check("raqm"))
    except Exception:
        return False


_HAS_RAQM = _check_raqm()
RTL_KW = {"direction": "rtl", "language": "ar"} if _HAS_RAQM else {}

if not _HAS_RAQM:
    print(
        "[page_renderer] Pillow was built without libraqm -- falling back to "
        "manual Arabic reshaping (lower quality than raqm, but functional). "
        "This is common on Windows; Linux hosts (e.g. your production bot "
        "server) typically have raqm support out of the box via pip."
    )
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        _reshaper = arabic_reshaper.ArabicReshaper(
            configuration={"delete_harakat": False, "support_ligatures": True}
        )

        def _prepare_rtl(text: str) -> str:
            """Pre-shapes and visually reorders Arabic text for plain (no
            raqm) rendering. Only used when libraqm isn't available."""
            return get_display(_reshaper.reshape(text))
    except ImportError:
        raise ImportError(
            "Pillow lacks libraqm AND arabic-reshaper/python-bidi aren't "
            "installed -- Arabic text cannot be rendered at all. Run: "
            "pip install arabic-reshaper python-bidi"
        )
else:
    def _prepare_rtl(text: str) -> str:
        return text  # raqm handles shaping/reordering itself via RTL_KW


# ---- API calls --------------------------------------------------------

def fetch_turath_page(book_id: int, page_number: int) -> dict:
    resp = _request_with_retry(
        "https://api.turath.io/page",
        params={"book_id": book_id, "pg": page_number, "ver": 3},
        headers={"Accept": "application/json"},
    )
    data = resp.json()
    if not data.get("meta") and not data.get("text"):
        raise ValueError(f"Book {book_id}, page {page_number} not found")
    return {"meta": json.loads(data["meta"]), "text": data["text"]}


def get_book_meta(book_id: int) -> dict:
    """Fetches book.meta (NOT the indexes) -- this is the only /book call that
    includes pdf_links, needed to locate the real scanned PDF."""
    resp = _request_with_retry(
        "https://api.turath.io/book",
        params={"id": book_id, "include": "meta", "ver": 3},
        headers={"Accept": "application/json"},
    )
    return resp.json()


def resolve_pdf_url(book_id: int, volume: str | int) -> tuple[str, int] | None:
    """Replicates turath's client-side file-matching logic (from their
    reader's `j()` function) to build the real scanned-PDF URL for a given
    volume. Returns (url, total_file_count) or None if this book has no
    PDF. total_file_count matters because some multi-volume books have a
    SEPARATE PDF per volume (file count == volume count) while others have
    ALL volumes merged into a single combined PDF (file count == 1) -- the
    two cases need different page-numbering logic in render_scan_page."""
    meta = get_book_meta(book_id).get("meta", {})
    pdf_links = meta.get("pdf_links") or {}
    files = [f for f in (pdf_links.get("files") or []) if isinstance(f, str) and f.strip()]
    if not files:
        return None

    volume = str(volume)
    chosen = files[0]
    if len(files) > 1:
        chosen = next((f for f in files if f.endswith("|0")), None)
        if not chosen:
            by_key = {}
            for f in files:
                if "|" in f:
                    key = f.split("|")[1]
                else:
                    m = re.search(r"0*(\d+)", f)
                    key = m.group(1) if m else ""
                by_key[key] = f
            chosen = by_key.get(volume, "")
    filename = chosen.split("|")[0]
    if not filename:
        return None

    root = (pdf_links.get("root") or "").strip()
    combined = f"{root.rstrip('/')}/{filename}" if root else filename

    if "archive.org" in combined:
        # turath mirrors some archive.org-hosted PDFs under a different path
        # convention: archive/{identifier}_=_{filename}.pdf (only the FIRST
        # slash after the archive.org download prefix becomes "_=_"). This
        # must be checked on the COMBINED root+filename path, since some
        # books put the archive.org domain in `root` rather than embedding
        # it directly in the file entry.
        stripped = combined.replace("https://archive.org/download/", "")
        combined = "archive/" + stripped.replace("/", "_=_", 1)

    return f"https://files.turath.io/pdf/{quote(combined)}", len(files)


_pdf_cache_dir = Path(tempfile.gettempdir()) / "turath_pdfs"
_pdf_cache_dir.mkdir(exist_ok=True)

# Cache cap in bytes -- big books (a full multi-volume combined PDF can be
# several hundred MB) are kept in /tmp, so the cache must be bounded or the
# disk fills up and downloads start failing with OSError(28). Older entries
# are evicted LRU-style once the cap is crossed.
_PDF_CACHE_MAX_BYTES = 2 * 1024**3  # 2 GiB

# PDFs are deduplicated by content hash (combined multi-volume books get
# cached once, not once per volume, which both saves disk and fixes the
# "No space left on device" errors).
_pdf_cache_hashes: dict[str, str] = {}
_cache_loaded = False


def _load_pdf_cache_index() -> None:
    """Scans the cache dir once at startup to rebuild the path->hash index."""
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        for p in _pdf_cache_dir.iterdir():
            if p.is_file():
                _pdf_cache_hashes[str(p)] = _hash_file(p)
    except OSError:
        pass


def _hash_file(path: Path) -> str:
    """Content hash of a cached PDF (sample first/last 64 KiB -- enough to
    catch a different file cheaply, without re-reading the whole blob)."""
    size = path.stat().st_size
    if size <= 0:
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(min(64 * 1024, size)))
        if size > 64 * 1024:
            f.seek(max(0, size - 64 * 1024))
            h.update(f.read(64 * 1024))
    return h.hexdigest()


def _evict_pdf_cache() -> None:
    """Deletes oldest files until the cache is back under the cap."""
    try:
        files = [
            (p.stat().st_mtime, p) for p in _pdf_cache_dir.iterdir() if p.is_file()
        ]
    except OSError:
        return
    total = sum(p.stat().st_size for _, p in files)
    if total <= _PDF_CACHE_MAX_BYTES:
        return
    for _, p in sorted(files):
        if total <= _PDF_CACHE_MAX_BYTES:
            break
        try:
            size = p.stat().st_size
            p.unlink()
            _pdf_cache_hashes.pop(str(p), None)
            total -= size
        except OSError:
            pass


def _download_pdf(url: str, book_id: int, volume: str) -> Path:
    """Downloads (and caches) a book's PDF for a given volume. Cache entries
    are keyed by a SHA-256 content hash so identical PDFs (e.g. the same
    combined file requested for different volumes) are only stored once,
    and the cache is kept under a size cap so a full disk can't recur."""
    _load_pdf_cache_index()
    cache_path = _pdf_cache_dir / f"{book_id}_{volume}.pdf"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    resp = _request_with_retry(url, timeout=60, stream=True, retries=3, backoff=2.0)
    resp.raise_for_status()

    tmp_path = _pdf_cache_dir / f".{book_id}_{volume}.part"
    try:
        with tmp_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        digest = _hash_file(tmp_path)
        if digest:
            for existing, existing_hash in _pdf_cache_hashes.items():
                if existing_hash == digest:
                    # Remember this volume's alias so a repeat request for
                    # the same (book, volume) reuses the file without a
                    # duplicate download. A hardlink keeps disk usage to a
                    # single copy; if that isn't possible (cross-device,
                    # Windows without NTFS hardlinks), just use the cache
                    # entry as-is and let the alias stay in-memory.
                    if str(cache_path) != existing:
                        try:
                            os.link(existing, cache_path)
                        except OSError:
                            pass
                        _pdf_cache_hashes[str(cache_path)] = digest
                    return Path(existing)
        os.replace(tmp_path, cache_path)
        _pdf_cache_hashes[str(cache_path)] = digest
    finally:
        tmp_path.unlink(missing_ok=True)

    _evict_pdf_cache()
    return cache_path


def _resolve_physical_page(book_id: int, volume: str | int, printed_page: int, file_count: int) -> int:
    """Shared page-numbering logic used by both render_scan_page and
    render_scan_page_with_front -- see render_scan_page's docstring for
    the split-per-volume vs combined-PDF distinction."""
    try:
        volumes = _get_book_info_cached(book_id)["indexes"].get("volumes") or []
    except Exception:
        volumes = []
    is_combined_pdf = file_count == 1 and len(volumes) > 1
    if is_combined_pdf:
        return resolve_printed_page(book_id, volume, printed_page)
    return printed_page


def _save_page(doc: "fitz.Document", page_index: int, output_path: str, dpi: int) -> str:
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    pix.save(output_path)
    return output_path


def render_scan_page(
    book_id: int,
    volume: str | int,
    printed_page: int,
    output_path: str,
    dpi: int = 200,
) -> str | None:
    """Fetches and renders the ACTUAL scanned page image (not a text
    re-rendering), by downloading the book's real PDF and extracting the
    correct page. Returns None if this book has no PDF (caller should fall
    back to render_page_image / fetch_and_render_page instead).

    Page numbering has two cases:
      - Split-per-volume PDFs: each volume's file starts at physical page 1,
        matching turath's own printed-page numbering directly.
      - Single combined PDF covering multiple volumes: the file's physical
        pages run continuously across ALL volumes, while turath's printed
        page numbers reset to 1 at the start of each volume -- so we
        translate (volume, printed_page) to turath's internal running page
        count via resolve_printed_page() instead of using printed_page
        directly."""
    result = resolve_pdf_url(book_id, volume)
    if result is None:
        return None
    pdf_url, file_count = result

    physical_page = _resolve_physical_page(book_id, volume, printed_page, file_count)
    pdf_path = _download_pdf(pdf_url, book_id, str(volume))
    doc = fitz.open(pdf_path)
    try:
        page_index = physical_page - 1  # 1-indexed -> 0-indexed
        if not (0 <= page_index < doc.page_count):
            raise ValueError(
                f"Printed page {printed_page} out of range (PDF has {doc.page_count} pages)"
            )
        return _save_page(doc, page_index, output_path, dpi)
    finally:
        doc.close()


def render_scan_page_with_front(
    book_id: int,
    volume: str | int,
    printed_page: int,
    output_path: str,
    front_output_path: str,
    dpi: int = 200,
) -> tuple[str, str] | None:
    """Like render_scan_page, but also extracts physical page 1 (the PDF's
    own front/title page) from the SAME downloaded file, for citation
    purposes -- e.g. showing which edition/volume a quoted page comes
    from. Returns (front_page_path, requested_page_path), or None if this
    book has no PDF. Downloads and opens the PDF only once for both pages."""
    result = resolve_pdf_url(book_id, volume)
    if result is None:
        return None
    pdf_url, file_count = result

    physical_page = _resolve_physical_page(book_id, volume, printed_page, file_count)
    pdf_path = _download_pdf(pdf_url, book_id, str(volume))
    doc = fitz.open(pdf_path)
    try:
        page_index = physical_page - 1
        if not (0 <= page_index < doc.page_count):
            raise ValueError(
                f"Printed page {printed_page} out of range (PDF has {doc.page_count} pages)"
            )
        front_path = _save_page(doc, 0, front_output_path, dpi)
        content_path = _save_page(doc, page_index, output_path, dpi)
        return front_path, content_path
    finally:
        doc.close()


def get_book_info(book_id: int) -> dict:
    """Fetches book.indexes (page_map, print_pg_to_pg, volume_bounds,
    volumes, headings) -- NOT meta/pdf_links. Use get_book_meta() for
    pdf_links instead."""
    resp = _request_with_retry(
        "https://api.turath.io/book",
        params={"id": book_id, "include": "indexes", "ver": 3},
        headers={"Accept": "application/json"},
    )
    return resp.json()


def get_book_file(book_id: int) -> dict:
    """Fetches the FULL book dump from the file CDN (not the /book API) —
    includes meta.has_pdf and indexes.pdf_base/indexes.pdfs, which the
    /book endpoint (get_book_info) does NOT return. This is a much larger
    payload (all pages + both index sets) since it's meant for bulk/offline
    caching of a whole book, not a single lookup."""
    resp = _request_with_retry(
        f"https://files.turath.io/books/{book_id}.json",
        headers={"Accept": "application/json"},
        timeout=30,
    )
    return resp.json()


def search_turath(query: str, category: int | None = None,
                  book_id: int | None = None, limit: int = 100) -> dict:
    params = {"q": query, "ver": 3, "limit": limit}
    if category:
        params["cat_id"] = category
    if book_id:
        params["book_id"] = book_id
    resp = _request_with_retry(
        "https://api.turath.io/search",
        params=params,
        headers={"Accept": "application/json"},
    )
    return resp.json()


# Simple in-memory cache so repeated calls for the same book don't re-fetch
# the (fairly heavy) indexes payload every time.
_book_info_cache: dict[int, dict] = {}


def _get_book_info_cached(book_id: int) -> dict:
    if book_id not in _book_info_cache:
        _book_info_cache[book_id] = get_book_info(book_id)
    return _book_info_cache[book_id]


def resolve_printed_page(book_id: int, volume: str | int, printed_page: int) -> int:
    """Converts a (volume, printed page) pair — the numbers a human sees on
    the physical scan — into the internal sequential `pg` that /page needs.
    Raises ValueError if that volume/page combination doesn't exist."""
    info = _get_book_info_cached(book_id)
    key = f"{volume},{printed_page}"
    pg = info["indexes"]["print_pg_to_pg"].get(key)
    if pg is None:
        raise ValueError(
            f"No such page: book {book_id}, volume {volume}, printed page {printed_page}"
        )
    return pg


# ---- Rendering ----------------------------------------------------------

def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), _prepare_rtl(text), font=font, **RTL_KW)
    return bbox[2] - bbox[0]


def _wrap_arabic(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                  max_width: int) -> list[str]:
    """Word-wraps text (in normal logical/reading order) to fit max_width,
    measuring with the same rtl shaping Pillow will use to draw it."""
    paragraphs = text.split("\n")
    lines: list[str] = []
    for para in paragraphs:
        if not para.strip():
            lines.append("")
            continue
        words = para.split(" ")
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if _text_width(draw, trial, font) <= max_width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def render_page_image(
    book_title: str,
    page_number: int | str,
    text: str,
    output_path: str,
    author: str | None = None,
) -> str:
    body_font = ImageFont.truetype(FONT_PATH, BODY_FONT_SIZE)
    header_font = ImageFont.truetype(FONT_PATH_BOLD, HEADER_FONT_SIZE)
    footer_font = ImageFont.truetype(FONT_PATH, FOOTER_FONT_SIZE)

    # Need a draw context to measure text before we know final image height,
    # so build on a throwaway 1x1 image first.
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_text_width = PAGE_WIDTH - 2 * MARGIN
    lines = _wrap_arabic(probe, text, body_font, max_text_width)

    line_height = BODY_FONT_SIZE + LINE_SPACING
    header_block_height = 90
    footer_block_height = 60
    body_height = len(lines) * line_height
    page_height = header_block_height + body_height + footer_block_height + 2 * MARGIN

    img = Image.new("RGB", (PAGE_WIDTH, page_height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header: book title (right-aligned)
    header_w = _text_width(draw, book_title, header_font)
    draw.text((PAGE_WIDTH - MARGIN - header_w, MARGIN - 10), _prepare_rtl(book_title),
               font=header_font, fill=TEXT_COLOR, **RTL_KW)
    if author:
        author_w = _text_width(draw, author, footer_font)
        draw.text((PAGE_WIDTH - MARGIN - author_w, MARGIN + 24), _prepare_rtl(author),
                   font=footer_font, fill=TEXT_COLOR, **RTL_KW)

    rule_y = header_block_height + MARGIN - 20
    draw.line([(MARGIN, rule_y), (PAGE_WIDTH - MARGIN, rule_y)], fill=RULE_COLOR, width=2)

    # Body: right-aligned RTL lines
    y = rule_y + 30
    for line in lines:
        line_w = _text_width(draw, line, body_font)
        x = PAGE_WIDTH - MARGIN - line_w
        draw.text((x, y), _prepare_rtl(line), font=body_font, fill=TEXT_COLOR, **RTL_KW)
        y += line_height

    # Footer: page number, centered
    footer_text = str(page_number)
    footer_w = _text_width(draw, footer_text, footer_font)
    fx = (PAGE_WIDTH - footer_w) / 2
    draw.text((fx, page_height - MARGIN - 10), footer_text, font=footer_font, fill=TEXT_COLOR)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)
    return output_path


def clean_page_text(text: str) -> str:
    """Strips turath's embedded markup meant for their own web reader --
    narrator profile links (<a href="inr://...">name</a>), hadith boundary
    markers (<hadeeth-NNNN>, <hadeeth>), and similar tags -- leaving the
    underlying visible text intact. Also drops the footnote-separator
    block. Used both for the raw-text command and before rendering the
    text-fallback page image (the markup would otherwise get drawn
    literally into the image)."""
    text = re.sub(r"<[^>]+>", "", text)
    sep_index = text.find("_________")
    if sep_index != -1:
        text = text[:sep_index]
    return text.strip()


def fetch_and_render_page(
    book_id: int,
    output_path: str,
    page_number: int | None = None,
    volume: str | int | None = None,
    printed_page: int | None = None,
    book_title: str | None = None,
    author: str | None = None,
) -> str:
    """Fetches a page from turath.io and renders it to a page-styled image.

    Specify EITHER:
      - page_number: the raw internal /page index (rarely what you want —
        it does not match the number printed on the physical page), OR
      - volume + printed_page: the volume and printed page number as they
        actually appear on the scan (e.g. volume=1, printed_page=45) —
        this is resolved to the correct internal page_number automatically.
    """
    if page_number is None:
        if volume is None or printed_page is None:
            raise ValueError("Provide either page_number, or both volume and printed_page")
        page_number = resolve_printed_page(book_id, volume, printed_page)
        footer_label = str(printed_page)
    else:
        footer_label = str(page_number)

    if book_title is None:
        info = _get_book_info_cached(book_id)
        book_title = info.get("meta", {}).get("name", f"Book {book_id}")

    page = fetch_turath_page(book_id, page_number)
    return render_page_image(
        book_title=book_title,
        page_number=footer_label,
        text=clean_page_text(page["text"]),
        output_path=output_path,
        author=author,
    )


if __name__ == "__main__":
    from PIL import features
    assert features.check("raqm"), (
        "Pillow was built without libraqm — RTL shaping will be wrong. "
        "Run: pip install --upgrade --force-reinstall pillow"
    )
    sample_text = (
        "الإمام محيي الدين النووي\n"
        "(٦٣١ - ٦٧٦ هـ)\n"
        "مع زيادات الحافظ ابن رجر الحنبلي\n"
        "(٧٩٥ - ٧٣٦ هـ)\n"
        "تحقيق\n"
        "الشيخين: زفيان الشريبيني"
    )
    render_page_image(
        book_title="Book 147927",
        page_number=1,
        text=sample_text,
        output_path="/home/claude/test_output/v2_titlepage.png",
    )
    print("Rendered sample title page.")