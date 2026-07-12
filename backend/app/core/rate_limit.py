import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, max_requests: int = 100, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.request_counts: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        client_ip = request.client.host if request.client else "unknown"

        # Allow test client without rate limits if it matches testserver
        if client_ip == "testserver" and request.headers.get("X-Skip-Rate-Limit") == "true":
            return await call_next(request)

        now = time.time()
        # Filter timestamps within window
        self.request_counts[client_ip] = [
            t for t in self.request_counts[client_ip] if now - t < self.window_seconds
        ]

        if len(self.request_counts[client_ip]) >= self.max_requests:
            return JSONResponse(
                status_code=429, content={"detail": "Too many requests. Rate limit exceeded."}
            )

        self.request_counts[client_ip].append(now)
        return await call_next(request)
