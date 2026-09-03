"""Cross-site request guard for a no-auth LAN app.

Browsers send form posts cross-site without preflight, so a page on any site the
operator visits could otherwise re-point a connection at an attacker's host (the
blank-keeps-key form then leaks the *arr key) or rewrite yt-dlp options. Modern
browsers label such requests with `Sec-Fetch-Site: cross-site`; older ones at least
send an `Origin`/`Referer` that does not match `Host`. Same-origin browser requests
and non-browser clients (curl, scripts: no Origin, no Sec-Fetch-Site) pass.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def cross_site(request: Request) -> str | None:
    """Reason the request is cross-site, or None when it is allowed."""
    if request.method not in UNSAFE:
        return None
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site == "cross-site":
        return "Sec-Fetch-Site: cross-site"
    if fetch_site in ("same-origin", "same-site", "none"):
        # the browser's own verdict; it also covers "Origin: null", which a same-origin
        # form sends when the page's referrer policy is no-referrer (a common proxy header)
        return None
    host = request.headers.get("host", "")
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        if value == "null":
            # a sandboxed iframe, a data:/file: page or a no-referrer form: never a
            # same-origin page of ours, and non-browser clients send no Origin at all
            return f"{header.title()} null"
        other = urlsplit(value).netloc
        if other and other.lower() != host.lower():
            return f"{header.title()} {other} does not match Host {host}"
    return None


class StaticCacheHeaders(BaseHTTPMiddleware):
    """Static assets are cacheable: without a Cache-Control the browser revalidated the
    logo on every server-rendered navigation and it flickered. A versioned URL
    (`?v=<token>`, the token changes with the image) is immutable; a bare one is fresh
    for an hour."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/") and response.status_code == 200:
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if "v" in request.query_params
                else "public, max-age=3600"
            )
        return response


class SameOriginGuard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        reason = cross_site(request)
        if reason is not None:
            return JSONResponse({"detail": f"cross-site request refused ({reason})"}, 403)
        return await call_next(request)
