import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from config import ANTHROPIC_API_BASE, UPSTREAM_READ_TIMEOUT
from logger import AsyncJSONLLogger, mask_headers, new_request_id

HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
)

logger = AsyncJSONLLogger()
http_client: httpx.AsyncClient = None  # type: ignore[assignment]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        base_url=ANTHROPIC_API_BASE,
        timeout=httpx.Timeout(connect=10, read=UPSTREAM_READ_TIMEOUT, write=30, pool=10),
        follow_redirects=True,
    )
    await logger.start()
    yield
    await logger.stop()
    await http_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


def forward_headers(request_headers: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in request_headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }


SESSION_HEADER_CANDIDATES = (
    "x-session-id",
    "anthropic-session-id",
    "x-claude-session-id",
)


def extract_session_info(headers: dict[str, str], body: bytes) -> dict[str, str | None]:
    """Extract session/conversation identifiers from headers and body metadata."""
    session_id = None
    for h in SESSION_HEADER_CANDIDATES:
        for k, v in headers.items():
            if k.lower() == h:
                session_id = v
                break
        if session_id:
            break

    conversation_id = None
    try:
        data = json.loads(body)
        meta = data.get("metadata", {})
        if isinstance(meta, dict):
            conversation_id = meta.get("conversation_id") or meta.get("session_id")
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    return {
        "session_id": session_id,
        "conversation_id": conversation_id,
    }


def is_streaming(body: bytes) -> bool:
    try:
        data = json.loads(body)
        return data.get("stream", False) is True
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def parse_response_headers(resp: httpx.Response) -> dict[str, str]:
    headers = {}
    for k, v in resp.headers.items():
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "content-encoding":
            headers[k] = v
    return headers


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    request_id = new_request_id()
    start_time = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()

    body = await request.body()
    raw_headers = dict(request.headers)
    headers = forward_headers(raw_headers)
    url = f"/v1/{path}"
    streaming = is_streaming(body)
    session_info = extract_session_info(raw_headers, body)

    should_log = (url == "/v1/messages")
    if should_log:
        try:
            data = json.loads(body)
            if data.get("max_tokens") == 1:
                should_log = False
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    if streaming:
        return await handle_streaming(
            request_id, timestamp, start_time, request.method, url, headers, body, session_info,
            should_log,
        )
    else:
        return await handle_non_streaming(
            request_id, timestamp, start_time, request.method, url, headers, body, session_info,
            should_log,
        )


async def handle_non_streaming(
    request_id: str,
    timestamp: str,
    start_time: float,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    session_info: dict[str, str | None],
    should_log: bool,
) -> Response:
    upstream_resp = await http_client.request(
        method=method,
        url=url,
        headers=headers,
        content=body,
    )
    resp_headers = parse_response_headers(upstream_resp)
    resp_body = upstream_resp.content

    background = None
    if should_log:
        duration_ms = (time.monotonic() - start_time) * 1000
        log_entry = build_log_entry(
            request_id=request_id,
            timestamp=timestamp,
            method=method,
            path=url,
            request_headers=headers,
            request_body=body,
            response_status=upstream_resp.status_code,
            response_headers=dict(upstream_resp.headers),
            response_body=resp_body,
            is_streaming=False,
            duration_ms=duration_ms,
            time_to_first_byte_ms=duration_ms,
            session_info=session_info,
        )
        background = BackgroundTask(logger.log, log_entry)

    return Response(
        content=resp_body,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        background=background,
    )


async def handle_streaming(
    request_id: str,
    timestamp: str,
    start_time: float,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    session_info: dict[str, str | None],
    should_log: bool,
) -> StreamingResponse:
    upstream_req = http_client.build_request(
        method=method,
        url=url,
        headers=headers,
        content=body,
    )
    upstream_resp = await http_client.send(upstream_req, stream=True)
    resp_headers = parse_response_headers(upstream_resp)
    response_chunks: list[bytes] = [] if should_log else None
    ttfb_ms: float | None = None

    async def stream_generator():
        nonlocal ttfb_ms
        try:
            async for chunk in upstream_resp.aiter_bytes():
                if should_log:
                    if ttfb_ms is None:
                        ttfb_ms = (time.monotonic() - start_time) * 1000
                    response_chunks.append(chunk)
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            await upstream_resp.aclose()

    async def log_after_stream():
        duration_ms = (time.monotonic() - start_time) * 1000
        full_response = b"".join(response_chunks)
        log_entry = build_log_entry(
            request_id=request_id,
            timestamp=timestamp,
            method=method,
            path=url,
            request_headers=headers,
            request_body=body,
            response_status=upstream_resp.status_code,
            response_headers=dict(upstream_resp.headers),
            response_body=full_response,
            is_streaming=True,
            duration_ms=duration_ms,
            time_to_first_byte_ms=ttfb_ms or duration_ms,
            session_info=session_info,
        )
        await logger.log(log_entry)

    background = BackgroundTask(log_after_stream) if should_log else None

    return StreamingResponse(
        content=stream_generator(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        background=background,
    )


def build_log_entry(
    *,
    request_id: str,
    timestamp: str,
    method: str,
    path: str,
    request_headers: dict[str, str],
    request_body: bytes,
    response_status: int,
    response_headers: dict[str, str],
    response_body: bytes,
    is_streaming: bool,
    duration_ms: float,
    time_to_first_byte_ms: float,
    session_info: dict[str, str | None],
) -> dict:
    # Try to decode bodies as JSON for structured logging
    try:
        req_body_parsed = json.loads(request_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        req_body_parsed = request_body.decode("utf-8", errors="replace")

    try:
        resp_body_parsed = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        resp_body_parsed = response_body.decode("utf-8", errors="replace")

    return {
        "id": request_id,
        "timestamp": timestamp,
        "session_id": session_info.get("session_id"),
        "conversation_id": session_info.get("conversation_id"),
        "method": method,
        "path": path,
        "request_headers": mask_headers(request_headers),
        "request_body": req_body_parsed,
        "response_status": response_status,
        "is_streaming": is_streaming,
        "duration_ms": round(duration_ms, 2),
        "time_to_first_byte_ms": round(time_to_first_byte_ms, 2),
    }
