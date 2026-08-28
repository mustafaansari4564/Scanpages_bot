"""
Scan Page Bot -- Discord bot for turath.io book lookup and page scans.

Commands:
    /bookinfo book:<autocomplete>          -- shows title/author/category/
                                               scan-availability. Never
                                               shows the internal book_id.
    /getscan  book:<autocomplete> page:<n> [volume:<n>]
                                            -- posts the requested page as
                                               an image: the real scanned
                                               page when available, a
                                               text-render fallback
                                               otherwise.

Access control (matches the Lughat Bot pattern):
    - COD role: usable in any channel, server-wide.
    - Library Pass role: usable only in the configured LIBRARY_CHANNEL_ID.

Setup:
    1. Build the catalog first (one-time, see build_catalog.py):
           python build_catalog.py
    2. Fill in .env (see .env.example below this docstring).
    3. python bot.py

.env.example:
    DISCORD_TOKEN=your-bot-token
    COD_ROLE_ID=123456789012345678
    LIBRARY_PASS_ROLE_ID=123456789012345678
    LIBRARY_CHANNEL_ID=123456789012345678
    CATALOG_DB_PATH=turath_catalog.db
    GUILD_ID=123456789012345678   # optional: instant sync to one guild
                                   # while developing; omit for global sync
"""

import asyncio
import json
import logging
import os
import re
import sys
import tempfile

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scanpages")

from catalog import search_books, get_book_row, display_name, display_author
from page_renderer import (
    render_scan_page,
    render_scan_page_with_front,
    fetch_and_render_page,
    get_book_info,
    fetch_turath_page,
    resolve_printed_page,
    clean_page_text,
    search_turath,
    _pdf_cache_dir,
)
from transliterate import transliterate

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CATALOG_DB = os.getenv("CATALOG_DB_PATH", "turath_catalog_corrected.db")
COD_ROLE_ID = int(os.getenv("COD_ROLE_ID", "0"))
LIBRARY_PASS_ROLE_ID = int(os.getenv("LIBRARY_PASS_ROLE_ID", "0"))
LIBRARY_CHANNEL_ID = int(os.getenv("LIBRARY_CHANNEL_ID", "0"))
GUILD_ID = os.getenv("GUILD_ID")

FOOTER_TEXT = "Powered by 𝐓𝐡𝐞𝐨𝐥𝐨𝐠𝐢𝐜𝐚𝐥 𝐃𝐢𝐬𝐜𝐨𝐮𝐫𝐬𝐞🎙"


def _set_footer(embed: discord.Embed, extra: str | None = None) -> None:
    """Applies the standard bot footer, matching the other THxD bots.
    If `extra` is given (e.g. file size), it's shown alongside it."""
    embed.set_footer(text=f"{extra} • {FOOTER_TEXT}" if extra else FOOTER_TEXT)


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ---- Access control -------------------------------------------------

def _has_access(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    role_ids = {r.id for r in interaction.user.roles}
    if COD_ROLE_ID in role_ids:
        return True
    if LIBRARY_PASS_ROLE_ID in role_ids:
        # Allow in the designated library channel
        if interaction.channel_id == LIBRARY_CHANNEL_ID:
            return True
        # Allow in threads (research threads, etc.) — threads are private
        # spaces where the user was explicitly granted access
        if isinstance(interaction.channel, discord.Thread):
            return True
    return False


async def access_check(interaction: discord.Interaction) -> bool:
    if _has_access(interaction):
        return True
    await interaction.response.send_message(
        "You don't have access to this command here.", ephemeral=True
    )
    return False


# ---- Autocomplete (book_id NEVER shown -- it's only the hidden value) --

def _format_choice_label(row: dict) -> str:
    ar = row["name"]
    en = display_name(row) or transliterate(ar)
    author = row.get("author_name") or ""
    author_en = display_author(row) or (transliterate(author) if author else "")
    label = f"{en} — {ar}"
    if author:
        label += f" ({author_en})"
    return label[:100]  # Discord's hard limit on choice label length


async def book_autocomplete(interaction: discord.Interaction, current: str):
    try:
        if len(current.strip()) < 2:
            return []
        results = search_books(CATALOG_DB, current, limit=25)
        return [
            app_commands.Choice(name=_format_choice_label(row), value=str(row["id"]))
            for row in results
        ]
    except Exception as e:
        # Autocomplete exceptions don't go through our normal error handler
        # and Discord just shows "Loading options failed" with no detail --
        # log it here so the real cause is visible.
        logger.warning("autocomplete error: command=%s query=%r: %r",
                       interaction.command.name if interaction.command else "?",
                       current, e)
        return []


def _resolve_book(book: str) -> tuple[int | None, str | None]:
    """Resolves the book option to a book_id. Handles the normal case
    (user picked an autocomplete suggestion, so `book` is already the
    id) and falls back to a live search if they typed free text and hit
    enter without selecting a suggestion. Returns (book_id, error_message)."""
    try:
        return int(book), None
    except ValueError:
        pass
    matches = search_books(CATALOG_DB, book, limit=2)
    if len(matches) == 1:
        return matches[0]["id"], None
    if len(matches) > 1:
        return None, (
            "That matched more than one book. Please type a bit more of the "
            "title and pick one of the suggestions that pop up as you type."
        )
    suggestions = search_books(CATALOG_DB, book, limit=3)
    if suggestions:
        lines = []
        for s in suggestions:
            en = display_name(s) or transliterate(s["name"])
            lines.append(f"• **{en}** — {s['name']}")
        suggestion_text = "\n".join(lines)
        return None, (
            f"No exact match found. Did you mean one of these?\n{suggestion_text}\n\n"
            f"Start typing to see autocomplete suggestions."
        )
    return None, (
        "No book found. Start typing a title or author, then pick one of "
        "the suggestions that appear -- don't just press enter on typed text."
    )


# ---- /bookinfo --------------------------------------------------------

class VolumeSelect(discord.ui.Select):
    """Dropdown to pick a volume from a multi-volume book."""

    def __init__(self, book_id: int, book_name: str, volumes: list):
        options = [
            discord.SelectOption(
                label=f"Volume {v}",
                value=str(v),
                description=f"Open /getscan for vol. {v}",
            )
            for v in volumes[:25]
        ]
        super().__init__(
            placeholder="Select a volume to get a scan…",
            options=options,
        )
        self.book_id = book_id
        self.book_name = book_name

    async def callback(self, interaction: discord.Interaction):
        volume = self.values[0]
        embed = discord.Embed(
            title=f"{self.book_name} — Volume {volume}",
            description=(
                f"Use `/getscan` with this book and **volume {volume}** "
                f"to fetch a specific page.\n\n"
                f"Example: `/getscan book:{self.book_name[:50]} page:1 volume:{volume}`"
            ),
            color=0x2F7D5C,
        )
        _set_footer(embed)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BookInfoView(discord.ui.View):
    """View with volume selector for multi-volume books."""

    def __init__(self, book_id: int, book_name: str, volumes: list):
        super().__init__(timeout=120)
        self.add_item(VolumeSelect(book_id, book_name, volumes))


@bot.tree.command(name="bookinfo", description="Get information about a book")
@app_commands.describe(book="Search by title or author")
@app_commands.autocomplete(book=book_autocomplete)
@app_commands.check(access_check)
async def bookinfo(interaction: discord.Interaction, book: str):
    book_id, error = _resolve_book(book)
    if book_id is None:
        await interaction.response.send_message(error, ephemeral=True)
        return

    row = get_book_row(CATALOG_DB, book_id)
    if row is None:
        await interaction.response.send_message("Book not found.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        book_info = await asyncio.to_thread(get_book_info, book_id)
        indexes = book_info["indexes"]
        volumes = indexes.get("volumes") or []
        total_pages = len(indexes.get("page_map") or [])
    except Exception as e:
        logger.warning("bookinfo indexes error for book_id=%d: %r", book_id, e)
        volumes, total_pages = [], 0

    ar_name = row["name"]
    en_name = display_name(row) or transliterate(ar_name)
    author_ar = row.get("author_name") or ""
    author_en = display_author(row) or (transliterate(author_ar) if author_ar else "Unknown")
    death = row.get("author_death")
    death_str = f"d. {death} AH" if death else "era unknown"
    cat = row.get("cat_name") or "Uncategorized"
    availability = (
        "✅ Scanned pages available"
        if row.get("has_pdf")
        else "📝 Text only (no scanned pages for this book)"
    )
    size_mb = (row.get("size") or 0) / 1_000_000

    if volumes:
        vol_count = len(volumes)
        vol_str = "1 volume" if vol_count == 1 else f"{vol_count} volumes"
        if 1 < vol_count <= 10:
            vol_str += f" ({', '.join(str(v) for v in volumes)})"
    else:
        vol_str = "unknown"

    embed = discord.Embed(title=en_name, description=ar_name, color=0x2F7D5C)
    if author_ar:
        embed.add_field(
            name="Author", value=f"{author_en} — {author_ar}\n({death_str})", inline=False
        )
    embed.add_field(name="Category", value=cat, inline=True)
    embed.add_field(name="Availability", value=availability, inline=True)
    embed.add_field(name="Volumes", value=vol_str, inline=True)
    if total_pages:
        embed.add_field(name="Pages", value=str(total_pages), inline=True)
    if size_mb:
        _set_footer(embed, f"~{size_mb:.1f} MB")
    else:
        _set_footer(embed)

    if volumes and len(volumes) > 1:
        view = BookInfoView(book_id, en_name or ar_name, volumes)
        await interaction.followup.send(embed=embed, view=view)
    else:
        await interaction.followup.send(embed=embed)


# ---- shared volume resolution (used by /getscan, /gettext, /gethadith) --

async def _resolve_volume(book_id: int, volume: str | None) -> tuple[str | None, str | None]:
    """Returns (volume, error_message). If volume is None, tries to
    auto-detect it (single-volume books) or returns an error listing the
    available volumes (multi-volume books)."""
    if volume is not None:
        return volume, None
    try:
        book_info = await asyncio.to_thread(get_book_info, book_id)
        volumes = book_info["indexes"]["volumes"]
    except Exception:
        volumes = None
    if volumes and len(volumes) == 1:
        return volumes[0], None
    if volumes and len(volumes) > 1:
        listing = ", ".join(str(v) for v in volumes)
        return None, (
            f"This book has multiple volumes: {listing}. "
            f"Please specify which one with the `volume` option."
        )
    return "1", None


# ---- /pagecache -----------------------------------------------------------

@bot.tree.command(name="pagecache", description="View or clear the PDF cache")
@app_commands.describe(action="What to do with the cache")
@app_commands.choices(action=[
    app_commands.Choice(name="📊 Status", value="status"),
    app_commands.Choice(name="🗑️ Clear", value="clear"),
])
@app_commands.check(access_check)
async def pagecache(interaction: discord.Interaction, action: str):
    await interaction.response.defer(ephemeral=True)

    if action == "status":
        try:
            files = [p for p in _pdf_cache_dir.iterdir() if p.is_file()]
            total_size = sum(p.stat().st_size for p in files)
            total_mb = total_size / (1024 * 1024)
            embed = discord.Embed(
                title="📊 PDF Cache Status",
                description=(
                    f"**Files:** {len(files)}\n"
                    f"**Size:** {total_mb:.1f} MB\n"
                    f"**Max:** 2048.0 MB"
                ),
                color=0x2F7D5C,
            )
            if files:
                oldest = min(files, key=lambda p: p.stat().st_mtime)
                newest = max(files, key=lambda p: p.stat().st_mtime)
                embed.add_field(
                    name="Oldest",
                    value=f"`{oldest.name}`",
                    inline=True,
                )
                embed.add_field(
                    name="Newest",
                    value=f"`{newest.name}`",
                    inline=True,
                )
            _set_footer(embed)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error("pagecache status error: %r", e)
            await interaction.followup.send(
                f"Error reading cache: `{e}`", ephemeral=True
            )

    elif action == "clear":
        try:
            files = [p for p in _pdf_cache_dir.iterdir() if p.is_file()]
            total_size = sum(p.stat().st_size for p in files)
            total_mb = total_size / (1024 * 1024)
            count = len(files)
            for p in files:
                try:
                    p.unlink()
                except OSError:
                    pass
            embed = discord.Embed(
                title="🗑️ Cache Cleared",
                description=(
                    f"Removed **{count}** files ({total_mb:.1f} MB freed)"
                ),
                color=0x2F7D5C,
            )
            _set_footer(embed)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error("pagecache clear error: %r", e)
            await interaction.followup.send(
                f"Error clearing cache: `{e}`", ephemeral=True
            )


# ---- /getscan -----------------------------------------------------------

@bot.tree.command(name="getscan", description="Get a scanned page from a book")
@app_commands.describe(
    book="Search by title or author",
    page="Printed page number (as shown on the physical page)",
    volume="Volume -- only needed for multi-volume books (may be a number or a label like المقدمة)",
)
@app_commands.autocomplete(book=book_autocomplete)
@app_commands.check(access_check)
async def getscan(
    interaction: discord.Interaction, book: str, page: int, volume: str | None = None
):
    book_id, error = _resolve_book(book)
    if book_id is None:
        await interaction.response.send_message(error, ephemeral=True)
        return

    row = get_book_row(CATALOG_DB, book_id)
    if row is None:
        await interaction.response.send_message("Book not found.", ephemeral=True)
        return

    await interaction.response.defer()

    volume, vol_error = await _resolve_volume(book_id, volume)
    if volume is None:
        await interaction.followup.send(vol_error, ephemeral=True)
        return

    output_path = os.path.join(
        tempfile.gettempdir(), f"scan_{book_id}_{volume}_{page}.png"
    )
    ar_name = row["name"]

    try:
        result_path = await asyncio.to_thread(render_scan_page, book_id, volume, page, output_path)
        used_real_scan = result_path is not None
        if result_path is None:
            result_path = await asyncio.to_thread(
                fetch_and_render_page,
                book_id=book_id, volume=volume, printed_page=page, output_path=output_path
            )
    except ValueError as e:
        await interaction.followup.send(f"Couldn't get that page: {e}", ephemeral=True)
        return
    except Exception as e:
        logger.error("getscan error for book_id=%d volume=%r page=%d: %r",
                     book_id, volume, page, e)
        await interaction.followup.send(
            f"Something went wrong fetching that page: `{e}`\n"
            f"Double check the volume and page numbers.",
            ephemeral=True,
        )
        return

    caption = "Scanned page" if used_real_scan else "Text render (no scan available for this book)"
    filename = os.path.basename(result_path)
    embed = discord.Embed(
        title=f"{ar_name} — vol. {volume}, p. {page}",
        description=caption,
        color=0x2F7D5C,
    )
    embed.set_image(url=f"attachment://{filename}")
    _set_footer(embed)

    await interaction.followup.send(embed=embed, file=discord.File(result_path, filename=filename))
    try:
        os.remove(result_path)
    except OSError:
        pass


# ---- /getscanwfp (scan with front page) ----------------------------------

@bot.tree.command(
    name="getscanwfp",
    description="Get a scanned page along with the book's front/title page",
)
@app_commands.describe(
    book="Search by title or author",
    page="Printed page number (as shown on the physical page)",
    volume="Volume -- only needed for multi-volume books (may be a number or a label like المقدمة)",
)
@app_commands.autocomplete(book=book_autocomplete)
@app_commands.check(access_check)
async def getscanwfp(
    interaction: discord.Interaction, book: str, page: int, volume: str | None = None
):
    book_id, error = _resolve_book(book)
    if book_id is None:
        await interaction.response.send_message(error, ephemeral=True)
        return

    row = get_book_row(CATALOG_DB, book_id)
    if row is None:
        await interaction.response.send_message("Book not found.", ephemeral=True)
        return

    await interaction.response.defer()

    volume, vol_error = await _resolve_volume(book_id, volume)
    if volume is None:
        await interaction.followup.send(vol_error, ephemeral=True)
        return

    content_path = os.path.join(
        tempfile.gettempdir(), f"scan_{book_id}_{volume}_{page}.png"
    )
    front_path = os.path.join(
        tempfile.gettempdir(), f"front_{book_id}_{volume}.png"
    )
    ar_name = row["name"]

    try:
        result = await asyncio.to_thread(
            render_scan_page_with_front, book_id, volume, page, content_path, front_path
        )
    except ValueError as e:
        await interaction.followup.send(f"Couldn't get that page: {e}", ephemeral=True)
        return
    except Exception as e:
        logger.error("getscanwfp error for book_id=%d volume=%r page=%d: %r",
                     book_id, volume, page, e)
        await interaction.followup.send(
            f"Something went wrong fetching that page: `{e}`\n"
            f"Double check the volume and page numbers.",
            ephemeral=True,
        )
        return

    if result is None:
        # no PDF for this book -- fall back to a plain text render of just
        # the requested page (no front-page concept for text-only books)
        try:
            result_path = await asyncio.to_thread(
                fetch_and_render_page,
                book_id=book_id, volume=volume, printed_page=page, output_path=content_path,
            )
        except Exception as e:
            logger.error("getscanwfp fallback error for book_id=%d: %r", book_id, e)
            await interaction.followup.send(
                f"Something went wrong fetching that page: `{e}`", ephemeral=True
            )
            return
        filename = os.path.basename(result_path)
        embed = discord.Embed(
            title=f"{ar_name} — vol. {volume}, p. {page}",
            description="Text render (no scan available for this book)",
            color=0x2F7D5C,
        )
        embed.set_image(url=f"attachment://{filename}")
        _set_footer(embed)
        await interaction.followup.send(embed=embed, file=discord.File(result_path, filename=filename))
        try:
            os.remove(result_path)
        except OSError:
            pass
        return

    front_result_path, content_result_path = result
    embed = discord.Embed(
        title=f"{ar_name} — vol. {volume}, p. {page}",
        description="Front page + requested page",
        color=0x2F7D5C,
    )
    _set_footer(embed)
    front_filename = os.path.basename(front_result_path)
    content_filename = os.path.basename(content_result_path)

    await interaction.followup.send(
        embed=embed,
        files=[
            discord.File(front_result_path, filename=front_filename),
            discord.File(content_result_path, filename=content_filename),
        ],
    )
    for p in (front_result_path, content_result_path):
        try:
            os.remove(p)
        except OSError:
            pass


# ---- /getscan range (multiple pages) ------------------------------------

class PageRangeView(discord.ui.View):
    """Gallery view for /getscan range with page navigation buttons."""

    def __init__(self, book_id: int, book_name: str, volume: str,
                 pages: list[int], start_idx: int = 0):
        super().__init__(timeout=120)
        self.book_id = book_id
        self.book_name = book_name
        self.volume = volume
        self.pages = pages
        self.start_idx = start_idx

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        new_idx = max(0, self.start_idx - 1)
        if new_idx == self.start_idx:
            await interaction.response.defer()
            return
        await self._show_page(interaction, new_idx)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        new_idx = min(len(self.pages) - 1, self.start_idx + 1)
        if new_idx == self.start_idx:
            await interaction.response.defer()
            return
        await self._show_page(interaction, new_idx)

    async def _show_page(self, interaction: discord.Interaction, idx: int):
        self.start_idx = idx
        page_num = self.pages[idx]
        output_path = os.path.join(
            tempfile.gettempdir(), f"range_{self.book_id}_{self.volume}_{page_num}.png"
        )
        await interaction.response.defer()
        try:
            result_path = await asyncio.to_thread(
                render_scan_page, self.book_id, self.volume, page_num, output_path
            )
            used_real_scan = result_path is not None
            if result_path is None:
                result_path = await asyncio.to_thread(
                    fetch_and_render_page,
                    book_id=self.book_id, volume=self.volume,
                    printed_page=page_num, output_path=output_path,
                )
        except Exception as e:
            logger.error("getscan range error: %r", e)
            await interaction.followup.send(
                f"Error fetching page {page_num}: `{e}`", ephemeral=True
            )
            return

        caption = "Scanned page" if used_real_scan else "Text render"
        filename = os.path.basename(result_path)
        embed = discord.Embed(
            title=f"{self.book_name} — vol. {self.volume}, p. {page_num}",
            description=f"{caption}\n📄 Page {idx + 1} of {len(self.pages)}",
            color=0x2F7D5C,
        )
        embed.set_image(url=f"attachment://{filename}")
        _set_footer(embed)

        self.children[0].disabled = (idx == 0)
        self.children[1].disabled = (idx == len(self.pages) - 1)

        await interaction.followup.send(
            embed=embed,
            file=discord.File(result_path, filename=filename),
            view=self,
        )
        try:
            os.remove(result_path)
        except OSError:
            pass


@bot.tree.command(
    name="getscanrange",
    description="Get multiple consecutive scanned pages from a book",
)
@app_commands.describe(
    book="Search by title or author",
    start_page="First printed page number",
    end_page="Last printed page number (max 10 pages)",
    volume="Volume -- only needed for multi-volume books",
)
@app_commands.autocomplete(book=book_autocomplete)
@app_commands.check(access_check)
async def getscanrange(
    interaction: discord.Interaction,
    book: str,
    start_page: int,
    end_page: int,
    volume: str | None = None,
):
    book_id, error = _resolve_book(book)
    if book_id is None:
        await interaction.response.send_message(error, ephemeral=True)
        return

    row = get_book_row(CATALOG_DB, book_id)
    if row is None:
        await interaction.response.send_message("Book not found.", ephemeral=True)
        return

    if end_page < start_page:
        await interaction.response.send_message(
            "End page must be greater than or equal to start page.", ephemeral=True
        )
        return

    page_count = end_page - start_page + 1
    if page_count > 10:
        await interaction.response.send_message(
            "Maximum 10 pages per request. Please use a smaller range.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    volume, vol_error = await _resolve_volume(book_id, volume)
    if volume is None:
        await interaction.followup.send(vol_error, ephemeral=True)
        return

    pages = list(range(start_page, end_page + 1))
    first_page = pages[0]
    output_path = os.path.join(
        tempfile.gettempdir(), f"range_{book_id}_{volume}_{first_page}.png"
    )
    ar_name = row["name"]

    try:
        result_path = await asyncio.to_thread(
            render_scan_page, book_id, volume, first_page, output_path
        )
        used_real_scan = result_path is not None
        if result_path is None:
            result_path = await asyncio.to_thread(
                fetch_and_render_page,
                book_id=book_id, volume=volume,
                printed_page=first_page, output_path=output_path,
            )
    except Exception as e:
        logger.error("getscanrange error for book_id=%d: %r", book_id, e)
        await interaction.followup.send(
            f"Error fetching pages: `{e}`", ephemeral=True
        )
        return

    caption = "Scanned page" if used_real_scan else "Text render"
    filename = os.path.basename(result_path)
    embed = discord.Embed(
        title=f"{ar_name} — vol. {volume}, p. {first_page}",
        description=f"{caption}\n📄 Page 1 of {page_count}",
        color=0x2F7D5C,
    )
    embed.set_image(url=f"attachment://{filename}")
    _set_footer(embed)

    view = PageRangeView(book_id, ar_name, volume, pages, start_idx=0)
    view.children[0].disabled = True
    if page_count == 1:
        view.children[1].disabled = True

    await interaction.followup.send(
        embed=embed,
        file=discord.File(result_path, filename=filename),
        view=view,
    )
    try:
        os.remove(result_path)
    except OSError:
        pass


# ---- /gettext ----------------------------------------------------------

_EMBED_DESC_LIMIT = 4096


@bot.tree.command(name="gettext", description="Get the raw text of a book page (no image)")
@app_commands.describe(
    book="Search by title or author",
    page="Printed page number (as shown on the physical page)",
    volume="Volume -- only needed for multi-volume books (may be a number or a label like المقدمة)",
)
@app_commands.autocomplete(book=book_autocomplete)
@app_commands.check(access_check)
async def gettext(
    interaction: discord.Interaction, book: str, page: int, volume: str | None = None
):
    book_id, error = _resolve_book(book)
    if book_id is None:
        await interaction.response.send_message(error, ephemeral=True)
        return

    row = get_book_row(CATALOG_DB, book_id)
    if row is None:
        await interaction.response.send_message("Book not found.", ephemeral=True)
        return

    await interaction.response.defer()

    volume, vol_error = await _resolve_volume(book_id, volume)
    if volume is None:
        await interaction.followup.send(vol_error, ephemeral=True)
        return

    ar_name = row["name"]

    try:
        internal_pg = await asyncio.to_thread(resolve_printed_page, book_id, volume, page)
        page_data = await asyncio.to_thread(fetch_turath_page, book_id, internal_pg)
    except ValueError as e:
        await interaction.followup.send(f"Couldn't get that page: {e}", ephemeral=True)
        return
    except Exception as e:
        logger.error("gettext error for book_id=%d volume=%r page=%d: %r",
                     book_id, volume, page, e)
        await interaction.followup.send(
            f"Something went wrong fetching that page: `{e}`\n"
            f"Double check the volume and page numbers.",
            ephemeral=True,
        )
        return

    text = clean_page_text(page_data.get("text", ""))
    truncated = len(text) > _EMBED_DESC_LIMIT
    if truncated:
        text = text[:_EMBED_DESC_LIMIT - 20].rstrip() + "\n\n… (truncated)"
    if not text:
        text = "*(No text found for this page.)*"

    embed = discord.Embed(
        title=f"{ar_name} — vol. {volume}, p. {page}",
        description=text,
        color=0x2F7D5C,
    )
    _set_footer(embed)
    await interaction.followup.send(embed=embed)


# ---- /searchtext ----------------------------------------------------------

# Search highlights use <em> around matched terms; convert to **bold**,
# and strip any other turath markup (titles, span tags) so Discord doesn't
# show raw HTML.
_SEARCH_TAG_RE = re.compile(r"<(?!em\b|/em\b)[^>]*>", re.IGNORECASE)
_SEARCH_EM_RE = re.compile(r"</?em>", re.IGNORECASE)
_SEARCH_SNIP_LIMIT = 1000
_SEARCH_PAGE_SIZE = 10


def _format_search_hit(hit: dict, catalog_db: str) -> dict:
    """Builds a display dict from one /search hit: book title (corrected
    name from the local catalog when we have it, else turath's own name),
    author, volume, internal page, the highlighted snippet, and a 'book_id'
    that is also the /searchtext value (hidden book_id, never shown)."""
    try:
        meta = json.loads(hit.get("meta") or "{}")
    except (ValueError, TypeError):
        meta = {}

    book_id = hit.get("book_id") or meta.get("book_id") or 0
    row = get_book_row(catalog_db, book_id) if catalog_db and book_id else None

    book_name = (
        display_name(row)
        if row
        else meta.get("book_name") or meta.get("name") or f"Book {book_id}"
    )
    author = (
        display_author(row)
        if row
        else meta.get("author_name") or meta.get("author") or ""
    )
    if not author and row:
        author = display_author(row) or ""

    vol = meta.get("vol") or meta.get("volume") or ""
    page = meta.get("page") or ""
    headings = meta.get("headings") or []
    section = " › ".join(str(h) for h in headings if str(h) not in ("[*]", ""))

    snip_raw = (hit.get("snip") or "").strip()
    snip = _SEARCH_TAG_RE.sub("", snip_raw)
    snip = _SEARCH_EM_RE.sub("**", snip)
    snip = " ".join(snip.split())
    if len(snip) > _SEARCH_SNIP_LIMIT:
        snip = snip[:_SEARCH_SNIP_LIMIT].rstrip() + "…"

    loc = f"vol. {vol}" if vol else ""
    if page:
        loc += f"{', ' if loc else ''}p. {page}"
    loc = loc or "—"

    return {
        "book_id": book_id,
        "title": book_name,
        "author": author,
        "loc": loc,
        "snip": snip,
        "section": section,
    }


def _build_search_page(hits: list[dict], query: str, page_num: int,
                       total_pages: int) -> discord.Embed:
    """Build a single search results page embed."""
    lines = []
    start_idx = page_num * _SEARCH_PAGE_SIZE
    for i, hit in enumerate(hits, start=start_idx + 1):
        h = _format_search_hit(hit, CATALOG_DB)
        title = h["title"] or "Unknown"
        loc = h["loc"]
        snip = h["snip"]
        section_part = f"\n> 📁 {h['section']}" if h["section"] else ""
        lines.append(
            f"**{i}. {title}** ({h['author']}) — {loc}"
            f"{section_part}\n"
            f"> {snip}"
        )

    embed = discord.Embed(
        title=f"Search results for \u201c{query}\u201d",
        description="\n\n".join(lines),
        color=0x2F7D5C,
    )
    embed.set_footer(
        text=f"Page {page_num + 1}/{total_pages} • {len(hits)} results"
    )
    return embed


class SearchPaginationView(discord.ui.View):
    """Pagination view for /searchtext results with Previous/Next buttons."""

    def __init__(self, all_hits: list[dict], query: str):
        super().__init__(timeout=180)
        self.all_hits = all_hits
        self.query = query
        self.page = 0
        self.total_pages = max(1, -(-len(all_hits) // _SEARCH_PAGE_SIZE))
        self._update_buttons()

    def _update_buttons(self):
        self.children[0].disabled = (self.page == 0)
        self.children[1].disabled = (self.page >= self.total_pages - 1)

    def current_page_hits(self) -> list[dict]:
        start = self.page * _SEARCH_PAGE_SIZE
        return self.all_hits[start:start + _SEARCH_PAGE_SIZE]

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self._update_buttons()
            embed = _build_search_page(
                self.current_page_hits(), self.query,
                self.page, self.total_pages,
            )
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
            self._update_buttons()
            embed = _build_search_page(
                self.current_page_hits(), self.query,
                self.page, self.total_pages,
            )
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()


@bot.tree.command(name="searchtext", description="Search the full text of turath.io books")
@app_commands.describe(query="What to search for, e.g. a topic, phrase, or hadith wording")
@app_commands.check(access_check)
async def searchtext(interaction: discord.Interaction, query: str):
    if not query.strip():
        await interaction.response.send_message(
            "Please type something to search for.", ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        raw = await asyncio.to_thread(search_turath, query, limit=100)
    except Exception as e:
        logger.error("searchtext error for query=%r: %r", query, e)
        await interaction.followup.send(
            f"Search failed: `{e}`. Try again in a moment.", ephemeral=True
        )
        return

    hits = (raw or {}).get("data") or []
    if not hits:
        await interaction.followup.send(
            f"No results found for **{query}**. Try different wording.",
            ephemeral=True,
        )
        return

    total_hits = len(hits)
    total_pages = max(1, -(-total_hits // _SEARCH_PAGE_SIZE))

    first_page_hits = hits[:_SEARCH_PAGE_SIZE]
    embed = _build_search_page(first_page_hits, query, 0, total_pages)
    embed.set_footer(
        text=f"Page 1/{total_pages} • {total_hits} total results"
    )

    if total_pages > 1:
        view = SearchPaginationView(hits, query)
        await interaction.followup.send(embed=embed, view=view)
    else:
        await interaction.followup.send(embed=embed)


# ---- /searchinbook -------------------------------------------------------

@bot.tree.command(
    name="searchinbook",
    description="Search for text within a specific book",
)
@app_commands.describe(
    book="Search by title or author",
    query="Text to search for inside this book",
)
@app_commands.autocomplete(book=book_autocomplete)
@app_commands.check(access_check)
async def searchinbook(
    interaction: discord.Interaction, book: str, query: str
):
    book_id, error = _resolve_book(book)
    if book_id is None:
        await interaction.response.send_message(error, ephemeral=True)
        return

    row = get_book_row(CATALOG_DB, book_id)
    if row is None:
        await interaction.response.send_message("Book not found.", ephemeral=True)
        return

    if not query.strip():
        await interaction.response.send_message(
            "Please type something to search for.", ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        raw = await asyncio.to_thread(search_turath, query, None, book_id, limit=100)
    except Exception as e:
        logger.error("searchinbook error for book_id=%d query=%r: %r",
                     book_id, query, e)
        await interaction.followup.send(
            f"Search failed: `{e}`. Try again in a moment.", ephemeral=True
        )
        return

    hits = (raw or {}).get("data") or []
    if not hits:
        ar_name = row["name"]
        en_name = display_name(row) or transliterate(ar_name)
        await interaction.followup.send(
            f"No results found for **{query}** in *{en_name}*.",
            ephemeral=True,
        )
        return

    total_hits = len(hits)
    total_pages = max(1, -(-total_hits // _SEARCH_PAGE_SIZE))

    first_page_hits = hits[:_SEARCH_PAGE_SIZE]
    ar_name = row["name"]
    en_name = display_name(row) or transliterate(ar_name)

    lines = []
    for i, hit in enumerate(first_page_hits, 1):
        h = _format_search_hit(hit, CATALOG_DB)
        loc = h["loc"]
        snip = h["snip"]
        section_part = f"\n> 📁 {h['section']}" if h["section"] else ""
        lines.append(
            f"**{i}.** — {loc}"
            f"{section_part}\n"
            f"> {snip}"
        )

    embed = discord.Embed(
        title=f"Results for \u201c{query}\u201d in {en_name}",
        description="\n\n".join(lines),
        color=0x2F7D5C,
    )
    embed.set_footer(
        text=f"Page 1/{total_pages} • {total_hits} results in this book"
    )

    if total_pages > 1:
        view = SearchPaginationView(hits, query)
        await interaction.followup.send(embed=embed, view=view)
    else:
        await interaction.followup.send(embed=embed)


# ---- /gethadith -----------------------------------------------------------

@bot.tree.command(name="gethadith", description="Get a page by hadith number (for hadith-numbered books)")
@app_commands.describe(
    book="Search by title or author",
    hadith_number="The hadith number as numbered in this book's edition",
)
@app_commands.autocomplete(book=book_autocomplete)
@app_commands.check(access_check)
async def gethadith(interaction: discord.Interaction, book: str, hadith_number: int):
    book_id, error = _resolve_book(book)
    if book_id is None:
        await interaction.response.send_message(error, ephemeral=True)
        return

    row = get_book_row(CATALOG_DB, book_id)
    if row is None:
        await interaction.response.send_message("Book not found.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        indexes = (await asyncio.to_thread(get_book_info, book_id))["indexes"]
    except Exception as e:
        await interaction.followup.send(
            f"Couldn't load this book's index: `{e}`", ephemeral=True
        )
        return

    numbers = indexes.get("numbers")
    if not numbers:
        await interaction.followup.send(
            "This book doesn't have hadith-number lookup available -- try `/getscan` "
            "or `/gettext` with a page number instead.",
            ephemeral=True,
        )
        return

    pg = numbers.get(str(hadith_number))
    if pg is None:
        max_n = indexes.get("number_max")
        hint = f" (this edition seems to go up to number {max_n})" if max_n else ""
        await interaction.followup.send(
            f"Hadith number {hadith_number} wasn't found in this book{hint}.",
            ephemeral=True,
        )
        return

    page_map = indexes.get("page_map") or []
    if not (1 <= pg <= len(page_map)):
        await interaction.followup.send(
            "Couldn't resolve that hadith number to a page -- this book's index "
            "may be incomplete.",
            ephemeral=True,
        )
        return

    volume, printed_page_str = (page_map[pg - 1] or ",").split(",")
    try:
        printed_page = int(printed_page_str)
    except ValueError:
        await interaction.followup.send(
            "Couldn't resolve that hadith number to a page number.", ephemeral=True
        )
        return

    output_path = os.path.join(
        tempfile.gettempdir(), f"hadith_{book_id}_{hadith_number}.png"
    )
    ar_name = row["name"]

    try:
        result_path = await asyncio.to_thread(
            render_scan_page, book_id, volume, printed_page, output_path
        )
        used_real_scan = result_path is not None
        if result_path is None:
            result_path = await asyncio.to_thread(
                fetch_and_render_page,
                book_id=book_id, volume=volume, printed_page=printed_page,
                output_path=output_path,
            )
    except ValueError as e:
        await interaction.followup.send(f"Couldn't get that page: {e}", ephemeral=True)
        return
    except Exception as e:
        logger.error("gethadith error for book_id=%d hadith=%d: %r",
                     book_id, hadith_number, e)
        await interaction.followup.send(
            f"Something went wrong fetching that page: `{e}`", ephemeral=True
        )
        return

    caption = "Scanned page" if used_real_scan else "Text render (no scan available for this book)"
    filename = os.path.basename(result_path)
    embed = discord.Embed(
        title=f"{ar_name} — hadith #{hadith_number} (vol. {volume}, p. {printed_page})",
        description=caption,
        color=0x2F7D5C,
    )
    embed.set_image(url=f"attachment://{filename}")
    _set_footer(embed)

    await interaction.followup.send(embed=embed, file=discord.File(result_path, filename=filename))
    try:
        os.remove(result_path)
    except OSError:
        pass


# ---- error handling -----------------------------------------------------

async def _on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return  # already messaged by access_check
    logger.error("Command error: %r", error)
    msg = "An unexpected error occurred."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


bookinfo.error(_on_app_command_error)
getscan.error(_on_app_command_error)
getscanwfp.error(_on_app_command_error)
getscanrange.error(_on_app_command_error)
gettext.error(_on_app_command_error)
searchtext.error(_on_app_command_error)
searchinbook.error(_on_app_command_error)
gethadith.error(_on_app_command_error)
pagecache.error(_on_app_command_error)


@bot.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    logger.info("Logged in as %s (catalog: %s)", bot.user, CATALOG_DB)


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("DISCORD_TOKEN is not set in .env")
    if not os.path.exists(CATALOG_DB):
        sys.exit(
            f"Catalog database not found at '{CATALOG_DB}'.\n"
            f"Run `python build_catalog.py` once first to build it."
        )
    bot.run(TOKEN)