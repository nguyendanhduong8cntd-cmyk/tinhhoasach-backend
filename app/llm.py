"""Gemini LLM integration — AI book-summary generation (Google Gemini, free tier).

The Gemini API key lives ONLY here (server-side), read from GEMINI_API_KEY. The Android app never
sees it — it calls /v1/ai/summary, which calls this module. When no key is set, a demo sample is
returned so the whole flow works free.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import requests

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


# ── OpenAI-compatible free providers (Groq, then Cerebras) ───────────────────
# Both speak the OpenAI /chat/completions shape, so one small client covers them. They are tried
# BEFORE Gemini because they have real free tiers, while newly-created Gemini keys return quota
# limit:0 on every model.
_GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # strong all-rounder, good Vietnamese
    "openai/gpt-oss-120b",
    "moonshotai/kimi-k2-instruct",
    "llama-3.1-8b-instant",      # fastest fallback
]
_CEREBRAS_MODELS = [
    "llama-3.3-70b",
    "qwen-3-235b-a22b-instruct-2507",
    "llama3.1-8b",
]
# Model ids that can't do normal chat completions — skipped during auto-discovery.
_NON_CHAT_HINTS = ("whisper", "tts", "embed", "guard", "rerank", "vision", "image", "audio")


def _provider_chain() -> list[dict]:
    """Configured OpenAI-compatible providers, in priority order. Empty when no keys are set."""
    s = get_settings()
    out: list[dict] = []
    if s.groq_api_key:
        out.append({"name": "groq", "base": "https://api.groq.com/openai/v1", "key": s.groq_api_key,
                    "models": [m for m in ([s.groq_model] if s.groq_model else []) + _GROQ_MODELS if m]})
    if s.cerebras_api_key:
        out.append({"name": "cerebras", "base": "https://api.cerebras.ai/v1", "key": s.cerebras_api_key,
                    "models": [m for m in ([s.cerebras_model] if s.cerebras_model else []) + _CEREBRAS_MODELS if m]})
    return out


def _discover_models(p: dict) -> list[str]:
    """Ask the provider which models the key can use — self-heals when a hard-coded id is retired."""
    try:
        r = requests.get(f"{p['base']}/models",
                         headers={"Authorization": f"Bearer {p['key']}"}, timeout=15)
        r.raise_for_status()
        ids = [m.get("id", "") for m in (r.json().get("data") or [])]
        return [i for i in ids if i and not any(b in i.lower() for b in _NON_CHAT_HINTS)]
    except Exception:
        return []


def _oai_chat(p: dict, model: str, messages: list[dict], *, temperature: float,
              max_tokens: int, json_mode: bool, timeout: int) -> str:
    payload: dict = {"model": model, "messages": messages,
                     "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(f"{p['base']}/chat/completions",
                      headers={"Authorization": f"Bearer {p['key']}",
                               "Content-Type": "application/json"},
                      json=payload, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    return (r.json()["choices"][0]["message"].get("content") or "").strip()


def _run_provider_chain(messages: list[dict], *, errors: list[str], temperature: float = 0.7,
                        max_tokens: int = 1200, json_mode: bool = False,
                        timeout: int = 90) -> Optional[str]:
    """Try each provider over its candidate models; when the hard-coded list is exhausted, ask the
    API what it offers and try a few of those. Returns the first non-empty reply, else None."""
    for p in _provider_chain():
        models = list(p["models"])
        discovered = False
        i = 0
        while i < len(models):
            model = models[i]
            i += 1
            try:
                text = _oai_chat(p, model, messages, temperature=temperature,
                                 max_tokens=max_tokens, json_mode=json_mode, timeout=timeout)
                if text:
                    return text
                errors.append(f"{p['name']}/{model}: empty reply")
            except Exception as e:
                errors.append(f"{p['name']}/{model}: {e}"[:220])
            if i >= len(models) and not discovered:
                discovered = True
                models += [m for m in _discover_models(p) if m not in models][:4]
    return None


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


def _normalize_summary(data: dict, title: str, author: Optional[str]) -> dict:
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


def generate_summary(title: str, author: Optional[str] = None) -> dict:
    """Generate a structured book summary. Returns a normalized dict.

    Provider order: Groq -> Cerebras -> Gemini. Nothing configured -> demo sample."""
    user = f"Book title: {title}" + (f"\nAuthor: {author}" if author else "")
    errors: list[str] = []

    # 1) OpenAI-compatible providers, asking for a JSON object (8k out = full multi-chapter summary).
    if _provider_chain():
        raw = _run_provider_chain(
            [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}],
            errors=errors, temperature=0.6, max_tokens=8000, json_mode=True, timeout=120)
        if raw:
            try:
                return _normalize_summary(_extract_json(raw), title, author)
            except (json.JSONDecodeError, ValueError):
                errors.append(f"unparseable JSON (len={len(raw)}): {raw[:120]}")

    # 2) Gemini last resort.
    if not _has_key():
        if not _provider_chain():
            return _mock_summary(title, author)
        raise ApiError(502, "AI generation failed: " + " | ".join(errors[:6]))
    client = _get_client()
    from google.genai import types  # imported lazily

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
        errors.append(f"gemini: {last_err}"[:220])
        raise ApiError(502, "AI generation failed: " + " | ".join(errors[:6]))
    raw = (getattr(resp, "text", None) or "").strip()
    try:
        return _normalize_summary(_extract_json(raw), title, author)
    except (json.JSONDecodeError, ValueError):
        raise ApiError(502, f"AI returned an unparseable summary (len={len(raw)}): {raw[:180]}")


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
        "Khi backend có GROQ_API_KEY (hoặc CEREBRAS/GEMINI), mình sẽ trả lời thật bằng AI nhé!"
    )


def chat(messages: list[dict]) -> str:
    """Multi-turn chat reply. `messages` = [{role: 'user'|'assistant', content: str}, ...].

    Provider order: Groq -> Cerebras (both OpenAI-compatible, real free tiers) -> Gemini (last,
    because new free Gemini keys return quota limit:0). No provider configured -> demo reply."""
    errors: list[str] = []

    # 1) OpenAI-compatible providers. Drop leading assistant turns (our UI opens with a welcome
    #    bubble) so the conversation starts with the user, and prepend the system prompt.
    oai_msgs: list[dict] = [{"role": "system", "content": CHAT_SYSTEM}]
    for m in messages:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        role = "assistant" if m.get("role") == "assistant" else "user"
        if len(oai_msgs) == 1 and role == "assistant":
            continue
        oai_msgs.append({"role": role, "content": text})
    if len(oai_msgs) == 1:
        raise ApiError(400, "no user message to reply to")

    if _provider_chain():
        reply = _run_provider_chain(oai_msgs, errors=errors, temperature=0.7, max_tokens=1200)
        if reply:
            return reply

    # 2) Gemini last resort.
    if not _has_key():
        if not _provider_chain():
            return _mock_chat(messages)
        raise ApiError(502, "AI chat failed: " + " | ".join(errors[:6]))
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
        errors.append(f"gemini: {last_err}"[:220])
        raise ApiError(502, "AI chat failed: " + " | ".join(errors[:6]))
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


# Canonical cache code -> Google Translate target code. Google Translate has no Brazilian/European
# Portuguese split, so pt-BR collapses to "pt"; Chinese keeps the meaningful Simplified/Traditional
# split; Hebrew uses Google's legacy "iw".
_GT_CODES = {
    "vi": "vi", "ar": "ar", "de": "de", "es": "es", "fa": "fa", "fr": "fr", "hi": "hi",
    "id": "id", "it": "it", "he": "iw", "ja": "ja", "ko": "ko", "nl": "nl", "ru": "ru",
    "tr": "tr", "uk": "uk", "pt": "pt", "pt-BR": "pt", "zh": "zh-CN", "zh-TW": "zh-TW",
}


def translate_fields(description: str, insights: list, canon_code: str,
                     target_language: Optional[str] = None) -> Optional[dict]:
    """Translate a book's description + insights into the language of ``canon_code``.

    Engine = Google Translate via deep-translator: NO API key, NO quota, NO billing. We moved off
    Gemini here because newly-created Gemini keys return free-tier ``limit: 0`` (429) on every model,
    which made this feature unusable. Each string is translated individually so the insight list
    keeps its exact length and order.

    Returns {"description", "insights"} or None on any failure (caller then serves the English source
    text, so book loading can never break on a translation problem). Result is cached by the caller,
    so this runs at most once per (book, language)."""
    gt_code = _GT_CODES.get(canon_code)
    if not gt_code:
        return None
    try:
        from deep_translator import GoogleTranslator  # imported lazily

        translator = GoogleTranslator(source="auto", target=gt_code)

        def _tr(text: str) -> str:
            text = text or ""
            if not text.strip():
                return text
            out = translator.translate(text)
            return out if isinstance(out, str) and out.strip() else text

        out_desc = _tr(description or "")
        out_insights = [_tr(str(x)) for x in (insights or [])]
        return {"description": out_desc, "insights": out_insights}
    except Exception:
        return None  # never let a translation hiccup break book loading — serve the source text
