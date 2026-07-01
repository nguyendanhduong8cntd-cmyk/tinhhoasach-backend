"""Audio URL signing.

Scaffold returns a short-TTL pseudo-signed URL. In prod, swap ``sign_audio_url``
for a Firebase Storage / S3 signed-URL call (server decides access — never let the
client unlock). Absolute http(s) seed URLs are passed through with an ``exp`` param.
"""
from __future__ import annotations

import time


def sign_audio_url(path: str | None, ttl_sec: int = 3600) -> str | None:
    if not path:
        return None
    exp = int(time.time()) + ttl_sec
    if path.startswith("http"):
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}exp={exp}"
    # TODO(prod): Firebase Admin SDK generate_signed_url / boto3 presign.
    return f"https://storage.tinhhoasach.local/{path.lstrip('/')}?exp={exp}"
