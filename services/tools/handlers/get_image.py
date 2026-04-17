"""Image search handler: Brave Images API with fallback descriptive response."""

import os

import httpx

BRAVE_IMAGES_URL = "https://api.search.brave.com/res/v1/images/search"


async def search_image(query: str, num_results: int = 5) -> dict:
    """Search images. Output: dict with image results list. Input: query and count."""
    if not os.getenv("TOOLS_GET_IMAGE_ENABLED", "false").lower() in ("true", "1", "yes"):
        return {"error": "get_image is disabled (TOOLS_GET_IMAGE_ENABLED=false)"}

    key = os.getenv("TOOLS_WEBSEARCH_BRAVE_KEY", "").strip()
    if key:
        return await _brave_images(query, num_results, key)

    # No key: return a Google Images search URL as fallback
    import urllib.parse
    google_url = f"https://www.google.com/search?tbm=isch&q={urllib.parse.quote(query)}"
    return {
        "provider": "fallback_url",
        "query": query,
        "note": "No Brave API key configured. Set TOOLS_WEBSEARCH_BRAVE_KEY for direct results.",
        "search_url": google_url,
        "results": [],
    }


async def _brave_images(query: str, num_results: int, api_key: str) -> dict:
    """Brave Image Search API. Output: results dict. Input: query, count, key."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": min(num_results, 20)}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(BRAVE_IMAGES_URL, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"Brave API error {exc.response.status_code}", "query": query, "results": []}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "query": query, "results": []}

    results = []
    for item in data.get("results", [])[:num_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "image_url": item.get("thumbnail", {}).get("src", ""),
            "source": item.get("source", ""),
        })
    return {"provider": "brave", "query": query, "results": results}
