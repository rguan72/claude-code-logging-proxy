import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import LOG_DIR, S3_BUCKET, S3_PREFIX

API_KEY_PATTERN = re.compile(r"(sk-ant-[a-zA-Z0-9]{0,4})[a-zA-Z0-9_-]*")


def mask_api_key(value: str) -> str:
    return API_KEY_PATTERN.sub(r"\1****", value)


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    masked = {}
    for k, v in headers.items():
        if k.lower() in ("x-api-key", "authorization"):
            masked[k] = mask_api_key(v)
        else:
            masked[k] = v
    return masked


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


class AsyncJSONLLogger:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._s3_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._writer_loop())
        if S3_BUCKET:
            self._s3_task = asyncio.create_task(self._s3_upload_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._s3_task:
            self._s3_task.cancel()
            try:
                await self._s3_task
            except asyncio.CancelledError:
                pass
        # Drain remaining items
        while not self._queue.empty():
            entry = self._queue.get_nowait()
            await self._write_entry(entry)

    async def log(self, entry: dict) -> None:
        await self._queue.put(entry)

    async def _writer_loop(self) -> None:
        while True:
            entry = await self._queue.get()
            await self._write_entry(entry)

    async def _write_entry(self, entry: dict) -> None:
        timestamp = entry.get("timestamp", datetime.now(timezone.utc).isoformat())
        date_str = timestamp[:10]  # YYYY-MM-DD
        log_dir = Path(LOG_DIR) / date_str
        log_file = log_dir / "requests.jsonl"

        def _write() -> None:
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")

        await asyncio.to_thread(_write)

    async def _s3_upload_loop(self) -> None:
        import boto3

        s3 = boto3.client("s3")
        while True:
            await asyncio.sleep(300)  # Every 5 minutes
            try:
                await asyncio.to_thread(self._upload_completed_logs, s3)
            except Exception as e:
                # Log but don't crash
                print(f"S3 upload error: {e}")

    def _upload_completed_logs(self, s3) -> None:
        log_root = Path(LOG_DIR)
        if not log_root.exists():
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for date_dir in sorted(log_root.iterdir()):
            if not date_dir.is_dir() or date_dir.name == today:
                continue
            jsonl_file = date_dir / "requests.jsonl"
            if not jsonl_file.exists():
                continue
            s3_key = f"{S3_PREFIX}/{date_dir.name}/requests.jsonl"
            s3.upload_file(str(jsonl_file), S3_BUCKET, s3_key)
            print(f"Uploaded {jsonl_file} to s3://{S3_BUCKET}/{s3_key}")
