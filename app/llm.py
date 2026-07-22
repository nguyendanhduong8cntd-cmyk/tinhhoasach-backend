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


# ── Alpha Helper chatbot ─────────────────────────────────────────────────────
CHAT_SYSTEM = (
    "You are Alpha Helper, a friendly and concise reading assistant living inside a book-summary "
    "app called 'AI Book Summaries & Ideas'. Help the user discover books, explain key ideas and "
    "concepts, summarize topics, and give reading recommendations. ALWAYS reply in the SAME LANGUAGE "
    "as the user's last message (Vietnamese in -> Vietnamese out; English in -> English out). Keep "
    "answers short and useful — usually 2-5 sentences — using plain text (no markdown headings, no "
    "tables). When recommending books, list a few titles with a one-line reason each. If the user "
    "asks something unrelated to books, reading, ideas or self-improvement, answer briefly and gently "
    "steer back to how you can help with books."
)


def _mock_chat(messages: list[dict]) -> str:
    """Demo reply when no GEMINI_API_KEY is set (keeps the chat UI testable for free)."""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = (m.get("content") or "").strip()
            break
    return (
        f"(demo) Mình là Alpha Helper 🤖. Bạn vừa hỏi: \"{last}\". "
        "Khi backend có GEMINI_API_KEY, mình sẽ trả lời thật bằng AI nhé!"
    )


def chat(messages: list[dict]) -> str:
    """Multi-turn chat reply via Gemini. `messages` = [{role: 'user'|'assistant', content: str}, ...].
    No key configured -> returns a demo reply so the UI is testable for free."""
    if not _has_key():
        return _mock_chat(messages)
    client = _get_client()
    from google.genai import types  # imported lazily

    # Map to Gemini roles ('user'/'model') and drop any leading non-user turns (Gemini requires the
    # conversation to start with a user message; our UI's welcome bubble is assistant-first).
    contents: list[dict] = []
    for m in messages:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        role = "model" if m.get("role") == "assistant" else "user"
        if not contents and role == "model":
            continue  # skip leading assistant/welcome turns
        contents.append({"role": role, "parts": [{"text": text}]})
    if not contents:
        raise ApiError(400, "no user message to reply to")

    cfg = types.GenerateContentConfig(
        system_instruction=CHAT_SYSTEM,
        temperature=0.7,
        max_output_tokens=1200,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    resp = None
    last_err: Optional[Exception] = None
    for model in _model_candidates():
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=cfg)
            break
        except Exception as e:
            last_err = e
            resp = None
    if resp is None:
        raise ApiError(502, f"AI chat failed: {last_err}")
    reply = (getattr(resp, "text", None) or "").strip()
    return reply or "…"


# ── Book-field translation (Tổng quan + Ý tưởng chính) ────────────────────────
# Maps the language tag the app sends (built from the user's chosen in-app language) to a canonical
# cache key + the human language NAME we hand Gemini. Regional variants collapse to one target EXCEPT
# where the difference is meaningful (Simplified vs Traditional Chinese, Brazilian vs European
# Portuguese). Anything starting with "en" (the source language) or unknown → NOT translated.
_LANG_NAMES = {
    "vi": "Vietnamese", "ar": "Arabic", "de": "German", "es": "Spanish", "fa": "Persian",
    "fr": "French", "hi": "Hindi", "id": "Indonesian", "it": "Italian", "iw": "Hebrew",
    "he": "Hebrew", "ja": "Japanese", "ko": "Korean", "nl": "Dutch", "ru": "Russian",
    "tr": "Turkish", "uk": "Ukrainian",
    "pt": "European Portuguese", "pt-BR": "Brazilian Portuguese",
    "zh": "Simplified Chinese", "zh-TW": "Traditional Chinese",
}


def canonical_lang(tag: Optional[str]):
    """(tag from the app) -> (canonical_cache_key, language_name) or None when it shouldn't translate.

    None means: source language (English), empty, or unsupported → serve the original text as-is."""
    if not tag:
        return None
    t = tag.strip().replace("_", "-")
    low = t.lower()
    if low.startswith("en"):
        return None                                  # source language — nothing to translate
    lang = low.split("-")[0]
    region = t.split("-")[1].upper() if "-" in t else ""
    # meaningful regional splits
    if lang == "zh":
        canon = "zh-TW" if region in ("TW", "HK", "MO") or "hant" in low else "zh"
    elif lang == "pt":
        canon = "pt-BR" if region == "BR" else "pt"
    elif lang == "in":                               # legacy Android code for Indonesian
        canon = "id"
    elif lang == "iw":                               # legacy Android code for Hebrew
        canon = "he"
    else:
        canon = lang
    name = _LANG_NAMES.get(canon)
    if not name:
        return None
    return canon, name


TRANSLATE_SYSTEM = (
    "You are a professional localizer for a book-summary app. You translate short UI content — a "
    "book's one-line overview and its list of key-idea bullet points — from English into a target "
    "language. Translate faithfully and naturally, the way a native reader would expect. Keep the "
    "meaning, tone and length similar. Do NOT translate well-known proper nouns, brand names or the "
    "book/author name. Keep the SAME NUMBER of insight items, in the SAME ORDER.\n\n"
    "Return ONLY a JSON object — no prose, no markdown, no code fences — of EXACTLY this shape:\n"
    '{ "description": string, "insights": [string] }'
)


def translate_fields(description: str, insights: list, target_language: str) -> Optional[dict]:
    """Translate a book's description + insights into ``target_language`` (a language NAME).
    Returns {"description", "insights"} or None on any failure / no key (caller falls back to source)."""
    if not _has_key():
        return None
    try:
        client = _get_client()
        from google.genai import types  # imported lazily

        payload = json.dumps({"description": description or "", "insights": insights or []},
                             ensure_ascii=False)
        user = f"Target language: {target_language}\n\nTranslate this JSON:\n{payload}"
        cfg = types.GenerateContentConfig(
            system_instruction=TRANSLATE_SYSTEM,
            temperature=0.3,
            max_output_tokens=2000,
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        resp = None
        for model in _model_candidates():
            try:
                resp = client.models.generate_content(model=model, contents=user, config=cfg)
                break
            except Exception:
                resp = None
        if resp is None:
            return None
        data = _extract_json((getattr(resp, "text", None) or "").strip())
        out_desc = data.get("description")
        out_ins = data.get("insights")
        if not isinstance(out_ins, list):
            out_ins = insights or []
        return {
            "description": out_desc if isinstance(out_desc, str) and out_desc.strip() else (description or ""),
            "insights": [str(x) for x in out_ins],
        }
    except Exception:
        return None  # never let a translation hiccup break book loading — serve the source text


# ── Diagnostics (temporary; never returns the key itself) ─────────────────────
def diagnose() -> dict:
    """List the models this API key can actually reach + probe each fallback candidate with a tiny
    generate, reporting per-model success/error. Lets us see WHY generation 429s (quota vs model
    unavailable) without exposing the key. Remove once the key situation is resolved."""
    out: dict = {"has_key": _has_key(), "candidates": _model_candidates()}
    if not _has_key():
        return out
    try:
        client = _get_client()
    except Exception as e:
        out["client_error"] = repr(e)[:300]
        return out
    try:
        names = []
        for m in client.models.list():
            nm = getattr(m, "name", "")
            acts = getattr(m, "supported_actions", None)
            names.append(nm + (f" [{','.join(acts)}]" if acts else ""))
        out["available_models"] = names
    except Exception as e:
        out["list_error"] = repr(e)[:300]
    from google.genai import types  # imported lazily
    probes: dict = {}
    for model in _model_candidates():
        try:
            r = client.models.generate_content(
                model=model, contents="Reply with the single word OK",
                config=types.GenerateContentConfig(
                    max_output_tokens=5,
                    thinking_config=types.ThinkingConfig(thinking_budget=0)))
            probes[model] = {"ok": True, "text": (getattr(r, "text", "") or "")[:40]}
        except Exception as e:
            probes[model] = {"ok": False, "error": repr(e)[:400]}
    out["probes"] = probes
    return out
