"""
Lightning Density Data Module

Fetches global lightning strike data from available sources and normalizes
to atmospheric driver metrics for correlation with Schumann resonance.

Sources:
- Earth Networks / Vaisala (if API available)
- NOAA WWLLN (World Wide Lightning Location Network) fallback
- Demo/synthetic data for development
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from typing import Dict, Optional, List, Tuple
import math

# Configuration
LIGHTNING_API_SOURCE = os.environ.get("LIGHTNING_API_SOURCE", "demo")  # demo|earth_networks|wwlln
EARTH_NETWORKS_API_KEY = os.environ.get("EARTH_NETWORKS_API_KEY", "")
EARTH_NETWORKS_API_URL = os.environ.get("EARTH_NETWORKS_API_URL", "https://api.earthnetworks.com/v2/")

# State tracking
_lightning_cache = {
    "last_fetch": 0,
    "strike_count_1h": 0,
    "strike_count_15m": 0,
    "peak_current_ka": 0,
    "flash_rate_per_min": 0,
    "density_score": 0,  # 0-15 scale, matching SRI components
}
_strike_history = []  # Rolling list of recent strike timestamps

LIGHTNING_CACHE_TTL_SECONDS = 300  # 5-minute cache


def fetch_earth_networks_strikes(lookback_minutes: int = 15) -> Optional[Dict]:
    """
    Fetch lightning strike data from Earth Networks API.
    
    Args:
        lookback_minutes: How far back to query (typical: 15 min for real-time)
    
    Returns:
        Dict with strike_count, avg_peak_current_ka, density_score, or None on error
    """
    if not EARTH_NETWORKS_API_KEY:
        return None
    
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(minutes=lookback_minutes)
    
    params = {
        "start_time": start_utc.isoformat(),
        "end_time": now_utc.isoformat(),
        "limit": 10000
    }
    
    url = f"{EARTH_NETWORKS_API_URL}strokes?api_key={EARTH_NETWORKS_API_KEY}"
    for k, v in params.items():
        url += f"&{k}={v}"
    
    try:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        
        strikes = payload.get("data", [])
        if not strikes:
            return {
                "strike_count": 0,
                "avg_peak_current_ka": 0,
                "source": "earth_networks",
                "timestamp": now_utc.isoformat()
            }
        
        peak_currents = [abs(s.get("peak_current_ka", 0)) for s in strikes if s.get("peak_current_ka")]
        avg_current = sum(peak_currents) / len(peak_currents) if peak_currents else 0
        
        return {
            "strike_count": len(strikes),
            "avg_peak_current_ka": round(avg_current, 1),
            "source": "earth_networks",
            "timestamp": now_utc.isoformat()
        }
    except (HTTPError, URLError, json.JSONDecodeError, Exception):
        return None


def fetch_wwlln_strikes(lookback_minutes: int = 15) -> Optional[Dict]:
    """
    Fetch NOAA WWLLN (World Wide Lightning Location Network) data.
    
    Currently a placeholder; WWLLN data typically requires direct file access
    or a specific API endpoint that may not be publicly available.
    """
    # Placeholder: WWLLN data integration would go here
    # For now, return None to fall back to demo mode
    return None


def fetch_demo_strikes() -> Dict:
    """
    Generate synthetic lightning data for development and testing.
    
    Simulates realistic strike patterns with some variation.
    """
    now_utc = datetime.now(timezone.utc)
    
    # Simulate variable global strike rate (typical: 40-100 strikes/sec globally)
    # Demo range: 50-150 strikes per 15-minute window
    base_rate = 50 + (time.time() % 100)
    variability = math.sin(time.time() / 3600) * 30  # 1-hour cycle
    strike_count = int(base_rate + variability + (hash(int(time.time())) % 50))
    
    # Typical peak current: 20-30 kA
    avg_peak_current = 20 + (hash(int(time.time() / 60)) % 20)
    
    return {
        "strike_count": max(0, strike_count),
        "avg_peak_current_ka": avg_peak_current,
        "source": "demo",
        "timestamp": now_utc.isoformat()
    }


def compute_lightning_density_score(strike_count: int, avg_peak_current_ka: float) -> float:
    """
    Normalize lightning metrics to a 0-15 driver score (matching Kp/plasma/X-ray scales).
    
    Scoring logic:
    - Strike count: higher count → higher score
    - Peak current: higher average current → higher score (indicates stronger storms)
    
    Global baseline:
    - 50 strikes/15min = low activity
    - 150 strikes/15min = moderate activity
    - 300+ strikes/15min = high activity
    
    Args:
        strike_count: Number of lightning strikes in lookback window
        avg_peak_current_ka: Average peak current in kiloamperes
    
    Returns:
        Score 0-15 (float, rounded to 1 decimal)
    """
    # Strike count contribution (0-10 scale)
    # Normalize: 300 strikes/15min = 10.0, 0 strikes = 0
    strike_score = min(10.0, (strike_count / 300.0) * 10.0)
    
    # Peak current contribution (0-5 scale)
    # Normalize: 50 kA = 5.0, 0 kA = 0
    current_score = min(5.0, (avg_peak_current_ka / 50.0) * 5.0)
    
    # Combined score (0-15)
    total_score = strike_score + current_score
    
    return round(total_score, 2)


def get_lightning_data(use_cache: bool = True) -> Dict:
    """
    Get current global lightning density data.
    
    Returns normalized metrics and cached strike history for correlation analysis.
    
    Returns:
        Dict with:
        - strike_count_15m: Lightning strikes in last 15 minutes
        - avg_peak_current_ka: Average peak current (kiloamperes)
        - density_score: Normalized 0-15 driver score
        - flash_rate_per_min: Computed flash rate
        - source: Data source (earth_networks|wwlln|demo)
        - timestamp: UTC timestamp of measurement
    """
    global _lightning_cache, _strike_history
    
    now = time.time()
    
    # Return cached if still valid and requested
    if use_cache and (now - _lightning_cache.get("last_fetch", 0)) < LIGHTNING_CACHE_TTL_SECONDS:
        return {
            "strike_count_15m": _lightning_cache.get("strike_count_15m", 0),
            "avg_peak_current_ka": _lightning_cache.get("peak_current_ka", 0),
            "density_score": _lightning_cache.get("density_score", 0),
            "flash_rate_per_min": _lightning_cache.get("flash_rate_per_min", 0),
            "cached": True,
            "cache_age_seconds": int(now - _lightning_cache.get("last_fetch", 0)),
        }
    
    # Attempt to fetch from configured source
    lightning_data = None
    
    if LIGHTNING_API_SOURCE == "earth_networks":
        lightning_data = fetch_earth_networks_strikes(lookback_minutes=15)
    elif LIGHTNING_API_SOURCE == "wwlln":
        lightning_data = fetch_wwlln_strikes(lookback_minutes=15)
    
    # Fallback to demo if source unavailable or disabled
    if lightning_data is None:
        lightning_data = fetch_demo_strikes()
    
    strike_count = lightning_data.get("strike_count", 0)
    avg_current = lightning_data.get("avg_peak_current_ka", 0)
    
    # Compute normalized driver score
    density_score = compute_lightning_density_score(strike_count, avg_current)
    
    # Update cache
    _lightning_cache["last_fetch"] = now
    _lightning_cache["strike_count_15m"] = strike_count
    _lightning_cache["peak_current_ka"] = avg_current
    _lightning_cache["density_score"] = density_score
    _lightning_cache["flash_rate_per_min"] = round(strike_count / 15.0, 2)
    
    return {
        "strike_count_15m": strike_count,
        "avg_peak_current_ka": avg_current,
        "density_score": density_score,
        "flash_rate_per_min": _lightning_cache["flash_rate_per_min"],
        "source": lightning_data.get("source", "unknown"),
        "timestamp": lightning_data.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "cached": False,
    }


def record_strike(timestamp_utc: Optional[datetime] = None, peak_current_ka: float = 0.0) -> None:
    """
    Record an individual lightning strike for fine-grained analysis.
    
    Args:
        timestamp_utc: UTC timestamp of strike (default: now)
        peak_current_ka: Peak current in kiloamperes
    """
    global _strike_history
    
    if timestamp_utc is None:
        timestamp_utc = datetime.now(timezone.utc)
    
    _strike_history.append({
        "timestamp": timestamp_utc.isoformat(),
        "peak_current_ka": peak_current_ka
    })
    
    # Keep only last hour of strikes
    cutoff = time.time() - 3600
    _strike_history = [
        s for s in _strike_history
        if datetime.fromisoformat(s["timestamp"]).timestamp() > cutoff
    ]


def get_strike_history(minutes: int = 60) -> List[Dict]:
    """
    Get recorded strike history over a rolling window.
    
    Args:
        minutes: Lookback window in minutes
    
    Returns:
        List of strike records with timestamp and peak_current_ka
    """
    cutoff = time.time() - (minutes * 60)
    return [
        s for s in _strike_history
        if datetime.fromisoformat(s["timestamp"]).timestamp() > cutoff
    ]


# ============================================================================
# TESTING / CLI
# ============================================================================

if __name__ == "__main__":
    print("Lightning Data Module - Demo")
    print("=" * 60)
    
    # Test data fetching
    for i in range(3):
        print(f"\nFetch #{i+1}:")
        data = get_lightning_data(use_cache=False)
        print(f"  Strike count (15min): {data['strike_count_15m']}")
        print(f"  Avg peak current: {data['avg_peak_current_ka']} kA")
        print(f"  Density score (0-15): {data['density_score']}")
        print(f"  Flash rate: {data['flash_rate_per_min']} strikes/min")
        print(f"  Source: {data['source']}")
        time.sleep(1)
    
    # Test caching
    print(f"\n\nCaching Test:")
    data = get_lightning_data(use_cache=False)
    print(f"Fresh fetch, density score: {data['density_score']}, cached: {data['cached']}")
    
    data_cached = get_lightning_data(use_cache=True)
    print(f"Cached fetch, density score: {data_cached['density_score']}, cached: {data_cached['cached']}, age: {data_cached['cache_age_seconds']}s")
