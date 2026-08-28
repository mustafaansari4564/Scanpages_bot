"""
Builds a local, searchable SQLite catalog of turath.io's entire book
library from their bulk data dump (the same file their own app loads on
first run) -- so a bot can look up book_id / author / has_pdf by name
without hitting the live /search API on every query.

Usage:
    from catalog import build_catalog, search_books, get_book_row

    build_catalog("turath_catalog.db")   # run once (or periodically)
    results = search_books("turath_catalog.db", "صحيح البخاري")
    for r in results:
        print(r["id"], r["name"], r["author_name"], r["has_pdf"])
"""

import json
import sqlite3
from pathlib import Path

import requests

from transliterate import transliterate

DATA_URL = "https://files.turath.io/data-v3.json"


def _fetch_bulk_data() -> dict:
    """Downloads turath's full books/authors/cats dump. This is a large
    file (thousands of books) -- expect it to take a little while."""
    resp = requests.get(DATA_URL, timeout=120)
    resp.raise_for_status()
    return resp.json()


def build_catalog(db_path: str = "turath_catalog.db", force: bool = False) -> str:
    """Downloads the bulk data and builds/rebuilds the local SQLite catalog.
    Safe to re-run periodically to pick up new books turath adds -- pass
    force=True to rebuild even if the db file already exists."""
    path = Path(db_path)
    if path.exists() and not force:
        return str(path)

    data = _fetch_bulk_data()
    books = data["books"]
    authors = data["authors"]
    cats = data["cats"]

    if path.exists():
        path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE books (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            name_en TEXT,
            author_id INTEGER,
            author_name TEXT,
            author_name_en TEXT,
            author_death INTEGER,
            cat_id INTEGER,
            cat_name TEXT,
            has_pdf INTEGER,
            size INTEGER
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE books_fts USING fts5(
            name, name_en, author_name, author_name_en,
            content='books', content_rowid='id'
        )
    """)

    rows = []
    for book_id_str, book in books.items():
        book_id = int(book_id_str)
        author = authors.get(str(book.get("author_id")), {})
        cat = cats.get(str(book.get("cat_id")), {})
        name = book.get("name", "")
        author_name = author.get("name", "")
        rows.append((
            book_id,
            name,
            transliterate(name),
            book.get("author_id"),
            author_name,
            transliterate(author_name),
            author.get("death"),
            book.get("cat_id"),
            cat.get("name", ""),
            1 if book.get("has_pdf") else 0,
            book.get("size", 0),
        ))

    conn.executemany(
        "INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    conn.execute("INSERT INTO books_fts(rowid, name, name_en, author_name, author_name_en) "
                 "SELECT id, name, name_en, author_name, author_name_en FROM books")
    conn.commit()
    conn.close()
    return str(path)


def search_books(db_path: str, query: str, limit: int = 10, pdf_only: bool = False) -> list[dict]:
    """Full-text search across book title + author name. Set pdf_only=True
    to only return books that have a real scanned PDF available. Searches the
    corrected scholarly transliterations too (name_en_corrected,
    author_name_corrected) when present."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # FTS5 needs the query terms quoted/escaped a bit for safety with
    # Arabic punctuation; wrap each token in double quotes as a phrase.
    tokens = query.strip().split()
    fts_query = " ".join(f'"{t}"' for t in tokens) if tokens else '""'

    sql = """
        SELECT b.id, b.name, b.name_en, b.name_en_corrected,
               b.author_name, b.author_name_en, b.author_name_corrected,
               b.author_death, b.cat_name, b.has_pdf, b.size
        FROM books_fts
        JOIN books b ON b.id = books_fts.rowid
        WHERE books_fts MATCH ?
    """
    params = [fts_query]
    if pdf_only:
        sql += " AND b.has_pdf = 1"
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        results = [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        # fall back to simple LIKE search if the FTS query syntax fails
        # (e.g. on punctuation-only input)
        like = f"%{query}%"
        sql2 = """
            SELECT id, name, name_en, name_en_corrected,
                   author_name, author_name_en, author_name_corrected,
                   author_death, cat_name, has_pdf, size
            FROM books
            WHERE name LIKE ? OR name_en LIKE ? OR name_en_corrected LIKE ?
               OR author_name LIKE ? OR author_name_en LIKE ? OR author_name_corrected LIKE ?
        """
        params2 = [like, like, like, like, like, like]
        if pdf_only:
            sql2 += " AND has_pdf = 1"
        sql2 += " LIMIT ?"
        params2.append(limit)
        results = [dict(r) for r in conn.execute(sql2, params2).fetchall()]
    finally:
        conn.close()
    return results


def display_name(row: dict) -> str:
    """Preferred English title: corrected scholarly transliteration, else the
    rough machine one, else a transliteration of the Arabic."""
    return row.get("name_en_corrected") or row.get("name_en") or ""


def display_author(row: dict) -> str:
    """Preferred English author: corrected, else rough, else empty."""
    return row.get("author_name_corrected") or row.get("author_name_en") or ""


def get_book_row(db_path: str, book_id: int) -> dict | None:
    """Look up a single book's cached catalog row by id."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


if __name__ == "__main__":
    print("This module needs network access to files.turath.io to build the "
          "catalog -- run build_catalog() in an environment with real "
          "internet access (e.g. Colab), not this sandbox.")