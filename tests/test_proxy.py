import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set LOG_DIR before importing app modules
_test_log_dir = tempfile.mkdtemp()
os.environ["LOG_DIR"] = _test_log_dir

from logger import AsyncJSONLLogger, mask_api_key, mask_headers, new_request_id
from proxy import app, extract_session_info


@pytest.fixture
def client():
    return TestClient(app)


# --- Unit tests for logger utilities ---


class TestMaskApiKey:
    def test_masks_full_key(self):
        result = mask_api_key("sk-ant-api03-abcdefghijklmnop")
        assert result == "sk-ant-api0****"
        assert "abcdefghijklmnop" not in result
        assert "api03" not in result

    def test_masks_real_key_format(self):
        key = "sk-ant-api03-abc_gOoWD2xniTwDG-SKgbmIKv-kXF3YNtoUL9hgs90q"
        result = mask_api_key(key)
        assert result == "sk-ant-api0****"
        assert "gOoWD2" not in result

    def test_masks_short_key(self):
        result = mask_api_key("sk-ant-abc")
        assert result == "sk-ant-abc****"

    def test_no_key_passthrough(self):
        result = mask_api_key("not-a-key")
        assert result == "not-a-key"

    def test_masks_in_longer_string(self):
        result = mask_api_key("Bearer sk-ant-api03-xyz123456789")
        assert "xyz123456789" not in result


class TestMaskHeaders:
    def test_masks_api_key_header(self):
        headers = {"x-api-key": "sk-ant-api03-secret", "content-type": "application/json"}
        masked = mask_headers(headers)
        assert "secret" not in masked["x-api-key"]
        assert masked["content-type"] == "application/json"

    def test_masks_authorization_header(self):
        headers = {"authorization": "Bearer sk-ant-api03-secret"}
        masked = mask_headers(headers)
        assert "secret" not in masked["authorization"]

    def test_passthrough_other_headers(self):
        headers = {"x-custom": "value", "accept": "text/event-stream"}
        masked = mask_headers(headers)
        assert masked == headers


class TestNewRequestId:
    def test_format(self):
        rid = new_request_id()
        assert rid.startswith("req_")
        assert len(rid) == 20  # "req_" + 16 hex chars

    def test_unique(self):
        ids = {new_request_id() for _ in range(100)}
        assert len(ids) == 100


# --- Unit tests for session extraction ---


class TestExtractSessionInfo:
    def test_extracts_session_from_header(self):
        headers = {"x-session-id": "sess_abc123", "content-type": "application/json"}
        info = extract_session_info(headers, b"{}")
        assert info["session_id"] == "sess_abc123"

    def test_extracts_anthropic_session_header(self):
        headers = {"anthropic-session-id": "sess_xyz"}
        info = extract_session_info(headers, b"{}")
        assert info["session_id"] == "sess_xyz"

    def test_extracts_conversation_id_from_body_metadata(self):
        body = json.dumps({"metadata": {"conversation_id": "conv_456"}}).encode()
        info = extract_session_info({}, body)
        assert info["conversation_id"] == "conv_456"

    def test_extracts_session_id_from_body_metadata(self):
        body = json.dumps({"metadata": {"session_id": "sess_789"}}).encode()
        info = extract_session_info({}, body)
        assert info["conversation_id"] == "sess_789"

    def test_returns_none_when_absent(self):
        info = extract_session_info({}, b'{"model": "claude-sonnet-4-5-20250929"}')
        assert info["session_id"] is None
        assert info["conversation_id"] is None

    def test_handles_invalid_body(self):
        info = extract_session_info({}, b"not json")
        assert info["session_id"] is None
        assert info["conversation_id"] is None


# --- Unit tests for AsyncJSONLLogger ---


@pytest.mark.asyncio
async def test_logger_writes_jsonl():
    log_dir = tempfile.mkdtemp()
    with patch("logger.LOG_DIR", log_dir):
        log = AsyncJSONLLogger()
        await log.start()
        entry = {
            "id": "req_test",
            "timestamp": "2025-01-15T00:00:00+00:00",
            "method": "POST",
            "path": "/v1/messages",
        }
        await log.log(entry)
        # Give the writer loop time to process
        await asyncio.sleep(0.1)
        await log.stop()

    log_file = Path(log_dir) / "2025-01-15" / "requests.jsonl"
    assert log_file.exists()
    with open(log_file) as f:
        written = json.loads(f.readline())
    assert written["id"] == "req_test"
    assert written["method"] == "POST"


# --- Integration tests for proxy endpoints ---


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestProxyNonStreaming:
    def test_non_streaming_forward(self, client):
        mock_response = httpx.Response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps({"id": "msg_123", "content": [{"text": "Hello"}]}).encode(),
        )

        with patch("proxy.http_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            resp = client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-5-20250929", "messages": [{"role": "user", "content": "hi"}]},
                headers={"x-api-key": "sk-ant-api03-test", "content-type": "application/json"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "msg_123"

    def test_non_streaming_preserves_error_status(self, client):
        mock_response = httpx.Response(
            status_code=401,
            headers={"content-type": "application/json"},
            content=json.dumps({"error": {"message": "invalid api key"}}).encode(),
        )

        with patch("proxy.http_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            resp = client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-5-20250929", "messages": []},
                headers={"x-api-key": "sk-ant-api03-bad"},
            )

        assert resp.status_code == 401


class TestProxyStreaming:
    def test_streaming_forward(self, client):
        sse_chunks = [
            b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
            b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\"}\n\n",
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
        ]

        mock_response = httpx.Response(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"".join(sse_chunks)),
        )

        with patch("proxy.http_client") as mock_client:
            mock_client.build_request = lambda **kwargs: httpx.Request(
                method=kwargs["method"], url=kwargs["url"]
            )
            mock_client.send = AsyncMock(return_value=mock_response)

            resp = client.post(
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-5-20250929",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                headers={"x-api-key": "sk-ant-api03-test", "content-type": "application/json"},
            )

        assert resp.status_code == 200
        assert b"message_start" in resp.content


class TestPathPassthrough:
    def test_count_tokens_endpoint(self, client):
        mock_response = httpx.Response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps({"input_tokens": 10}).encode(),
        )

        with patch("proxy.http_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            resp = client.post(
                "/v1/messages/count_tokens",
                json={"model": "claude-sonnet-4-5-20250929", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 200
        assert resp.json()["input_tokens"] == 10
