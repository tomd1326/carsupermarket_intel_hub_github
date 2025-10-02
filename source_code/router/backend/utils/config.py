import os

# ========================================
# API Rate Limits (requests per second)
# ========================================
# These match your API provider limits
AT_VEHICLES_RPS = float(os.getenv("AT_VEHICLES_RPS", "100"))
AT_VALUATIONS_RPS = float(os.getenv("AT_VALUATIONS_RPS", "100"))
CAP_RPS = float(os.getenv("CAP_RPS", "400"))

# ========================================
# Concurrency Settings
# ========================================
# Optimized for Lambda with 3GB+ memory
# MAX_WORKERS controls parallel vehicle processing
# Higher = more throughput but more memory
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "100"))  # Increased from 80

# HTTP connection pool per host
# Should be >= MAX_WORKERS for best performance
PER_HOST_LIMIT = int(os.getenv("PER_HOST_LIMIT", "120"))  # Increased from 80

# ========================================
# HTTP Client Settings
# ========================================
# Reduced timeout - most API calls complete in <5s
# Faster failures = faster retries = better overall speed
HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "10"))  # Reduced from 15

# Retry strategy - optimized for transient failures
HTTP_TOTAL_RETRIES = int(os.getenv("HTTP_TOTAL_RETRIES", "2"))  # Reduced from 3
HTTP_BACKOFF_BASE = float(os.getenv("HTTP_BACKOFF_BASE", "0.05"))  # Reduced from 0.1

# ========================================
# XLSX Generation
# ========================================
XLSX_WRITE_MODE = os.getenv("XLSX_WRITE_MODE", "write_only")

# ========================================
# Lambda-Specific Optimizations
# ========================================
# Set these in Lambda environment for production:
# AWS_REGION=eu-west-1 (for prod detection)
# 
# Recommended Lambda config:
# - Memory: 3008 MB (gets 2 vCPUs)
# - Timeout: 120s
# - Ephemeral storage: 512 MB (for temp XLSX files)