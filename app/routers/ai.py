"""AI features: LLM-generated book summaries + Alpha Helper chatbot (Claude, server-side).

The Anthropic key never leaves the server (see app/llm.py). Generated summaries are persisted as
normal Books+Chapters so they join the catalogue, are re-fetchable by /v1/books/{id}, and a repeat
request for the same title returns instantly from the DB instead of re-calling Claude.
"""
from __future__ import annotations

import hashlib
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import Book, Chapter, get_db
from ..deps import require_api_key
from ..envelope import ApiError
from ..llm import chat, generate_summary

router = APIRouter(prefix="/v1/ai", tags=["ai"], dependencies=[Depends(require_api_key)])


class SummaryReq(BaseModel):
    title: str
    author: str | None = None
    uid: str | None = None


class ChatMsg(BaseModel):
    role: str          # 'user' | 'assistant'
    content: str


class ChatReq(BaseModel):
    messages: list[ChatMsg]


def _ai_book_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "book"
    h = hashlib.sha1(title.lower().encode()).hexdigest()[:8]
    return f"ai-{slug}-{h}"


def _detail(book: Book, chapters: list[Chapter]) -> dict:
    """Fully-unlocked detail shape (AI summaries the user generated on request are readable)."""
    return {
        "id": book.id, "title": book.title, "author": book.author,
        "cover_url": book.cover_url or "", "description": book.description or "",
        "duration_min": book.duration_min, "pro_only": False, "is_free_today": False,
        "insights": book.insights or [],
        "chapters": [
            {"index": c.idx, "title": c.title, "text_md": c.text_md, "audio_url": None, "locked": False}
            for c in chapters
        ],
    }


@router.post("/summary")
def ai_summary(req: SummaryReq, db: Session = Depends(get_db)):
    title = (req.title or "").strip()
    if not title:
        raise ApiError(400, "title is required")
    book_id = _ai_book_id(title)

    # 1) Cache: an AI book we already made, or a catalogue book with the same title.
    existing = db.get(Book, book_id) or db.execute(
        select(Book).where(func.lower(Book.title) == title.lower())
    ).scalars().first()
    if existing is not None:
        chs = db.execute(
            select(Chapter).where(Chapter.book_id == existing.id).order_by(Chapter.idx)
        ).scalars().all()
        if chs:
            return _detail(existing, chs)

    # 2) Generate with Claude, then persist (upsert book + replace chapters).
    data = generate_summary(title, req.author)
    chapters_in = list(data.get("chapters") or [])
    fs = (data.get("final_summary") or "").strip()

    book = Book(
        id=book_id, title=data["title"], author=data["author"], cover_url="",
        description=data.get("description", ""), category=[data.get("category", "General")],
        insights=data.get("insights", []),
        duration_min=max(10, 3 * len(chapters_in)), chapter_count=len(chapters_in),
        pro_only=False, rating=4.7, lang="vi",
    )
    db.merge(book)
    db.execute(delete(Chapter).where(Chapter.book_id == book_id))

    chapters: list[Chapter] = []
    for i, ch in enumerate(chapters_in):
        c = Chapter(id=f"{book_id}-c{i}", book_id=book_id, idx=i,
                    title=(ch.get("title") or f"Chapter {i + 1}"),
                    text_md=(ch.get("text_md") or ""), audio_path=None)
        db.add(c)
        chapters.append(c)
    if fs:
        i = len(chapters)
        c = Chapter(id=f"{book_id}-c{i}", book_id=book_id, idx=i,
                    title="Final Summary", text_md=fs, audio_path=None)
        db.add(c)
        chapters.append(c)

    db.commit()
    return _detail(book, chapters)


@router.post("/chat")
def ai_chat(req: ChatReq):
    """Alpha Helper chatbot. Body: {messages:[{role,content}...]} -> {reply: str}."""
    msgs = [{"role": m.role, "content": m.content} for m in req.messages if (m.content or "").strip()]
    if not msgs:
        raise ApiError(400, "messages is required")
    return {"reply": chat(msgs)}
