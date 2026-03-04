import os


ANTHROPIC_API_BASE = os.environ.get("ANTHROPIC_API_BASE", "https://api.anthropic.com")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8080"))
LOG_DIR = os.environ.get("LOG_DIR", "./logs")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_PREFIX", "claude-proxy-logs")
UPSTREAM_READ_TIMEOUT = int(os.environ.get("UPSTREAM_READ_TIMEOUT", "300"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
USER_DB_PATH = os.environ.get("USER_DB_PATH", "./data/users.db")
