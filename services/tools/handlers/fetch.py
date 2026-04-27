"""URL fetch handler: retrieve text content from a URL."""

import os
import re

import httpx

_UA = "Mozilla/5.0 (compatible; colloc-tools/1.0)"
_MAX_BYTES = 512 * 1024  # 512 KB cap to avoid huge pages


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace. Output: plain text. Input: html string."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-z]+;", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


async def fetch(url: str, extract_text: bool = True) -> dict:
    """Fetch URL content. Output: dict with content/url/status_code. Input: url and extract_text flag."""
    if not os.getenv("TOOLS_FETCH_ENABLED", "false").lower() in ("true", "1", "yes"):
        return {"error": "url_fetch is disabled (TOOLS_FETCH_ENABLED=false)"}

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": _UA},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            raw = resp.content[:_MAX_BYTES]
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}", "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "url": url}

    is_html = "html" in content_type.lower()
    if extract_text and is_html:
        content = _strip_html(raw.decode("utf-8", errors="replace"))
    else:
        content = raw.decode("utf-8", errors="replace")

    return {
        "url": url,
        "status_code": resp.status_code,
        "content_type": content_type,
        "content": content[:8000],  # cap response sent to LLM
        "truncated": len(raw) == _MAX_BYTES,
    }
