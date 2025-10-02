# FILE PATH: source_code/router/backend/utils/config.py

import os

# ========================================
# API Rate Limits (requests per second)
# ========================================
# Set these to match your contracted limits
AT_VEHICLES_RPS = float(os.getenv("AT_VEHICLES_RPS", "100"))
AT_VALUATIONS_RPS = float(os.getenv("AT_VALUATIONS_RPS", "100"))
CAP_RPS = float(os.getenv("CAP_RPS", "300"))

# ========================================
# Concurrency Settings
# ========================================
# Default workers (good match for AT 100 RPS workloads)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "100"))

# Higher concurrency for CAP‑heavy runs (used dynamically for Motability)
# 150 workers gives room to overlap CAP and AT without breaching AT buckets.
MAX_WORKERS_CAP = int(os.getenv("MAX_WORKERS_CAP", "150"))

# HTTP connection pool per host
# IMPORTANT: For CAPIDValuation+GetUsedLive (2 calls/row), allow ample headroom
PER_HOST_LIMIT = int(os.getenv("PER_HOST_LIMIT", "400"))

# ========================================
# HTTP Client Settings
# ========================================
HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "10"))
HTTP_TOTAL_RETRIES = int(os.getenv("HTTP_TOTAL_RETRIES", "2"))
HTTP_BACKOFF_BASE = float(os.getenv("HTTP_BACKOFF_BASE", "0.05"))

# ========================================
# Progress Tracking
# ========================================
# Update database progress every N vehicles
PROGRESS_UPDATE_INTERVAL = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "100"))

# ========================================
# XLSX Generation
# ========================================
XLSX_WRITE_MODE = os.getenv("XLSX_WRITE_MODE", "write_only")

# ========================================
# Lambda / Runtime Hints
# ========================================
# For high concurrency:
# - Memory: >= 3008 MB (2 vCPUs)
# - Timeout: 300s
# - Ephemeral storage: 512 MB