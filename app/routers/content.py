"""Content: /v1/books, /v1/books/{id}, /v1/categories, /v1/search.

Entitlement gating lives on the server: if the user isn't premium and the book
isn't today's free pick, only chapter 0 is unlocked — later chapters return
``locked: true`` with ``audio_url: null``. The client never unlocks itself.
"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import Book, Category, Chapter, FreeDaily, Translation, User, get_db, now_ms
from ..deps import require_api_key
from ..entitlement import refresh_entitlement
from ..envelope import ApiError
from ..storage import sign_audio_url
from .. import llm

router = APIRouter(prefix="/v1", tags=["content"], dependencies=[Depends(require_api_key)])


def _book_card(b: Book) -> dict:
    return {
        "id": b.id, "title": b.title, "author": b.author, "cover_url": b.cover_url,
        "category": b.category or [], "duration_min": b.duration_min,
        "chapter_count": b.chapter_count, "pro_only": bool(b.pro_only), "rating": b.rating,
    }


def _is_free_today(db: Session, book_id: str) -> bool:
    row = db.get(FreeDaily, datetime.date.today().isoformat())
    return bool(row and book_id in (row.book_ids or []))


@router.get("/books")
def list_books(
    category: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(Book)
    if category:
        # JSON array contains — portable enough: filter in Python after a broad fetch
        rows = db.execute(stmt).scalars().all()
        rows = [b for b in rows if category in (b.category or [])]
    else:
        rows = db.execute(stmt).scalars().all()
    total = len(rows)
    start = (page - 1) * limit
    page_rows = rows[start:start + limit]
    return {
        "books": [_book_card(b) for b in page_rows],
        "page": page,
        "has_more": start + limit < total,
    }


@router.get("/categories")
def list_categories(db: Session = Depends(get_db)):
    rows = db.execute(select(Category)).scalars().all()
    return {"categories": [
        {"id": c.id, "name": c.name, "icon": c.icon, "book_count": c.book_count} for c in rows
    ]}


@router.get("/search")
def search_books(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100),
                 db: Session = Depends(get_db)):
    like = f"%{q.lower()}%"
    rows = db.execute(
        select(Book).where(func.lower(Book.title).like(like) | func.lower(Book.author).like(like)).limit(limit)
    ).scalars().all()
    return {"books": [_book_card(b) for b in rows], "page": 1, "has_more": False}


def _localized_fields(db: Session, book: Book, lang: str) -> tuple[str, list]:
    """Return (description, insights) for `book` in the requested language.

    English / empty / unsupported → the stored source text unchanged. Otherwise: serve the cached
    translation if present, else translate ONCE via Gemini and cache it. Any failure falls back to
    the source text so book loading can never break on a translation problem."""
    src_desc = book.description or ""
    src_insights = book.insights or []
    canon = llm.canonical_lang(lang)
    if canon is None:
        return src_desc, src_insights
    code, name = canon

    cached = db.get(Translation, (book.id, code))
    if cached is not None:
        return cached.description or src_desc, cached.insights or src_insights

    tr = llm.translate_fields(src_desc, src_insights, code, name)
    if not tr:
        return src_desc, src_insights  # translation engine failed → source text

    # Cache for next time. Guard the write so a concurrent first-open of the same book+lang can't 500.
    try:
        db.add(Translation(book_id=book.id, lang=code, description=tr["description"],
                            insights=tr["insights"], created_at=now_ms()))
        db.commit()
    except Exception:
        db.rollback()
    return tr["description"], tr["insights"]


@router.get("/books/{book_id}")
def get_book(book_id: str = Path(...), uid: str = Query(...), lang: str = Query(""),
             db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    if book is None:
        raise ApiError(404, "Book not found")

    user = db.get(User, uid)
    if user is not None:
        user = refresh_entitlement(db, user)
    is_premium = bool(user and user.premium)
    is_free_today = _is_free_today(db, book_id)
    unlocked = is_premium or is_free_today or not book.pro_only

    chapters = db.execute(
        select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.idx)
    ).scalars().all()

    out_chapters = []
    for ch in chapters:
        # Free users: only chapter 0 of a pro book is open.
        locked = (not unlocked) and ch.idx > 0
        out_chapters.append({
            "index": ch.idx,
            "title": ch.title,
            "text_md": None if locked else ch.text_md,
            "audio_url": None if locked else sign_audio_url(ch.audio_path),
            "locked": locked,
        })

    # Tổng quan (description) + Ý tưởng chính (insights) localized to the app's language (cached).
    loc_desc, loc_insights = _localized_fields(db, book, lang)

    return {
        "id": book.id, "title": book.title, "author": book.author,
        "cover_url": book.cover_url, "description": loc_desc,
        "duration_min": book.duration_min, "pro_only": bool(book.pro_only),
        "is_free_today": is_free_today,
        "insights": loc_insights,
        "chapters": out_chapters,
    }
