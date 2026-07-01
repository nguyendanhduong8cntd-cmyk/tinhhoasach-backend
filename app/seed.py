"""Idempotent dev seed: categories, books+chapters, today's free-daily rotation."""
from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Book, Category, Chapter, FreeDaily

_CATEGORIES = [
    {"id": "productivity", "name": "Năng suất", "icon": "bolt", "book_count": 2},
    {"id": "psychology", "name": "Tâm lý", "icon": "psychology", "book_count": 1},
    {"id": "finance", "name": "Tài chính", "icon": "payments", "book_count": 1},
]

_BOOKS = [
    {
        "id": "b_001", "title": "Atomic Habits", "author": "James Clear",
        "cover_url": "https://cdn.tinhhoasach.local/atomic.jpg",
        "description": "Thay đổi nhỏ, kết quả lớn — xây thói quen 1% mỗi ngày.",
        "category": ["productivity"], "insights": ["Thói quen 1% mỗi ngày cộng dồn.",
                                                    "Môi trường > ý chí."],
        "duration_min": 15, "chapter_count": 3, "pro_only": True, "rating": 4.8,
        "chapters": [
            {"idx": 0, "title": "Giới thiệu", "text_md": "# Giới thiệu\nThói quen là lãi kép.",
             "audio_path": "audio/b_001/0.mp3"},
            {"idx": 1, "title": "Quy luật 1 — Làm cho rõ ràng",
             "text_md": "# Quy luật 1\n...", "audio_path": "audio/b_001/1.mp3"},
            {"idx": 2, "title": "Quy luật 2 — Làm cho hấp dẫn",
             "text_md": "# Quy luật 2\n...", "audio_path": "audio/b_001/2.mp3"},
        ],
    },
    {
        "id": "b_002", "title": "Deep Work", "author": "Cal Newport",
        "cover_url": "https://cdn.tinhhoasach.local/deepwork.jpg",
        "description": "Tập trung sâu trong thế giới xao nhãng.",
        "category": ["productivity"], "insights": ["Tập trung sâu là siêu năng lực."],
        "duration_min": 12, "chapter_count": 2, "pro_only": True, "rating": 4.7,
        "chapters": [
            {"idx": 0, "title": "Deep Work là gì", "text_md": "# Deep Work\n...",
             "audio_path": "audio/b_002/0.mp3"},
            {"idx": 1, "title": "Bốn quy tắc", "text_md": "# Quy tắc\n...",
             "audio_path": "audio/b_002/1.mp3"},
        ],
    },
    {
        "id": "b_003", "title": "Thinking, Fast and Slow", "author": "Daniel Kahneman",
        "cover_url": "https://cdn.tinhhoasach.local/tfs.jpg",
        "description": "Hai hệ thống tư duy điều khiển quyết định của bạn.",
        "category": ["psychology"], "insights": ["Hệ 1 nhanh & cảm tính; Hệ 2 chậm & lý trí."],
        "duration_min": 18, "chapter_count": 1, "pro_only": True, "rating": 4.6,
        "chapters": [
            {"idx": 0, "title": "Hai hệ thống", "text_md": "# Hai hệ thống\n...",
             "audio_path": "audio/b_003/0.mp3"},
        ],
    },
    {
        "id": "b_004", "title": "The Psychology of Money", "author": "Morgan Housel",
        "cover_url": "https://cdn.tinhhoasach.local/psymoney.jpg",
        "description": "Hành vi quan trọng hơn kiến thức tài chính.",
        "category": ["finance"], "insights": ["Giàu là thứ bạn không thấy."],
        "duration_min": 14, "chapter_count": 1, "pro_only": False, "rating": 4.7,
        "chapters": [
            {"idx": 0, "title": "Không ai điên cả", "text_md": "# Mở đầu\n...",
             "audio_path": "audio/b_004/0.mp3"},
        ],
    },
]


def seed_all(db: Session) -> None:
    if db.execute(select(Book).limit(1)).first():
        return  # already seeded

    for c in _CATEGORIES:
        db.add(Category(**c))

    for b in _BOOKS:
        chapters = b.pop("chapters")
        db.add(Book(**b))
        for ch in chapters:
            db.add(Chapter(
                id=f"{b['id']}_{ch['idx']}", book_id=b["id"], idx=ch["idx"],
                title=ch["title"], text_md=ch["text_md"], audio_path=ch["audio_path"]))

    today = datetime.date.today().isoformat()
    db.add(FreeDaily(date=today, book_ids=["b_001"]))  # server-chosen free book of the day
    db.commit()
