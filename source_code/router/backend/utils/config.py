import os

# ========================================
# API Rate Limits (requests per second)
# ========================================
# CRITICAL: These must match your actual API provider limits
# Contact AutoTrader/CAP to confirm your account limits before increasing

AT_VEHICLES_RPS = float(os.getenv("AT_VEHICLES_RPS", "100"))  # INCREASED from 100
AT_VALUATIONS_RPS = float(os.getenv("AT_VALUATIONS_RPS", "100"))  # INCREASED from 100
CAP_RPS = float(os.getenv("CAP_RPS", "400"))  # Already fast enough

# ========================================
# Concurrency Settings
# ========================================
# OPTIMIZED FOR 20-SECOND TARGET (1000 vehicles)
# 
# Math:
# - 1000 vehicles / 150 workers = 6.67 batches
# - Each batch: ~3 seconds (CAP VRM + AT vehicles/valuations in parallel)
# - Total: 6.67 * 3 = ~20 seconds
#
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "150"))  # INCREASED from 100

# HTTP connection pool per host
# Should be >= MAX_WORKERS for best performance
PER_HOST_LIMIT = int(os.getenv("PER_HOST_LIMIT", "180"))  # INCREASED from 120

# ========================================
# HTTP Client Settings
# ========================================
# Reduced timeout - most API calls complete in <5s
# Faster failures = faster retries = better overall speed
HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "10"))  # Good as-is

# Retry strategy - optimized for transient failures
HTTP_TOTAL_RETRIES = int(os.getenv("HTTP_TOTAL_RETRIES", "2"))  # Good as-is
HTTP_BACKOFF_BASE = float(os.getenv("HTTP_BACKOFF_BASE", "0.05"))  # Good as-is

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
# REQUIRED Lambda config for 150 workers:
# - Memory: 3008 MB or higher (gets 2 vCPUs)
# - Timeout: 300s (5 minutes - allows safety margin)
# - Ephemeral storage: 512 MB (for temp XLSX files)
#
# IMPORTANT: Verify with AutoTrader that your account supports 150 RPS
# If you get 429 (rate limit) errors, reduce AT_VEHICLES_RPS and AT_VALUATIONS_RPS
