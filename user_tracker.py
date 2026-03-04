import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import redis.asyncio as aioredis

from config import REDIS_URL, USER_DB_PATH

logger = logging.getLogger(__name__)

REDIS_KEY = "unique_users"


class UserTracker:
    def __init__(self):
        self._redis: aioredis.Redis | None = None
        self._db: aiosqlite.Connection | None = None

    async def start(self):
        # Init SQLite
        try:
            db_path = Path(USER_DB_PATH)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(str(db_path))
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS unique_users ("
                "  hash TEXT PRIMARY KEY,"
                "  first_seen TEXT NOT NULL,"
                "  date TEXT NOT NULL"
                ")"
            )
            await self._db.commit()
        except Exception:
            logger.exception("Failed to init SQLite for user tracking")
            self._db = None

        # Init Redis
        try:
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            await self._redis.ping()
        except Exception:
            logger.warning("Redis unavailable at startup, will use SQLite only")
            self._redis = None

        # Seed Redis from SQLite
        if self._redis and self._db:
            try:
                async with self._db.execute("SELECT hash, date FROM unique_users") as cursor:
                    rows = await cursor.fetchall()
                if rows:
                    pipe = self._redis.pipeline()
                    for hash_val, date_val in rows:
                        pipe.sadd(REDIS_KEY, hash_val)
                        pipe.sadd(f"{REDIS_KEY}:{date_val}", hash_val)
                    await pipe.execute()
                    logger.info("Seeded Redis with %d users from SQLite", len(rows))
            except Exception:
                logger.exception("Failed to seed Redis from SQLite")

    async def track(self, key_hash: str):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        is_new_global = False

        # Try Redis first
        if self._redis:
            try:
                is_new_global = await self._redis.sadd(REDIS_KEY, key_hash) == 1
                await self._redis.sadd(f"{REDIS_KEY}:{today}", key_hash)
            except Exception:
                logger.warning("Redis error during track, falling back to SQLite")
                self._redis = None

        # Persist to SQLite if new (or if Redis is down, try SQLite-based dedup)
        if self._db:
            try:
                if is_new_global or not self._redis:
                    await self._db.execute(
                        "INSERT OR IGNORE INTO unique_users (hash, first_seen, date) "
                        "VALUES (?, ?, ?)",
                        (key_hash, datetime.now(timezone.utc).isoformat(), today),
                    )
                    await self._db.commit()
            except Exception:
                logger.warning("SQLite error during track")

    async def get_stats(self) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Try Redis first
        if self._redis:
            try:
                total = await self._redis.scard(REDIS_KEY)
                today_count = await self._redis.scard(f"{REDIS_KEY}:{today}")
                return {"unique_users": total, "unique_users_today": today_count}
            except Exception:
                logger.warning("Redis error during get_stats, falling back to SQLite")

        # Fallback to SQLite
        if self._db:
            try:
                async with self._db.execute("SELECT COUNT(*) FROM unique_users") as cur:
                    total = (await cur.fetchone())[0]
                async with self._db.execute(
                    "SELECT COUNT(*) FROM unique_users WHERE date = ?", (today,)
                ) as cur:
                    today_count = (await cur.fetchone())[0]
                return {"unique_users": total, "unique_users_today": today_count}
            except Exception:
                logger.warning("SQLite error during get_stats")

        return {"unique_users": 0, "unique_users_today": 0}

    async def stop(self):
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception:
                pass
        if self._db:
            try:
                await self._db.close()
            except Exception:
                pass
