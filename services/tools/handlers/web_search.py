"""Web search handler: Brave API (default) with DuckDuckGo Instant Answer fallback."""

import os
import re

import httpx

BRAVE_WEB_URL = "https://api.search.brave.com/res/v1/web/search"
DDG_INSTANT_URL = "https://api.duckduckgo.com/"


def _websearch_enabled() -> bool:
    """Check if web search is enabled. Output: bool. Input: none."""
    for key in ("TOOLS_WEBSEARCH_ENABLED", "TOOLS_WEB_SEARCH_ENABLED"):
        v = os.getenv(key, "").lower()
        if v in ("true", "1", "yes"):
            return True
    return False


def _brave_key() -> str:
    """Return Brave API key from env. Output: key string (may be empty). Input: none."""
    return os.getenv("TOOLS_WEBSEARCH_BRAVE_KEY", "").strip()


def _provider() -> str:
    """Return configured search provider. Output: provider name string. Input: none."""
    return os.getenv("TOOLS_WEBSEARCH_PROVIDER", "brave").lower().strip()


async def search(query: str, num_results: int = 5) -> dict:
    """Perform web search. Output: results dict with list of {title,url,snippet}. Input: query and count."""
    if not _websearch_enabled():
        return {"error": "web_search is disabled (TOOLS_WEBSEARCH_ENABLED=false)"}

    key = _brave_key()
    if _provider() == "brave" and key:
        return await _brave_search(query, num_results, key)
    return await _ddg_search(query, num_results)


async def _brave_search(query: str, num_results: int, api_key: str) -> dict:
    """Brave Search API. Output: results dict. Input: query, count, key."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": min(num_results, 20)}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(BRAVE_WEB_URL, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"Brave API error {exc.response.status_code}", "query": query, "results": []}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "query": query, "results": []}

    results = []
    for item in data.get("web", {}).get("results", [])[:num_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
        })
    return {"provider": "brave", "query": query, "results": results}


async def _ddg_search(query: str, num_results: int) -> dict:
    """DuckDuckGo Instant Answer API fallback. Output: results dict. Input: query and count."""
    params = {
        "q": query,
        "format": "json",
        "no_html": "1",
        "skip_disambig": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(DDG_INSTANT_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "query": query, "results": []}

    results = []

    # Abstract (single overview result)
    if data.get("Abstract"):
        results.append({
            "title": data.get("Heading", query),
            "url": data.get("AbstractURL", ""),
            "snippet": data.get("Abstract", ""),
        })

    # Related topics
    for topic in data.get("RelatedTopics", [])[:num_results]:
        if "Text" in topic and "FirstURL" in topic:
            results.append({
                "title": re.sub(r" - .*$", "", topic["Text"])[:80],
                "url": topic["FirstURL"],
                "snippet": topic["Text"],
            })
        elif "Topics" in topic:
            for sub in topic["Topics"][:3]:
                if "Text" in sub and "FirstURL" in sub:
                    results.append({
                        "title": re.sub(r" - .*$", "", sub["Text"])[:80],
                        "url": sub["FirstURL"],
                        "snippet": sub["Text"],
                    })

    return {"provider": "duckduckgo", "query": query, "results": results[:num_results]}
