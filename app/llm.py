"""Gemini LLM integration — AI book-summary generation (Google Gemini, free tier).

The Gemini API key lives ONLY here (server-side), read from GEMINI_API_KEY. The Android app never
sees it — it calls /v1/ai/summary, which calls this module. When no key is set, a demo sample is
returned so the whole flow works free.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .config import get_settings
from .envelope import ApiError

_client = None


def _has_key() -> bool:
    return bool(get_settings().gemini_api_key)


def _get_client():
    """Lazily build the Gemini client so the app still boots without the key/package."""
    global _client
    settings = get_settings()
    if not settings.gemini_api_key:
        raise ApiError(503, "AI service not configured (set GEMINI_API_KEY)")
    if _client is None:
        from google import genai  # imported lazily
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of the model reply (tolerates code fences / stray prose)."""
    text = (text or "").strip()
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
    """Sample summary used for UI testing when no GEMINI_API_KEY is set (fully free)."""
    demo = "(Nội dung demo để test giao diện — khi backend có GEMINI_API_KEY, phần này do AI thật viết.)"
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


def _model_candidates() -> list[str]:
    """Configured model first, then robust fallbacks — Google deprecates specific versions for
    new accounts, so we try a few current flash models until one is available to this key."""
    prefer = [
        get_settings().gemini_model,
        "gemini-flash-latest",
        "gemini-3.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
    ]
    out: list[str] = []
    for m in prefer:
        if m and m not in out:
            out.append(m)
    return out


def generate_summary(title: str, author: Optional[str] = None) -> dict:
    """Generate a structured book summary via Gemini. Returns a normalized dict.
    No key configured -> returns a demo sample so the UI is testable for free."""
    if not _has_key():
        return _mock_summary(title, author)
    client = _get_client()
    from google.genai import types  # imported lazily

    user = f"Book title: {title}" + (f"\nAuthor: {author}" if author else "")
    # thinking_budget=0 disables the default "thinking" on 2.5/3.x flash — otherwise it can eat the
    # whole output-token budget and return empty text.
    cfg = types.GenerateContentConfig(
        system_instruction=SUMMARY_SYSTEM,
        temperature=0.6,
        max_output_tokens=8000,
        response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    resp = None
    last_err: Optional[Exception] = None
    for model in _model_candidates():
        try:
            resp = client.models.generate_content(model=model, contents=user, config=cfg)
            break
        except Exception as e:  # model not available for this key / quota / network → try next
            last_err = e
            resp = None
    if resp is None:
        raise ApiError(502, f"AI generation failed: {last_err}")
    raw = (getattr(resp, "text", None) or "").strip()
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        raise ApiError(502, f"AI returned an unparseable summary (len={len(raw)}): {raw[:180]}")

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
