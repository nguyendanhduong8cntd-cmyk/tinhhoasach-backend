"""Claude LLM integration — AI book-summary generation + Alpha Helper chatbot.

The Anthropic API key lives ONLY here (server-side), read from ANTHROPIC_API_KEY. The Android app
never sees it — it calls the /v1/ai/* endpoints, which call this module. Model: claude-opus-4-8.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .config import get_settings
from .envelope import ApiError

_client = None


def _has_key() -> bool:
    return bool(get_settings().anthropic_api_key)


def _get_client():
    """Lazily build the Anthropic client so the app still boots without the key/package."""
    global _client
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise ApiError(503, "AI service not configured (set ANTHROPIC_API_KEY)")
    if _client is None:
        import anthropic  # imported lazily
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _reply_text(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of the model reply (tolerates code fences / stray prose)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


SUMMARY_SYSTEM = (
    "You are a professional book-summary writer for a book-summary mobile app. Given a book title "
    "(and optionally an author), write a concise, faithful, well-structured summary a reader can "
    "finish in ~15 minutes. Write in the SAME LANGUAGE as the title the user gives (a Vietnamese "
    "title -> Vietnamese summary; an English title -> English). Be accurate for real, known books; "
    "if the title is not a real book, still produce a coherent summary built around the title's theme.\n\n"
    "Return ONLY a JSON object — no prose, no markdown, no code fences — matching EXACTLY this shape:\n"
    "{\n"
    '  "title": string,          // cleaned book title\n'
    '  "author": string,         // best-known author, or "Unknown"\n'
    '  "description": string,    // one-sentence subtitle / hook\n'
    '  "category": string,       // one short genre label\n'
    '  "insights": [string],     // 3-5 key takeaways, one sentence each\n'
    '  "chapters": [             // 4-6 chapters\n'
    '    { "title": string, "text_md": string }   // text_md = 2-4 short markdown paragraphs\n'
    "  ],\n"
    '  "final_summary": string   // 2-3 sentence closing takeaway\n'
    "}"
)


def _mock_summary(title: str, author: Optional[str]) -> dict:
    """Sample summary used for UI testing when no ANTHROPIC_API_KEY is set (no credit spent)."""
    demo = "(Nội dung demo để test giao diện — khi backend có ANTHROPIC_API_KEY và tài khoản có credit, phần này do AI thật viết.)"
    return {
        "title": title,
        "author": author or "Tác giả",
        "description": f"Bản tóm tắt mẫu (demo) cho \"{title}\".",
        "category": "General",
        "insights": [
            f"Ý tưởng cốt lõi của \"{title}\" là thay đổi tư duy để hành động hiệu quả hơn.",
            "Những thay đổi nhỏ, nhất quán tạo ra kết quả lớn theo thời gian.",
            "Hiểu nguyên nhân gốc rễ quan trọng hơn xử lý triệu chứng bề mặt.",
            "Áp dụng ngay một điều nhỏ hôm nay tốt hơn kế hoạch hoàn hảo ngày mai.",
        ],
        "chapters": [
            {"title": "Chương 1 — Bối cảnh & vấn đề",
             "text_md": f"Cuốn \"{title}\" mở đầu bằng việc đặt ra vấn đề trung tâm và vì sao nó quan trọng.\n\n{demo}"},
            {"title": "Chương 2 — Nguyên tắc chính",
             "text_md": f"Tác giả trình bày những nguyên tắc nền tảng và ví dụ minh hoạ.\n\n{demo}"},
            {"title": "Chương 3 — Áp dụng thực tế",
             "text_md": f"Phần này biến lý thuyết thành các bước hành động cụ thể bạn làm được ngay.\n\n{demo}"},
        ],
        "final_summary": f"Tổng kết: \"{title}\" cho thấy thay đổi bền vững đến từ hệ thống và thói quen, không phải ý chí nhất thời. (Bản demo.)",
    }


def generate_summary(title: str, author: Optional[str] = None) -> dict:
    """Generate a structured book summary via Claude. Returns a normalized dict.
    No key configured -> returns a demo sample so the UI is testable without spending credit."""
    if not _has_key():
        return _mock_summary(title, author)
    client = _get_client()
    settings = get_settings()
    user = f"Book title: {title}" + (f"\nAuthor: {author}" if author else "")
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=8000,
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    try:
        data = _extract_json(_reply_text(resp))
    except (json.JSONDecodeError, ValueError):
        raise ApiError(502, "AI returned an unparseable summary")

    data.setdefault("title", title)
    data.setdefault("author", author or "Unknown")
    data.setdefault("description", "")
    data.setdefault("category", "General")
    if not isinstance(data.get("insights"), list):
        data["insights"] = []
    if not isinstance(data.get("chapters"), list):
        data["chapters"] = []
    data.setdefault("final_summary", "")
    return data


CHAT_SYSTEM = (
    "You are 'Alpha Helper', a friendly reading assistant inside a book-summary app. Help users "
    "discover books, explain ideas from books, recommend titles by topic or mood, and answer "
    "questions about reading. Keep replies short and conversational (2-5 sentences) unless the user "
    "asks for more. Reply in the SAME LANGUAGE the user writes in (default Vietnamese). When you "
    "recommend books, give the title plus a one-line reason."
)


def _mock_chat(message: str) -> str:
    """Sample chatbot reply for UI testing when no ANTHROPIC_API_KEY is set (no credit spent)."""
    return (
        f"Mình đã nhận: \"{message}\".\n\n"
        "Gợi ý (demo): thử \"Atomic Habits\" — xây thói quen tí hon, hoặc \"Deep Work\" — làm việc "
        "tập trung sâu. Bạn muốn thể loại nào để mình gợi ý thêm?\n\n"
        "(Đây là câu trả lời demo để test giao diện chat. Khi backend có ANTHROPIC_API_KEY + credit, "
        "Alpha Helper sẽ trả lời bằng AI thật.)"
    )


def chat(message: str, history: Optional[list] = None) -> str:
    """Alpha Helper chatbot turn. `history` is a list of {role, content} (last ~10 kept).
    No key configured -> returns a demo reply so the chat UI is testable without spending credit."""
    if not _has_key():
        return _mock_chat(message)
    client = _get_client()
    settings = get_settings()
    messages = []
    for turn in (history or [])[-10:]:
        role, content = turn.get("role"), (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=CHAT_SYSTEM,
        messages=messages,
    )
    return _reply_text(resp)
