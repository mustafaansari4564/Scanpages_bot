# Scanpages Bot

Discord bot for looking up and viewing pages from the [turath.io](https://turath.io) Islamic library. Fetches real scanned page images from PDFs when available, falls back to Arabic-aware text rendering when not, and supports full-text search across the entire catalog.

---

## Features

- **Book lookup** — search by title or author with autocomplete (Arabic + English transliteration)
- **Page scans** — fetch the actual scanned page image from the book's PDF
- **Text fallback** — when no scan exists, renders the text onto a styled Arabic page image using the Amiri font
- **Front page + scan** — return both the title page and requested page for citation
- **Page ranges** — fetch up to 10 consecutive pages with a paginated gallery (Previous/Next buttons)
- **Hadith number lookup** — jump directly to a hadith number (for numbered editions like Sahih al-Bukhari)
- **Full-text search** — search across all turath.io books with paginated results
- **In-book search** — search for text within a specific book
- **Raw text retrieval** — get the plain text of a page (no image)
- **PDF cache management** — view or clear the local PDF cache
- **Access control** — tiered role-based permissions (COD role = unrestricted, Library Pass role = channel-restricted)

---

## Commands

| Command | Description |
|---------|-------------|
| `/bookinfo` | Show book details: title, author, category, scan availability, volumes, page count |
| `/getscan` | Fetch a scanned page as an image (or text-render fallback) |
| `/getscanwfp` | Fetch a page along with the book's front/title page |
| `/getscanrange` | Fetch multiple consecutive pages (max 10) with navigation buttons |
| `/gethadith` | Look up a page by hadith number (for numbered editions) |
| `/gettext` | Get the raw text of a page (no image) |
| `/searchtext` | Full-text search across the entire turath.io library |
| `/searchinbook` | Search for text within a specific book |
| `/pagecache` | View or clear the PDF download cache |

All commands support **book autocomplete** — start typing a title or author name and pick from the suggestions. The internal book ID is never exposed to users.

---

## Architecture

```
bot.py              Discord bot, slash commands, access control, UI views
catalog.py          Local SQLite catalog built from turath.io's bulk data dump
page_renderer.py    Fetches pages via turath.io API, renders Arabic text to PNG,
                    downloads and extracts pages from real scanned PDFs
transliterate.py    Arabic → Latin transliteration (curated dictionary + fallback)
build_catalog.py    One-time script to download turath's library and build the catalog
setup_font.py       One-time script to download the Amiri Arabic font
amiri_font/         Amiri font files (downloaded by setup_font.py)
```

### Data Flow

```
User types /getscan book:"صحيح البخاري" page:45
  │
  ├─ autocomplete searches local SQLite catalog (FTS5)
  │
  ├─ resolve_printed_page() maps (volume, printed_page) → internal page number
  │
  ├─ render_scan_page() → tries real PDF first
  │   ├─ resolve_pdf_url() finds the scanned PDF URL
  │   ├─ _download_pdf() fetches/caches the PDF (content-hashed, 2GB cap)
  │   └─ PyMuPDF extracts the page as a PNG
  │
  ├─ if no PDF → fetch_and_render_page() text fallback
  │   ├─ fetch_turath_page() gets the text from api.turath.io
  │   └─ render_page_image() draws Arabic text using Amiri font (RTL-aware)
  │
  └─ Discord embed with the page image
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Download the Amiri font (one-time)

```bash
python setup_font.py
```

This downloads the Amiri Arabic font into `amiri_font/` for the text-render fallback.

### 3. Build the book catalog (one-time)

```bash
python build_catalog.py
```

Downloads turath.io's full library dump and builds `turath_catalog.db` — a local SQLite database with FTS5 search. Re-run with `--force` to pick up new books.

### 4. Configure environment

Create a `.env` file:

```env
DISCORD_TOKEN=your-bot-token
COD_ROLE_ID=
LIBRARY_PASS_ROLE_ID=
LIBRARY_CHANNEL_ID=
CATALOG_DB_PATH=turath_catalog_corrected.db
GUILD_ID=
```

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Bot token from the [Discord Developer Portal](https://discord.com/developers/applications) |
| `COD_ROLE_ID` | Role ID with unrestricted access (usable in any channel) |
| `LIBRARY_PASS_ROLE_ID` | Role ID restricted to the designated library channel |
| `LIBRARY_CHANNEL_ID` | Channel ID where Library Pass members can use commands |
| `CATALOG_DB_PATH` | Path to the SQLite catalog (default: `turath_catalog_corrected.db`) |
| `GUILD_ID` | Optional — syncs commands instantly to one guild instead of global (faster during development) |

### 5. Run

```bash
python bot.py
```

---

## Access Control

| Role | Permission |
|------|-----------|
| COD | Unrestricted — use commands in any channel, any server |
| Library Pass | Channel-restricted — only works in `LIBRARY_CHANNEL_ID` or threads |

Users without either role see "You don't have access to this command here."

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Bot framework | discord.py 2.3+ (slash commands) |
| Book catalog | SQLite + FTS5 full-text search |
| Page rendering | Pillow + Amiri font (libraqm for RTL) |
| PDF extraction | PyMuPDF (fitz) |
| PDF caching | Content-hashed, 2GB LRU cache in `/tmp` |
| Data source | turath.io API (`api.turath.io`, `files.turath.io`) |
| Transliteration | Curated Arabic → Latin dictionary + letter fallback |

---

## PDF Cache

Downloaded PDFs are cached in the system temp directory (`/tmp/turath_pdfs/` on Linux). Key behaviors:

- **Content-hashed** — identical PDFs (e.g. combined multi-volume books) are stored once
- **2 GB cap** — oldest files are evicted LRU when the cap is exceeded
- **Deduplicated** — volumes pointing to the same file share a single cache entry via hardlink

Use `/pagecache status` to check cache size and `/pagecache clear` to free disk space.

---

## License

MIT
