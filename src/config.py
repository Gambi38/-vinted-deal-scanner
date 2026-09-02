#!/usr/bin/env python3
"""Configuration constants and defaults for Vinted scanner."""

from typing import Dict, Any

# ============================================================================
# Performance & Resource Limits
# ============================================================================

# Maximum listings to track across all runs
MAX_TRACKED = 320

# Maximum listings to recheck for fast updates (sold/reserved)
MAX_FAST_RECHECK = 5

# Time window to follow promising listings (minutes)
FOLLOW_WINDOW_MINUTES = 30

# Maximum deep detail checks per run (expensive: opens new page)
DETAIL_CANDIDATE_LIMIT = 4

# Maximum searches per run
MAX_SEARCHES_PER_RUN = 24

# Cards to evaluate per search
CARDS_PER_SEARCH = 12

# Page wait time before extracting cards (ms)
PAGE_WAIT_MS = 900

# Timeout for page load (ms)
PAGE_LOAD_TIMEOUT = 12000

# Timeout for listing detail page (ms)
DETAIL_TIMEOUT = 8000

# Budget for entire scan run (seconds)
RUN_BUDGET_SECONDS = 430

# Delay between searches (seconds)
DELAY_BETWEEN_SEARCHES = 0.25

# Poll interval for continuous mode (seconds)
POLL_SECONDS = 300


# ============================================================================
# Default Configuration Template
# ============================================================================

DEFAULT_CONFIG = {
    "base_url": "https://www.vinted.be",
    "poll_seconds": POLL_SECONDS,
    "delay_between_searches": DELAY_BETWEEN_SEARCHES,
    "page_wait_ms": PAGE_WAIT_MS,
    "max_items_per_search": 15,
    "shipping_estimate": 4.5,
    "buyer_protection_estimate": {
        "fixed": 0.7,
        "pct": 0.05
    },
    "min_margin": 30,
    "min_roi_pct": 20,
    "min_demand_score": 4,
    "searches": [],
}


# ============================================================================
# Logging Configuration
# ============================================================================

LOG_FORMAT = "[%(asctime)s] %(levelname)-8s %(name)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR


# ============================================================================
# Notification Settings
# ============================================================================

NTFY_SERVER = "https://ntfy.sh"
NTFY_TIMEOUT = 7  # seconds

NTFY_PRIORITY_MAP = {
    90: ("BANGER EXPRESS", "urgent", "fire,moneybag"),
    78: ("VENTE RAPIDE A VOIR", "high", "zap,shopping_cart"),
    0: ("A EVALUER - NOUVEAU", "default", "eyes,shopping_cart"),
}


# ============================================================================
# Browser Settings
# ============================================================================

BROWSER_VIEWPORT = {
    "width": 1280,
    "height": 900,
}

BROWSER_LOCALE = "fr-BE"

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

BROWSER_HEADLESS = True


# ============================================================================
# Data Retention
# ============================================================================

# Keep last N IDs in seen list
SEEN_HISTORY_LIMIT = 12000

# Keep last N fast sales for learning
FAST_SALES_LIMIT = 220

# Keep last N fast disappearances
FAST_DISAPPEARANCES_LIMIT = 220

# Keep last N positive examples
POSITIVE_EXAMPLES_LIMIT = 120

# Keep last N negative examples
NEGATIVE_EXAMPLES_LIMIT = 120


# ============================================================================
# Image Hashing
# ============================================================================

# Hamming similarity threshold for image matching
IMAGE_SIMILARITY_THRESHOLD = 0.72

# Image download size limit (bytes)
IMAGE_DOWNLOAD_LIMIT = 4_000_000

# Image hash size (bits)
IMAGE_HASH_SIZE = 64


# ============================================================================
# Similarity Thresholds (v8)
# ============================================================================

# Token similarity thresholds for learning
TOKEN_SIMILARITY_THRESHOLDS = {
    "very_close": 0.68,    # 24 point bonus
    "close": 0.50,         # 16 point bonus
    "similar": 0.36,       # 8 point bonus
    "positive_example": 0.62,  # positive example match
    "negative_example": 0.72,  # negative example (penalty)
}

# Weighted combo for fast sales scoring
FAST_SALES_WEIGHTS = {
    "token": 0.55,
    "price": 0.25,
    "image": 0.20,
}


# ============================================================================
# Error Handling
# ============================================================================

# Maximum retries for failed operations
MAX_RETRIES = 3

# Retry delay (seconds)
RETRY_DELAY = 1


# ============================================================================
# Data Paths (relative to project root)
# ============================================================================

DATA_PATHS = {
    "config": "config.json",
    "blacklist": "blacklist.json",
    "filtres": "filtres.json",
    "exemples": "exemples.txt",
    "rejets": "rejets.txt",
    "seen": "annonces_vues.json",
    "base": "base_apprentissage.json",
    "alerts": "alertes.csv",
    "history": "historique_annonces.jsonl",
}
