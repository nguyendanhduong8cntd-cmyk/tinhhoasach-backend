"""Idempotent dev seed.

On an empty DB, seeds the full catalog from ``seed_data.json`` (100 books with
covers, chapters, categories, and today's free-daily) if that file is present;
otherwise falls back to a tiny built-in sample. Runs on every startup but only
writes when the ``books`` table is empty, so it is safe on ephemeral hosts
(e.g. a free Render web service whose disk resets on each deploy).
"""
from __future__ import annotations

import datetime
import json
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Book, Category, Chapter, FreeDaily

_SEED_JSON = os.path.join(os.path.dirname(__file__), "seed_data.json")

# Tiny fallback used only when seed_data.json is missing.
_FALLBACK_CATEGORIES = [
    {"id": "productivity", "name": "Năng suất", "icon": "bolt", "book_count": 2},
    {"id": "psychology", "name": "Tâm lý", "icon": "psychology", "book_count": 1},
    {"id": "finance", "name": "Tài chính", "icon": "payments", "book_count": 1},
]
_FALLBACK_BOOKS = [
    {
        "id": "b_001", "title": "Atomic Habits", "author": "James Clear",
        "cover_url": "", "description": "Thay đổi nhỏ, kết quả lớn.",
        "category": ["productivity"], "insights": ["Thói quen 1% mỗi ngày cộng dồn."],
        "duration_min": 15, "chapter_count": 1, "pro_only": False, "rating": 4.8, "lang": "en",
        "chapters": [{"idx": 0, "title": "Giới thiệu", "text_md": "# Giới thiệu\nThói quen là lãi kép.",
                      "audio_path": "audio/b_001/0.mp3"}],
    },
]


def _load_seed() -> dict:
    """Return {'books', 'categories', 'free_daily'} from JSON, or the fallback."""
    try:
        with open(_SEED_JSON, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("books"):
            return data
    except (OSError, ValueError):
        pass
    today = datetime.date.today().isoformat()
    return {"books": _FALLBACK_BOOKS, "categories": _FALLBACK_CATEGORIES,
            "free_daily": [{"date": today, "book_ids": ["b_001"]}]}


def seed_all(db: Session) -> None:
    if db.execute(select(Book).limit(1)).first():
        return  # already seeded

    data = _load_seed()

    for c in data.get("categories", []):
        db.add(Category(id=c["id"], name=c["name"], icon=c.get("icon", ""),
                        book_count=c.get("book_count", 0)))

    for b in data["books"]:
        chapters = b.get("chapters", [])
        db.add(Book(
            id=b["id"], title=b["title"], author=b["author"], cover_url=b.get("cover_url", ""),
            description=b.get("description", ""), category=b.get("category", []),
            insights=b.get("insights", []), duration_min=b.get("duration_min", 0),
            chapter_count=b.get("chapter_count", len(chapters)),
            pro_only=bool(b.get("pro_only", False)), rating=b.get("rating", 0.0),
            lang=b.get("lang", "en"),
        ))
        for ch in chapters:
            db.add(Chapter(
                id=f"{b['id']}_{ch['idx']}", book_id=b["id"], idx=ch["idx"],
                title=ch["title"], text_md=ch["text_md"], audio_path=ch.get("audio_path", "")))

    # Free-daily: use the seeded rows, but always ensure TODAY has an entry.
    today = datetime.date.today().isoformat()
    seen_today = False
    for fd in data.get("free_daily", []):
        date = fd["date"]
        if date == today:
            seen_today = True
        db.add(FreeDaily(date=date, book_ids=fd.get("book_ids", [])))
    if not seen_today:
        first_ids = [b["id"] for b in data["books"][:5]]
        db.merge(FreeDaily(date=today, book_ids=first_ids))

    db.commit()
