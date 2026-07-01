"""Per-user: /v1/library, /v1/highlights, /v1/progress, /v1/streak."""
from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, Header, Path, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import Book, Highlight, Library, Progress, User, get_db, now_ms
from ..deps import require_api_key
from ..entitlement import get_or_create_user
from ..envelope import ApiError, status_only
from ..schemas import HighlightCreate, LibraryAction, ProgressUpdate

router = APIRouter(prefix="/v1", tags=["per-user"], dependencies=[Depends(require_api_key)])


def _uid(uid_q: str | None, uid_h: str | None) -> str:
    uid = uid_q or uid_h
    if not uid:
        raise ApiError(400, "Missing uid")
    return uid


# ── Library ──────────────────────────────────────────────────────────
@router.post("/library")
def library_action(body: LibraryAction, uid: str = Query(None),
                   x_uid: str = Header(None), db: Session = Depends(get_db)):
    user_id = _uid(uid, x_uid)
    if body.action == "remove":
        db.execute(delete(Library).where(Library.uid == user_id, Library.book_id == body.book_id))
    else:
        if db.get(Library, {"uid": user_id, "book_id": body.book_id}) is None:
            db.add(Library(uid=user_id, book_id=body.book_id, saved_at=now_ms()))
    db.commit()
    return status_only()


@router.get("/library")
def library_list(uid: str = Query(None), x_uid: str = Header(None), db: Session = Depends(get_db)):
    user_id = _uid(uid, x_uid)
    rows = db.execute(select(Library).where(Library.uid == user_id)).scalars().all()
    books = []
    for r in rows:
        b = db.get(Book, r.book_id)
        if b:
            books.append({"id": b.id, "title": b.title, "author": b.author, "cover_url": b.cover_url})
    return {"books": books}


# ── Highlights ───────────────────────────────────────────────────────
@router.post("/highlights")
def add_highlight(body: HighlightCreate, uid: str = Query(None),
                  x_uid: str = Header(None), db: Session = Depends(get_db)):
    user_id = _uid(uid, x_uid)
    hid = uuid.uuid4().hex
    db.add(Highlight(
        id=hid, uid=user_id, book_id=body.book_id, chapter_index=body.chapter_index,
        text=body.text, color=body.color, created_at=now_ms()))
    db.commit()
    return {"status": {"code": 200, "message": "OK"}, "id": hid}


@router.get("/highlights")
def list_highlights(uid: str = Query(None), x_uid: str = Header(None), db: Session = Depends(get_db)):
    user_id = _uid(uid, x_uid)
    rows = db.execute(select(Highlight).where(Highlight.uid == user_id)
                      .order_by(Highlight.created_at.desc())).scalars().all()
    return {"highlights": [{
        "id": h.id, "book_id": h.book_id, "chapter_index": h.chapter_index,
        "text": h.text, "color": h.color, "created_at": h.created_at,
    } for h in rows]}


@router.delete("/highlights/{hid}")
def delete_highlight(hid: str = Path(...), uid: str = Query(None),
                     x_uid: str = Header(None), db: Session = Depends(get_db)):
    user_id = _uid(uid, x_uid)
    db.execute(delete(Highlight).where(Highlight.id == hid, Highlight.uid == user_id))
    db.commit()
    return status_only()


# ── Progress + streak ────────────────────────────────────────────────
def _bump_streak(user: User) -> bool:
    """Update streak on app-open. Returns True if the streak counter changed."""
    today = datetime.date.today()
    last = user.last_open_date
    if last == today.isoformat():
        return False  # already counted today
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    user.current_streak = (user.current_streak or 0) + 1 if last == yesterday else 1
    user.best_streak = max(user.best_streak or 0, user.current_streak)
    user.last_open_date = today.isoformat()
    return True


@router.post("/progress")
def save_progress(body: ProgressUpdate, uid: str = Query(None),
                  x_uid: str = Header(None), db: Session = Depends(get_db)):
    user_id = body.user_id or _uid(uid, x_uid)
    user = get_or_create_user(db, user_id)

    row = db.get(Progress, {"uid": user_id, "book_id": body.book_id})
    if row is None:
        db.add(Progress(uid=user_id, book_id=body.book_id, chapter_index=body.chapter_index,
                        position=body.position, updated_at=now_ms()))
    else:
        row.chapter_index = body.chapter_index
        row.position = body.position
        row.updated_at = now_ms()

    changed = _bump_streak(user)
    db.commit()
    db.refresh(user)
    return {"streak_updated": changed, "current_streak": user.current_streak or 0}


@router.get("/streak")
def get_streak(uid: str = Query(None), x_uid: str = Header(None), db: Session = Depends(get_db)):
    user_id = _uid(uid, x_uid)
    user = get_or_create_user(db, user_id)
    today = datetime.date.today()
    streak = user.current_streak or 0
    # Synthesize a 7-day window: the last `streak` days ending today are done.
    days = []
    for i in range(6, -1, -1):
        d = today - datetime.timedelta(days=i)
        done = i < streak  # i days ago is within the current streak
        days.append({"date": d.isoformat(), "done": done})
    return {"current_streak": streak, "best_streak": user.best_streak or 0, "days": days}
