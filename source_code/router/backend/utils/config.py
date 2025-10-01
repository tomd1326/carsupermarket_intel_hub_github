import os

# Rates per second
AT_VEHICLES_RPS = float(os.getenv("AT_VEHICLES_RPS", "100"))
AT_VALUATIONS_RPS = float(os.getenv("AT_VALUATIONS_RPS", "100"))
CAP_RPS = float(os.getenv("CAP_RPS", "300"))

# Concurrency
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "80"))     # tuned to test_fixed.py
PER_HOST_LIMIT = int(os.getenv("PER_HOST_LIMIT", "80"))

# HTTP
HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "15"))
HTTP_TOTAL_RETRIES = int(os.getenv("HTTP_TOTAL_RETRIES", "3"))
HTTP_BACKOFF_BASE = float(os.getenv("HTTP_BACKOFF_BASE", "0.1"))

# XLSX
XLSX_WRITE_MODE = os.getenv("XLSX_WRITE_MODE", "write_only")
