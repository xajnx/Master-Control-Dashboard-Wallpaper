from flask import Flask, Response, jsonify, request
from flask_cors import CORS
import psutil
import time
import os
import json
from datetime import date, datetime, timezone
from urllib.error import URLError, HTTPError
import ssl
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from schumann_adapter import adapt_schumann_payload
from lightning_data import get_lightning_data
from coherence_anomaly_index import compute_cai
import numpy as np
from scipy import signal
from PIL import Image, ImageDraw, ImageFont
import io as io_module

app = Flask(__name__)


def parse_allowed_origins():
    configured = os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "null,http://localhost:8080,http://127.0.0.1:8080,http://localhost:5500,http://127.0.0.1:5500"
    )
    origins = [o.strip() for o in configured.split(",") if o.strip()]
    return origins or ["null"]


CORS(app, resources={
    r"/*": {
        "origins": parse_allowed_origins()
    }
})

start_time = time.time()
_net_prev = {"bytes_sent": 0, "bytes_recv": 0, "ts": 0.0}
neo_cache = {}
tsunami_cache = {"cached_at": 0, "payload": None}
schumann_cache = {"cached_at": 0, "payload": None}
solar_cache = {"cached_at": 0, "payload": None}
weather_alerts_cache = {}
weather_current_cache = {}
spectrogram_cache = {}
NEO_CACHE_TTL_SECONDS = 6 * 3600
TSUNAMI_CACHE_TTL_SECONDS = 120
SCHUMANN_DERIVED_TTL_SECONDS = 300
SOLAR_COMPOSITE_CACHE_TTL_SECONDS = 60
WEATHER_ALERTS_CACHE_TTL_SECONDS = 60
WEATHER_CURRENT_CACHE_TTL_SECONDS = 60
SPECTROGRAM_PROXY_CACHE_TTL_SECONDS = 300
generated_spectrogram_cache = {"cached_at": 0, "payload": None}
SPECTROGRAM_GENERATED_CACHE_TTL_SECONDS = 300

SPECTROGRAM_INSECURE_HOST_ALLOWLIST = {
    host.strip().lower()
    for host in os.environ.get("SPECTROGRAM_INSECURE_HOST_ALLOWLIST", "sosrff.tsu.ru").split(",")
    if host.strip()
}
LUNAR_DISTANCE_KM = 384400
NEAR_MISS_LD_THRESHOLD = 0.75
CLOSE_LD_THRESHOLD = 3.0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TREND_HISTORY_PATH = os.path.join(DATA_DIR, "trend_history.json")
LOCAL_SCHUMANN_PATH = os.path.join(DATA_DIR, "schumann_response.json")
CORRELATION_EVENTS_PATH = os.path.join(DATA_DIR, "correlation_events.jsonl")

TSUNAMI_EVENT_ALLOWLIST = {
    "tsunami warning",
    "tsunami watch"
}

SEVERITY_SCORE = {"extreme": 4, "severe": 3, "moderate": 2, "minor": 1, "unknown": 0}
URGENCY_SCORE = {"immediate": 4, "expected": 3, "future": 2, "past": 1, "unknown": 0}
CERTAINTY_SCORE = {"observed": 3, "likely": 2, "possible": 1, "unlikely": 0, "unknown": 0}
NWS_USER_AGENT = os.environ.get(
    "NWS_USER_AGENT",
    "Mission-Control-Dashboard/1.0 (https://localhost, support@example.com)"
)

def get_uptime():
    uptime_seconds = time.time() - start_time
    hours = int(uptime_seconds // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    seconds = int(uptime_seconds % 60)
    return f"{hours}h {minutes}m {seconds}s"


def fetch_json(url, timeout=8):
    req = Request(url, headers={
        "User-Agent": NWS_USER_AGENT,
        "Accept": "application/geo+json, application/json"
    })

    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)
    except HTTPError as err:
        raise RuntimeError(f"Upstream HTTP {err.code}") from err
    except URLError as err:
        raise RuntimeError(f"Upstream network error: {err.reason}") from err
    except json.JSONDecodeError as err:
        raise RuntimeError("Upstream returned invalid JSON") from err


def fetch_json_with_metadata(url, timeout=8, headers=None):
    request_headers = {
        "User-Agent": NWS_USER_AGENT,
        "Accept": "application/geo+json, application/json"
    }
    if headers:
        request_headers.update(headers)

    req = Request(url, headers=request_headers)

    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            response_headers = resp.headers
            return {
                "status": getattr(resp, "status", 200),
                "data": json.loads(payload),
                "etag": response_headers.get("ETag"),
                "last_modified": response_headers.get("Last-Modified"),
                "cache_control": response_headers.get("Cache-Control")
            }
    except HTTPError as err:
        if err.code == 304:
            return {"status": 304, "data": None, "etag": None, "last_modified": None, "cache_control": None}
        raise RuntimeError(f"Upstream HTTP {err.code}") from err
    except URLError as err:
        raise RuntimeError(f"Upstream network error: {err.reason}") from err
    except json.JSONDecodeError as err:
        raise RuntimeError("Upstream returned invalid JSON") from err


def fetch_bytes(url, timeout=12, headers=None, verify_ssl=True):
    request_headers = {
        "User-Agent": NWS_USER_AGENT,
        "Accept": "image/*,*/*;q=0.8"
    }
    if headers:
        request_headers.update(headers)

    req = Request(url, headers=request_headers)

    context = None
    parsed = urlparse(url)
    if parsed.scheme == "https" and not verify_ssl:
        context = ssl._create_unverified_context()

    try:
        with urlopen(req, timeout=timeout, context=context) as resp:
            response_headers = resp.headers
            payload = resp.read()
            return {
                "status": getattr(resp, "status", 200),
                "bytes": payload,
                "content_type": response_headers.get("Content-Type") or "image/jpeg"
            }
    except HTTPError as err:
        raise RuntimeError(f"Upstream HTTP {err.code}") from err
    except URLError as err:
        raise RuntimeError(f"Upstream network error: {err.reason}") from err


def is_allowed_remote_image_url(raw_url):
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False

    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return False

    return True


def is_allowed_insecure_image_host(raw_url):
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except ValueError:
        return False

    if parsed.scheme != "https":
        return False

    hostname = (parsed.hostname or "").lower()
    return hostname in SPECTROGRAM_INSECURE_HOST_ALLOWLIST


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_text(value):
    return str(value or "unknown").strip().lower()


def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def parse_time_millis(value):
    if value is None:
        return 0

    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    if not text:
        return 0

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return 0

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def classify_proximity(distance_km):
    if distance_km is None:
        return "unknown"

    near_miss_km = LUNAR_DISTANCE_KM * NEAR_MISS_LD_THRESHOLD
    close_km = LUNAR_DISTANCE_KM * CLOSE_LD_THRESHOLD

    if distance_km <= near_miss_km:
        return "near-miss"
    if distance_km <= close_km:
        return "close"
    return "far"


def is_critical_tsunami_feature(feature):
    properties = feature.get("properties", {}) if isinstance(feature, dict) else {}
    event_name = normalize_text(properties.get("event"))
    if event_name not in TSUNAMI_EVENT_ALLOWLIST:
        return False

    severity = normalize_text(properties.get("severity"))
    urgency = normalize_text(properties.get("urgency"))
    certainty = normalize_text(properties.get("certainty"))

    severity_ok = SEVERITY_SCORE.get(severity, 0) >= SEVERITY_SCORE["severe"]
    urgency_ok = URGENCY_SCORE.get(urgency, 0) >= URGENCY_SCORE["expected"]
    certainty_ok = CERTAINTY_SCORE.get(certainty, 0) >= CERTAINTY_SCORE["likely"]

    return severity_ok or (urgency_ok and certainty_ok)


def sort_tsunami_features(features):
    def key(feature):
        properties = feature.get("properties", {}) if isinstance(feature, dict) else {}
        severity = SEVERITY_SCORE.get(normalize_text(properties.get("severity")), 0)
        urgency = URGENCY_SCORE.get(normalize_text(properties.get("urgency")), 0)
        certainty = CERTAINTY_SCORE.get(normalize_text(properties.get("certainty")), 0)
        timestamp = parse_time_millis(
            properties.get("sent")
            or properties.get("effective")
            or properties.get("onset")
            or properties.get("expires")
        )
        return (severity, urgency, certainty, timestamp)

    return sorted(features, key=key, reverse=True)


def build_weather_alerts_url(area=None, point=None):
    query = {
        "status": "actual",
        "message_type": "alert"
    }

    if area:
        query["area"] = area
    if point:
        query["point"] = point

    return f"https://api.weather.gov/alerts/active?{urlencode(query)}"


def weather_alerts_cache_key(area=None, point=None):
    area_key = (area or "").strip().upper()
    point_key = (point or "").strip()
    return f"area={area_key}|point={point_key}"


def prune_weather_alerts_cache(max_entries=32):
    if len(weather_alerts_cache) <= max_entries:
        return

    ordered = sorted(weather_alerts_cache.items(), key=lambda item: item[1].get("cached_at", 0), reverse=True)
    weather_alerts_cache.clear()
    weather_alerts_cache.update(dict(ordered[:max_entries]))


def get_weather_alerts(area=None, point=None):
    now = time.time()
    cache_key = weather_alerts_cache_key(area=area, point=point)
    cached = weather_alerts_cache.get(cache_key)

    if cached and (now - cached.get("cached_at", 0) <= WEATHER_ALERTS_CACHE_TTL_SECONDS):
        payload = dict(cached.get("payload", {}))
        payload["source"] = "cache"
        payload["stale"] = False
        return payload, 200

    headers = {}
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]

    try:
        upstream = fetch_json_with_metadata(build_weather_alerts_url(area=area, point=point), timeout=10, headers=headers)
    except RuntimeError as err:
        if cached:
            payload = dict(cached.get("payload", {}))
            payload["source"] = "cache-stale"
            payload["stale"] = True
            payload["error"] = str(err)
            return payload, 200
        return {"error": str(err), "features": [], "count": 0, "stale": False, "source": "nws-unavailable"}, 502

    if upstream.get("status") == 304 and cached:
        cached["cached_at"] = now
        payload = dict(cached.get("payload", {}))
        payload["source"] = "cache-revalidated"
        payload["stale"] = False
        return payload, 200

    data = upstream.get("data") if isinstance(upstream, dict) else None
    features = data.get("features", []) if isinstance(data, dict) else []
    if not isinstance(features, list):
        features = []

    payload = {
        "updated": data.get("updated") if isinstance(data, dict) else None,
        "features": features,
        "count": len(features),
        "source": "nws",
        "stale": False,
        "scope": {
            "area": area,
            "point": point
        }
    }

    weather_alerts_cache[cache_key] = {
        "cached_at": now,
        "payload": payload,
        "etag": upstream.get("etag"),
        "last_modified": upstream.get("last_modified")
    }
    prune_weather_alerts_cache()
    return dict(payload), 200


def get_weather_current(point):
    point_key = (point or "").strip()
    if not point_key:
        return {
            "error": "point is required (lat,lon)",
            "source": "invalid-request",
            "stale": False
        }, 400

    now = time.time()
    cached = weather_current_cache.get(point_key)
    if cached and (now - cached.get("cached_at", 0) <= WEATHER_CURRENT_CACHE_TTL_SECONDS):
        payload = dict(cached.get("payload", {}))
        payload["source"] = "cache"
        payload["stale"] = False
        return payload, 200

    try:
        point_data = fetch_json(f"https://api.weather.gov/points/{point_key}", timeout=10)
        point_props = (point_data or {}).get("properties") or {}
        stations_url = (point_props.get("observationStations") or "").strip()
        forecast_url = (point_props.get("forecast") or "").strip()
        if not stations_url:
            raise RuntimeError("NWS points payload missing observation stations URL")

        stations_data = fetch_json(stations_url, timeout=10)
        station_urls = stations_data.get("observationStations", []) if isinstance(stations_data, dict) else []
        if not isinstance(station_urls, list) or not station_urls:
            raise RuntimeError("No observation stations returned for point")

        station_url = str(station_urls[0]).strip()
        observation_data = fetch_json(f"{station_url}/observations/latest", timeout=10)
        properties = observation_data.get("properties", {}) if isinstance(observation_data, dict) else {}

        text_description = (properties.get("textDescription") or "").strip() or "Unknown"
        timestamp = properties.get("timestamp")

        today_short = None
        today_detailed = None
        if forecast_url:
            forecast_data = fetch_json(forecast_url, timeout=10)
            forecast_props = forecast_data.get("properties", {}) if isinstance(forecast_data, dict) else {}
            periods = forecast_props.get("periods", []) if isinstance(forecast_props, dict) else []
            if isinstance(periods, list) and periods:
                today_period = None
                for period in periods:
                    if not isinstance(period, dict):
                        continue
                    if period.get("isDaytime") is True:
                        today_period = period
                        break
                if today_period is None:
                    first_period = periods[0]
                    if isinstance(first_period, dict):
                        today_period = first_period

                if isinstance(today_period, dict):
                    today_short = (today_period.get("shortForecast") or "").strip() or None
                    today_detailed = (today_period.get("detailedForecast") or "").strip() or None

        payload = {
            "point": point_key,
            "text": text_description,
            "timestamp": timestamp,
            "station": properties.get("station"),
            "today_short": today_short,
            "today_detailed": today_detailed,
            "source": "nws",
            "stale": False
        }
        weather_current_cache[point_key] = {
            "cached_at": now,
            "payload": payload
        }
        return dict(payload), 200
    except RuntimeError as err:
        if cached:
            payload = dict(cached.get("payload", {}))
            payload["source"] = "cache-stale"
            payload["stale"] = True
            payload["error"] = str(err)
            return payload, 200
        return {
            "point": point_key,
            "text": None,
            "today_short": None,
            "today_detailed": None,
            "source": "nws-unavailable",
            "stale": False,
            "error": str(err)
        }, 502


def get_spectrogram_image(source_url, force_refresh=False, ttl_seconds=SPECTROGRAM_PROXY_CACHE_TTL_SECONDS, allow_insecure=False):
    if not is_allowed_remote_image_url(source_url):
        return None, "Unsupported spectrogram source URL", 400, False

    ttl = max(300, min(3600, ttl_seconds or SPECTROGRAM_PROXY_CACHE_TTL_SECONDS))
    key = str(source_url).strip()
    now = time.time()
    cached = spectrogram_cache.get(key)

    if cached and not force_refresh and (now - cached.get("cached_at", 0) <= ttl):
        return cached, None, 200, False

    insecure_used = False
    try:
        fetched = fetch_bytes(key, timeout=15, verify_ssl=True)
    except RuntimeError as err:
        should_try_insecure = (
            allow_insecure
            and is_allowed_insecure_image_host(key)
            and "CERTIFICATE_VERIFY_FAILED" in str(err)
        )
        if not should_try_insecure:
            if cached:
                stale_payload = dict(cached)
                stale_payload["stale"] = True
                stale_payload["insecure_tls"] = False
                return stale_payload, str(err), 200, True
            return None, str(err), 502, False

        try:
            fetched = fetch_bytes(key, timeout=15, verify_ssl=False)
            insecure_used = True
        except RuntimeError as insecure_err:
            if cached:
                stale_payload = dict(cached)
                stale_payload["stale"] = True
                stale_payload["insecure_tls"] = False
                return stale_payload, str(insecure_err), 200, True
            return None, str(insecure_err), 502, False

    payload = {
        "cached_at": now,
        "content_type": fetched.get("content_type") or "image/jpeg",
        "bytes": fetched.get("bytes") or b"",
        "stale": False,
        "insecure_tls": insecure_used
    }
    spectrogram_cache[key] = payload
    if len(spectrogram_cache) > 16:
        ordered = sorted(spectrogram_cache.items(), key=lambda item: item[1].get("cached_at", 0), reverse=True)
        spectrogram_cache.clear()
        spectrogram_cache.update(dict(ordered[:16]))
    return payload, None, 200, False


def compute_spectrogram_band_intensity(image_bytes, analysis=None):
    if not image_bytes:
        return None, "No image bytes"

    cfg = analysis if isinstance(analysis, dict) else {}
    band_min_hz = to_float(cfg.get("band_min_hz"))
    band_max_hz = to_float(cfg.get("band_max_hz"))
    top_hz = to_float(cfg.get("spectrogram_top_hz"))
    bottom_hz = to_float(cfg.get("spectrogram_bottom_hz"))
    left_ratio = to_float(cfg.get("plot_left_ratio"))
    right_ratio = to_float(cfg.get("plot_right_ratio"))
    top_ratio = to_float(cfg.get("plot_top_ratio"))
    bottom_ratio = to_float(cfg.get("plot_bottom_ratio"))

    band_min_hz = band_min_hz if band_min_hz is not None else 7.0
    band_max_hz = band_max_hz if band_max_hz is not None else 9.0
    top_hz = top_hz if top_hz is not None else 40.0
    bottom_hz = bottom_hz if bottom_hz is not None else 0.0
    left_ratio = clamp(left_ratio if left_ratio is not None else 0.039, 0.0, 0.4)
    right_ratio = clamp(right_ratio if right_ratio is not None else 0.81, 0.5, 1.0)
    top_ratio = clamp(top_ratio if top_ratio is not None else 0.072, 0.0, 0.4)
    bottom_ratio = clamp(bottom_ratio if bottom_ratio is not None else 0.935, 0.6, 1.0)

    if band_min_hz >= band_max_hz:
        return None, "Invalid analysis band"

    try:
        with Image.open(io_module.BytesIO(image_bytes)) as image:
            rgb = image.convert("RGB")
            pixels = np.asarray(rgb, dtype=np.float32)
    except Exception as err:
        return None, f"Decode failed: {err}"

    if pixels.ndim != 3 or pixels.shape[2] < 3:
        return None, "Unexpected image format"

    height, width = pixels.shape[0], pixels.shape[1]
    if width < 2 or height < 2:
        return None, "Image too small"

    left = max(0, min(width - 1, int(np.floor(width * left_ratio))))
    right = max(left + 1, min(width, int(np.ceil(width * right_ratio))))
    top = max(0, min(height - 1, int(np.floor(height * top_ratio))))
    bottom = max(top + 1, min(height, int(np.ceil(height * bottom_ratio))))

    if right - left < 2 or bottom - top < 2:
        return None, "Plot bounds too narrow"

    plot = pixels[top:bottom, left:right, :3]
    if plot.size == 0:
        return None, "Empty plot window"

    r = plot[:, :, 0]
    g = plot[:, :, 1]
    b = plot[:, :, 2]
    luminance = (0.2126 * r) + (0.7152 * g) + (0.0722 * b)

    y_count = luminance.shape[0]
    y_span = max(1, y_count - 1)
    max_plot_hz = max(top_hz, bottom_hz + 1.0)
    min_plot_hz = min(bottom_hz, max_plot_hz - 1.0)
    y_indices = np.arange(y_count, dtype=np.float32)
    y_ratio = np.clip(y_indices / y_span, 0.0, 1.0)
    hz_values = max_plot_hz - (max_plot_hz - min_plot_hz) * y_ratio
    band_mask = (hz_values >= band_min_hz) & (hz_values <= band_max_hz)

    if not np.any(band_mask):
        return None, "Band outside plot bounds"

    band_luminance = luminance[band_mask, :]
    if band_luminance.size == 0:
        return None, "No band pixels"

    band_avg = float(np.mean(band_luminance))
    plot_avg = float(np.mean(luminance))
    normalized = clamp(band_avg / 255.0, 0.0, 1.0)
    contrast_boost = clamp((band_avg - plot_avg) / 255.0, -1.0, 1.0)
    intensity = clamp(normalized + max(0.0, contrast_boost) * 0.35, 0.0, 1.0)
    return float(intensity), None


def get_goes_magnetometer_data(satellite="primary", window="1-day"):
    """Fetch real-time GOES magnetometer data from NOAA SWPC.
    GOES fields: He (East), Hp (Parallel), Hn (North), total (magnitude), arcjet_flag.
    """
    url = f"https://services.swpc.noaa.gov/json/goes/{satellite}/magnetometers-{window}.json"
    try:
        data = fetch_json(url, timeout=10)
        if not isinstance(data, list) or len(data) == 0:
            return None, "No magnetometer data returned"
        return data, None
    except RuntimeError as err:
        return None, str(err)


def load_schumann_sri_for_chart(hours=24):
    """Load recent SRI history from trend_history.json scoped to the last N hours."""
    try:
        if not os.path.exists(TREND_HISTORY_PATH):
            return []
        with open(TREND_HISTORY_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        cutoff_ms = (time.time() - hours * 3600) * 1000
        points = []
        for p in raw.get("schumann", []):
            if not isinstance(p, dict):
                continue
            ts = to_int(p.get("ts"))
            val = to_float(p.get("value"))
            if ts is None or val is None:
                continue
            if ts < cutoff_ms:
                continue
            points.append({"ts": ts, "value": val})
        points.sort(key=lambda p: p["ts"])
        return points
    except Exception:
        return []


def load_goes_series_for_chart(hours=24, component="Hn"):
    """Load recent GOES magnetometer points for charting in the last N hours."""
    cutoff_ms = (time.time() - max(1, to_float(hours) or 24) * 3600) * 1000

    mag_data, mag_err = get_goes_magnetometer_data("primary", "1-day")
    source = "primary"
    if mag_err or not mag_data:
        mag_data, mag_err = get_goes_magnetometer_data("secondary", "1-day")
        source = "secondary"

    if mag_err or not mag_data:
        return [], mag_err or "No GOES data", source

    points = []
    for entry in mag_data:
        if not isinstance(entry, dict) or entry.get("arcjet_flag"):
            continue
        val = to_float(entry.get(component))
        tag = str(entry.get("time_tag") or "").strip()
        if val is None or not tag:
            continue
        try:
            ts_ms = datetime.fromisoformat(tag.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            continue
        if ts_ms < cutoff_ms:
            continue
        points.append({
            "ts": int(ts_ms),
            "value": val
        })

    points.sort(key=lambda p: p["ts"])
    return points, None, source


def generate_spectrogram_from_magnetometer(mag_data, component="Hn"):
    """Single-series GOES chart — delegates to dual chart with no SRI data."""
    return generate_dual_geomagnetic_chart(mag_data, sri_points=[], component=component)


def generate_dual_geomagnetic_chart(mag_data, sri_points=None, component="Hn"):
    """Render a dual-series 24-hour line chart (JPEG) of GOES Hn + Schumann SRI.

    Left Y-axis: GOES Hn nanoTeslas (teal line).
    Right Y-axis: SRI units (amber line), drawn only when sri_points is non-empty.
    X-axis: shared 24-hour time window.
    """
    try:
        from PIL import Image as PilImage, ImageDraw as PilDraw

        if sri_points is None:
            sri_points = []

        # --- Extract GOES values ---
        goes_vals, goes_ts_ms = [], []
        now_ms = time.time() * 1000
        cutoff_ms = now_ms - 24 * 3600 * 1000
        for entry in (mag_data or []):
            if not isinstance(entry, dict) or entry.get("arcjet_flag"):
                continue
            val = to_float(entry.get(component))
            tag = entry.get("time_tag", "")
            if val is None or not tag:
                continue
            try:
                # Parse ISO timestamp to ms
                from datetime import datetime, timezone as tz
                dt = datetime.strptime(tag[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=tz.utc)
                ts_ms = dt.timestamp() * 1000
            except Exception:
                ts_ms = now_ms  # fallback: put at end
            if ts_ms >= cutoff_ms:
                goes_vals.append(val)
                goes_ts_ms.append(ts_ms)

        has_goes = len(goes_vals) >= 10
        has_sri = len(sri_points) >= 5

        if not has_goes and not has_sri:
            return None, "No data available for chart"

        # --- Chart layout ---
        W, H = 600, 220
        ML = 52   # left margin (GOES Y labels)
        MR = 52   # right margin (SRI Y labels, or 12 if no SRI)
        MT = 18   # top
        MB = 36   # bottom (time labels)
        if not has_sri:
            MR = 12
        PW = W - ML - MR
        PH = H - MT - MB

        # --- Shared X: 0..1 = cutoff_ms..now_ms ---
        x_span = max(now_ms - cutoff_ms, 1)

        def t_to_x(ts_ms_val):
            ratio = max(0.0, min(1.0, (ts_ms_val - cutoff_ms) / x_span))
            return ML + int(ratio * PW)

        def norm_y(v, vlo, vhi, top, bottom):
            ratio = (v - vlo) / max(vhi - vlo, 1e-9)
            return bottom - int(ratio * (bottom - top))

        # --- Y ranges ---
        gmin, gmax = 0.0, 0.0
        if has_goes:
            g_arr = np.array(goes_vals, dtype=np.float64)
            gmin, gmax = float(g_arr.min()), float(g_arr.max())
            gpad = max((gmax - gmin) * 0.12, 0.5)
            g_lo, g_hi = gmin - gpad, gmax + gpad
        else:
            g_lo, g_hi = -1, 1

        smin, smax = 0.0, 0.0
        s_lo, s_hi = -1.0, 1.0
        sri_vals_arr = np.array([], dtype=np.float64)
        sri_ts_arr = np.array([], dtype=np.float64)
        if has_sri:
            sri_vals_arr = np.array([to_float(p["value"]) for p in sri_points], dtype=np.float64)
            sri_ts_arr = np.array([to_int(p["ts"]) for p in sri_points], dtype=np.float64)
            smin, smax = float(sri_vals_arr.min()), float(sri_vals_arr.max())
            spad = max((smax - smin) * 0.12, 0.5)
            s_lo, s_hi = smin - spad, smax + spad

        # --- Colors ---
        BG      = (22, 24, 32)
        GRID    = (42, 48, 62)
        GOES_C  = (0, 210, 180)      # teal
        SRI_C   = (255, 185, 50)     # amber
        ZERO_C  = (65, 75, 95)
        TEXT    = (150, 162, 178)
        HDR     = (200, 210, 220)

        img = PilImage.new("RGB", (W, H), BG)
        draw = PilDraw.Draw(img)

        top_px, bot_px = MT, MT + PH

        # --- Horizontal grid + GOES Y labels (left) ---
        n_yticks = 5
        for i in range(n_yticks + 1):
            frac = i / n_yticks
            y = bot_px - int(frac * PH)
            draw.line([(ML, y), (ML + PW, y)], fill=GRID, width=1)
            if has_goes:
                lv = g_lo + (g_hi - g_lo) * frac
                lbl = f"{lv:+.1f}" if abs(lv) >= 0.05 else "0"
                draw.text((2, y - 6), lbl, fill=GOES_C if has_goes else TEXT)

        # SRI Y labels (right axis)
        if has_sri:
            for i in range(n_yticks + 1):
                frac = i / n_yticks
                sv = s_lo + (s_hi - s_lo) * frac
                y = bot_px - int(frac * PH)
                lbl = f"{sv:.1f}"
                draw.text((ML + PW + 3, y - 6), lbl, fill=SRI_C)

        # Zero line for GOES if crosses zero
        if has_goes and g_lo < 0 < g_hi:
            zy = norm_y(0, g_lo, g_hi, top_px, bot_px)
            draw.line([(ML, zy), (ML + PW, zy)], fill=ZERO_C, width=1)

        # --- Vertical grid + time labels every 6h ---
        for h_offset in range(0, 25, 6):
            ts_mark = cutoff_ms + h_offset * 3600 * 1000
            xm = t_to_x(ts_mark)
            draw.line([(xm, MT), (xm, bot_px)], fill=GRID, width=1)
            lbl = f"-{24 - h_offset}h" if h_offset < 24 else "Now"
            draw.text((xm - 10, bot_px + 4), lbl, fill=TEXT)

        # --- Plot GOES line ---
        if has_goes:
            pts = [
                (t_to_x(ts), norm_y(v, g_lo, g_hi, top_px, bot_px))
                for ts, v in zip(goes_ts_ms, goes_vals)
            ]
            if len(pts) > 1:
                draw.line(pts, fill=GOES_C, width=2)

        # --- Plot SRI line ---
        if has_sri:
            sri_pts = [
                (t_to_x(float(ts)), norm_y(float(v), s_lo, s_hi, top_px, bot_px))
                for ts, v in zip(sri_ts_arr, sri_vals_arr)
                if float(ts) >= cutoff_ms
            ]
            if len(sri_pts) > 1:
                draw.line(sri_pts, fill=SRI_C, width=2)

        # --- Border ---
        draw.rectangle([ML, MT, ML + PW, bot_px], outline=GRID, width=1)

        # --- Header ---
        header_parts = []
        if has_goes:
            header_parts.append(f"GOES Hn nT  min {gmin:+.1f}  max {gmax:+.1f}")
        if has_sri:
            header_parts.append(f"SRI  min {smin:.1f}  max {smax:.1f}")
        draw.text((ML, 2), "  |  ".join(header_parts), fill=HDR)

        # --- Legend dots ---
        if has_goes:
            draw.ellipse([(ML, 2), (ML + 6, 8)], fill=GOES_C)
        if has_sri:
            lx = ML + (80 if has_goes else 0)
            draw.ellipse([(lx, 2), (lx + 6, 8)], fill=SRI_C)

        output = io_module.BytesIO()
        img.save(output, format="JPEG", quality=88)
        return output.getvalue(), None

    except Exception as err:
        return None, f"Chart generation failed: {err}"


def get_generated_spectrogram(force_refresh=False, ttl_seconds=SPECTROGRAM_GENERATED_CACHE_TTL_SECONDS):
    """Fetch NOAA GOES magnetometer data and generate spectrogram, with primary/secondary fallback."""
    now = time.time()
    if (generated_spectrogram_cache.get("payload")
            and not force_refresh
            and (now - generated_spectrogram_cache.get("cached_at", 0) <= ttl_seconds)):
        return generated_spectrogram_cache["payload"], None, 200

    # Try primary GOES satellite first, fall back to secondary
    mag_data, error = get_goes_magnetometer_data("primary", "1-day")
    if error:
        mag_data, error = get_goes_magnetometer_data("secondary", "1-day")

    if error or not mag_data:
        if generated_spectrogram_cache.get("payload"):
            stale = dict(generated_spectrogram_cache["payload"])
            stale["stale"] = True
            return stale, error or "No data available", 200
        return None, error or "Unable to fetch magnetometer data", 502

    spec_bytes, gen_error = generate_spectrogram_from_magnetometer(mag_data, component="Hn")

    if gen_error or not spec_bytes:
        if generated_spectrogram_cache.get("payload"):
            stale = dict(generated_spectrogram_cache["payload"])
            stale["stale"] = True
            return stale, gen_error or "Generation failed", 200
        return None, gen_error or "Failed to generate spectrogram", 502

    payload = {
        "cached_at": now,
        "bytes": spec_bytes,
        "content_type": "image/jpeg",
        "stale": False,
        "source": "noaa_goes",
        "component": "Hn"
    }
    generated_spectrogram_cache["cached_at"] = now
    generated_spectrogram_cache["payload"] = payload
    return payload, None, 200


def normalize_history_points(raw_points, max_points=5000):
    points = []
    if not isinstance(raw_points, list):
        return points

    for entry in raw_points[-max_points:]:
        if not isinstance(entry, dict):
            continue
        ts = to_int(entry.get("ts"))
        val = to_float(entry.get("value"))
        if ts is None or val is None:
            continue
        points.append({"ts": ts, "value": val})
    return points


def make_history_payload(raw):
    if not isinstance(raw, dict):
        raw = {}

    return {
        "saved_at": to_int(raw.get("saved_at")) or int(time.time() * 1000),
        "kp": normalize_history_points(raw.get("kp"), max_points=10000),
        "schumann": normalize_history_points(raw.get("schumann"), max_points=10000),
        "solar_wind": normalize_history_points(raw.get("solar_wind"), max_points=10000),
        "elf_observations": normalize_history_points(raw.get("elf_observations"), max_points=10000),
    }


def load_trend_history_file():
    if not os.path.exists(TREND_HISTORY_PATH):
        return make_history_payload({})

    try:
        with open(TREND_HISTORY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return make_history_payload({})

    return make_history_payload(data)


def save_trend_history_file(payload):
    ensure_data_dir()
    with open(TREND_HISTORY_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=True, separators=(",", ":"))


def normalize_correlation_event(raw_event, fallback_index=0):
    if not isinstance(raw_event, dict):
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    event = dict(raw_event)

    event_id = str(event.get("id") or "").strip()
    if not event_id:
        event_id = f"corr-{int(time.time() * 1000)}-{fallback_index}"

    timestamp = event.get("timestamp")
    if parse_time_millis(timestamp) <= 0:
        timestamp = now_iso

    event["id"] = event_id
    event["timestamp"] = timestamp
    event["ingested_at"] = now_iso
    return event


def append_correlation_events(events):
    if not events:
        return
    ensure_data_dir()
    with open(CORRELATION_EVENTS_PATH, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")))
            fh.write("\n")


def load_correlation_events(start_ms=None, end_ms=None, limit=500):
    if not os.path.exists(CORRELATION_EVENTS_PATH):
        return []

    events = []
    try:
        with open(CORRELATION_EVENTS_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_ms = parse_time_millis(event.get("timestamp"))
                if start_ms is not None and event_ms and event_ms < start_ms:
                    continue
                if end_ms is not None and event_ms and event_ms > end_ms:
                    continue
                events.append(event)
    except OSError:
        return []

    limit_safe = max(1, min(limit or 500, 5000))
    if len(events) > limit_safe:
        events = events[-limit_safe:]
    return events


def latest_noaa_kp_value():
    url = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
    data = fetch_json(url, timeout=8)

    if not isinstance(data, list):
        raise RuntimeError("NOAA Kp payload invalid")

    for row in reversed(data):
        if isinstance(row, list) and len(row) >= 2:
            parsed = to_float(row[1])
            if parsed is not None:
                return parsed, str(row[0])
        elif isinstance(row, dict):
            parsed = to_float(row.get("Kp"))
            if parsed is not None:
                return parsed, str(row.get("time_tag") or "")

    raise RuntimeError("NOAA Kp payload missing numeric value")


def latest_noaa_plasma_density():
    url = "https://services.swpc.noaa.gov/products/solar-wind/plasma-7-day.json"
    data = fetch_json(url, timeout=8)

    if not isinstance(data, list):
        raise RuntimeError("NOAA plasma payload invalid")

    for row in reversed(data):
        if isinstance(row, list) and len(row) >= 2:
            parsed = to_float(row[1])
            if parsed is not None:
                return parsed, str(row[0])

    raise RuntimeError("NOAA plasma payload missing numeric value")


def latest_noaa_xray_flux():
    url = "https://services.swpc.noaa.gov/json/goes/primary/xrays-7-day.json"
    data = fetch_json(url, timeout=8)

    if not isinstance(data, list):
        raise RuntimeError("NOAA X-ray payload invalid")

    best = None
    for row in reversed(data):
        if not isinstance(row, dict):
            continue
        # NOAA returns both channels; short-wave (0.1-0.8nm) is most used for flare class.
        if str(row.get("energy", "")).strip() != "0.1-0.8nm":
            continue
        parsed = to_float(row.get("flux"))
        if parsed is not None:
            best = (parsed, str(row.get("time_tag") or ""))
            break

    if best:
        return best
    raise RuntimeError("NOAA X-ray payload missing numeric value")


def latest_gfz_kp_value():
    url = "https://kp.gfz.de/app/files/Kp_ap_Ap_SN_F107_nowcast.txt"
    req = Request(url, headers={
        "User-Agent": NWS_USER_AGENT,
        "Accept": "text/plain, */*;q=0.8"
    })

    try:
        with urlopen(req, timeout=10) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except HTTPError as err:
        raise RuntimeError(f"GFZ HTTP {err.code}") from err
    except URLError as err:
        raise RuntimeError(f"GFZ network error: {err.reason}") from err

    rows = []
    for line in payload.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 15:
            continue
        rows.append(parts)

    if not rows:
        raise RuntimeError("GFZ Kp payload missing data rows")

    # Each row contains Kp1..Kp8 (3-hour bins) for one UTC day.
    # Prefer the latest non-negative Kp bin in the most recent day.
    for parts in reversed(rows):
        year = to_int(parts[0])
        month = to_int(parts[1])
        day = to_int(parts[2])
        if not (year and month and day):
            continue

        kp_bins = []
        for idx in range(8):
            kp_value = to_float(parts[7 + idx])
            kp_bins.append(kp_value)

        for idx in range(7, -1, -1):
            kp_value = kp_bins[idx]
            if kp_value is None or kp_value < 0:
                continue
            hour = idx * 3
            observed_at = datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc).isoformat()
            return kp_value, observed_at

    raise RuntimeError("GFZ Kp payload missing numeric value")


def normalize_observed_time(text):
    if not text:
        return datetime.now(timezone.utc).isoformat()

    raw = str(text).strip().replace(" ", "T")
    if raw.endswith("Z"):
        return raw
    if "+" in raw[10:] or raw.endswith("00:00"):
        return raw

    # NOAA/GFZ timestamps are UTC but often lack explicit timezone.
    return f"{raw}Z"


def get_solar_composite(force_refresh=False):
    now = time.time()
    cached = solar_cache.get("payload")
    if (not force_refresh) and cached and (now - solar_cache.get("cached_at", 0) <= SOLAR_COMPOSITE_CACHE_TTL_SECONDS):
        return dict(cached)

    errors = []

    kp_value = None
    kp_time = ""
    kp_source = ""
    fallback_used = False

    try:
        kp_value, kp_time = latest_noaa_kp_value()
        kp_source = "noaa-swpc-kp"
    except RuntimeError as err:
        errors.append(str(err))
        try:
            kp_value, kp_time = latest_gfz_kp_value()
            kp_source = "gfz-kp-nowcast"
            fallback_used = True
        except RuntimeError as gfz_err:
            errors.append(str(gfz_err))

    if kp_value is None:
        raise RuntimeError("Unable to fetch Kp from NOAA or GFZ")

    plasma_density = None
    plasma_time = ""
    try:
        plasma_density, plasma_time = latest_noaa_plasma_density()
    except RuntimeError as err:
        errors.append(str(err))

    xray_flux = None
    xray_time = ""
    try:
        xray_flux, xray_time = latest_noaa_xray_flux()
    except RuntimeError as err:
        errors.append(str(err))

    payload = {
        "kp": round(float(kp_value), 2),
        "observed_at": normalize_observed_time(kp_time),
        "kp_source": kp_source,
        "fallback_used": fallback_used,
        "plasma_density": None if plasma_density is None else round(float(plasma_density), 2),
        "plasma_observed_at": normalize_observed_time(plasma_time) if plasma_time else "",
        "xray_flux": None if xray_flux is None else float(xray_flux),
        "xray_observed_at": normalize_observed_time(xray_time) if xray_time else "",
        "component_errors": errors,
        "fetched_at": datetime.now(timezone.utc).isoformat()
    }

    solar_cache["cached_at"] = now
    solar_cache["payload"] = payload
    return dict(payload)


def derive_schumann_response():
    now = time.time()
    cached = schumann_cache.get("payload")
    if cached and (now - schumann_cache.get("cached_at", 0) <= SCHUMANN_DERIVED_TTL_SECONDS):
        return dict(cached)

    components = {}
    component_errors = []
    observed_times = []

    try:
        kp_value, kp_time = latest_noaa_kp_value()
        kp_component = clamp((kp_value / 9.0) * 30.0, 0.0, 30.0)
        components["kp"] = {
            "raw": kp_value,
            "weighted": round(kp_component, 2),
            "scale": "0-30",
            "observed_at": kp_time,
            "source": "NOAA SWPC planetary K-index"
        }
        if kp_time:
            observed_times.append(kp_time)
    except RuntimeError as err:
        component_errors.append(str(err))

    try:
        plasma_density, plasma_time = latest_noaa_plasma_density()
        plasma_component = clamp((plasma_density / 40.0) * 15.0, 0.0, 15.0)
        components["plasma_density"] = {
            "raw": plasma_density,
            "weighted": round(plasma_component, 2),
            "scale": "0-15",
            "observed_at": plasma_time,
            "source": "NOAA SWPC solar-wind plasma"
        }
        if plasma_time:
            observed_times.append(plasma_time)
    except RuntimeError as err:
        component_errors.append(str(err))

    try:
        xray_flux, xray_time = latest_noaa_xray_flux()
        # Typical operational X-ray range spans about 1e-8 to 1e-4 W/m^2.
        xray_normalized = (clamp(xray_flux, 1e-8, 1e-4) - 1e-8) / (1e-4 - 1e-8)
        xray_component = clamp(xray_normalized * 15.0, 0.0, 15.0)
        components["xray_flux"] = {
            "raw": xray_flux,
            "weighted": round(xray_component, 2),
            "scale": "0-15",
            "observed_at": xray_time,
            "source": "NOAA GOES primary X-ray"
        }
        if xray_time:
            observed_times.append(xray_time)
    except RuntimeError as err:
        component_errors.append(str(err))

    # Lightning density as atmospheric driver (new component)
    try:
        lightning_data = get_lightning_data(use_cache=True)
        lightning_component = clamp(lightning_data.get("density_score", 0), 0.0, 15.0)
        components["lightning_density"] = {
            "raw_strike_count_15m": lightning_data.get("strike_count_15m", 0),
            "raw_avg_current_ka": lightning_data.get("avg_peak_current_ka", 0),
            "weighted": round(lightning_component, 2),
            "scale": "0-15",
            "observed_at": lightning_data.get("timestamp"),
            "source": f"Lightning ({lightning_data.get('source', 'unknown')})",
            "flash_rate_per_min": lightning_data.get("flash_rate_per_min", 0)
        }
        if lightning_data.get("timestamp"):
            observed_times.append(lightning_data["timestamp"])
    except Exception as err:
        component_errors.append(f"Lightning fetch error: {str(err)}")

    if not components:
        raise RuntimeError("Unable to derive Schumann response from upstream feeds")

    derived_value = round(sum(item["weighted"] for item in components.values()), 2)
    observed_at = max(observed_times) if observed_times else datetime.now(timezone.utc).isoformat()

    payload = {
        "value": derived_value,
        "unit": "SRI",
        "source": "derived-space-weather",
        "observed_at": observed_at,
        "mode": "derived",
        "components": components,
        "notes": "Derived index from NOAA Kp, solar-wind density, and GOES X-ray flux.",
        "component_errors": component_errors
    }

    schumann_cache["cached_at"] = now
    schumann_cache["payload"] = payload
    return dict(payload)


def iter_configured_schumann_urls():
    single = os.environ.get("SCHUMANN_API_URL", "").strip()
    many = os.environ.get("SCHUMANN_API_URLS", "").strip()

    urls = []
    if many:
        urls.extend([u.strip() for u in many.split(",") if u.strip()])
    if single:
        urls.insert(0, single)

    deduped = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


@app.route("/system")
def system():
    global _net_prev
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/').percent

    mem_used_gb = round(mem.used / 1024 ** 3, 1)
    mem_total_gb = round(mem.total / 1024 ** 3, 1)
    mem_available_gb = round(mem.available / 1024 ** 3, 1)

    # Network Rx/Tx rate — delta between calls
    net_now = psutil.net_io_counters()
    now_ts = time.time()
    delta_t = now_ts - _net_prev["ts"]
    if delta_t > 0 and _net_prev["ts"] > 0:
        rx_kbps = round((net_now.bytes_recv - _net_prev["bytes_recv"]) / delta_t / 1024, 1)
        tx_kbps = round((net_now.bytes_sent - _net_prev["bytes_sent"]) / delta_t / 1024, 1)
    else:
        rx_kbps = None
        tx_kbps = None
    _net_prev["bytes_sent"] = net_now.bytes_sent
    _net_prev["bytes_recv"] = net_now.bytes_recv
    _net_prev["ts"] = now_ts

    # WiFi signal — parse /proc/net/wireless (Linux)
    wifi_signal_dbm = None
    wifi_iface = None
    try:
        with open("/proc/net/wireless", "r") as _f:
            _lines = _f.readlines()
        for _line in _lines[2:]:
            _parts = _line.split()
            if len(_parts) >= 4:
                _iface = _parts[0].rstrip(":")
                _level = float(_parts[3].rstrip("."))
                if _level > 0:
                    _level -= 256  # old kernel format: raw byte → dBm
                if _level < 0:
                    wifi_signal_dbm = int(_level)
                    wifi_iface = _iface
                    break
    except Exception:
        pass

    # CPU temperature — optional, omit if sensor unavailable; convert C to F
    cpu_temp_f = None
    try:
        _temps = psutil.sensors_temperatures()
        for _key in ("coretemp", "k10temp", "cpu_thermal", "cpu-thermal", "acpitz"):
            if _key in _temps and _temps[_key]:
                _c = _temps[_key][0].current
                cpu_temp_f = round(_c * 9 / 5 + 32, 1)
                break
        if cpu_temp_f is None and _temps:
            _first = next(iter(_temps.values()))
            if _first:
                _c = _first[0].current
                cpu_temp_f = round(_c * 9 / 5 + 32, 1)
    except Exception:
        pass

    return jsonify({
        "cpu": cpu,
        "memory": mem.percent,
        "memory_used_gb": mem_used_gb,
        "memory_total_gb": mem_total_gb,
        "memory_available_gb": mem_available_gb,
        "disk": disk,
        "uptime": get_uptime(),
        "net_rx_kbps": rx_kbps,
        "net_tx_kbps": tx_kbps,
        "wifi_signal_dbm": wifi_signal_dbm,
        "wifi_iface": wifi_iface,
        "cpu_temp_f": cpu_temp_f,
    })


@app.route("/neos")
def neos():
    target_date = request.args.get("date") or date.today().isoformat()
    api_key = os.environ.get("NASA_API_KEY", "DEMO_KEY")

    query = urlencode({
        "start_date": target_date,
        "end_date": target_date,
        "api_key": api_key
    })
    url = f"https://api.nasa.gov/neo/rest/v1/feed?{query}"

    try:
        data = fetch_json(url, timeout=10)
    except RuntimeError as err:
        cached = neo_cache.get(target_date)
        if cached and (time.time() - cached.get("cached_at", 0) <= NEO_CACHE_TTL_SECONDS):
            stale_payload = dict(cached.get("payload", {}))
            stale_payload["stale"] = True
            stale_payload["source"] = "cache"
            stale_payload["error"] = str(err)
            return jsonify(stale_payload), 200

        return jsonify({
            "date": target_date,
            "asteroids": [],
            "count": 0,
            "source": "live-unavailable",
            "stale": False,
            "error": str(err)
        }), 200

    near_earth = data.get("near_earth_objects", {})
    daily = near_earth.get(target_date, []) if isinstance(near_earth, dict) else []

    asteroids = []
    for item in daily:
        if not isinstance(item, dict):
            continue

        approach = item.get("close_approach_data", [])
        first_approach = approach[0] if isinstance(approach, list) and approach else {}

        miss_distance = first_approach.get("miss_distance", {}) if isinstance(first_approach, dict) else {}
        velocity = first_approach.get("relative_velocity", {}) if isinstance(first_approach, dict) else {}
        est_diameter = item.get("estimated_diameter", {}).get("meters", {})

        miss_km = to_float(miss_distance.get("kilometers"))
        miss_ld = to_float(miss_distance.get("lunar"))
        velocity_kph = to_float(velocity.get("kilometers_per_hour"))
        min_m = to_float(est_diameter.get("estimated_diameter_min"))
        max_m = to_float(est_diameter.get("estimated_diameter_max"))

        asteroids.append({
            "name": item.get("name", "Unnamed object"),
            "hazardous": bool(item.get("is_potentially_hazardous_asteroid", False)),
            "url": item.get("nasa_jpl_url", ""),
            "miss_distance_km": miss_km,
            "miss_distance_lunar": miss_ld,
            "velocity_kph": velocity_kph,
            "size_min_m": min_m,
            "size_max_m": max_m,
            "approach_time": first_approach.get("close_approach_date_full") if isinstance(first_approach, dict) else None,
            "proximity_class": classify_proximity(miss_km)
        })

    asteroids.sort(key=lambda a: a.get("miss_distance_km") if a.get("miss_distance_km") is not None else float("inf"))

    payload = {
        "date": target_date,
        "asteroids": asteroids,
        "count": len(asteroids),
        "source": "live",
        "stale": False
    }

    neo_cache[target_date] = {
        "cached_at": time.time(),
        "payload": payload
    }

    return jsonify(payload)


@app.route("/tsunami-alerts")
def tsunami_alerts():
    now = time.time()
    if tsunami_cache.get("payload") and (now - tsunami_cache.get("cached_at", 0) <= TSUNAMI_CACHE_TTL_SECONDS):
        return jsonify(tsunami_cache["payload"])

    url = "https://api.weather.gov/alerts/active?status=actual&message_type=alert"

    try:
        data = fetch_json(url, timeout=10)
    except RuntimeError as err:
        return jsonify({"error": str(err), "features": []}), 502

    features = data.get("features", []) if isinstance(data, dict) else []
    if not isinstance(features, list):
        features = []

    tsunami_features = [f for f in features if is_critical_tsunami_feature(f)]
    tsunami_features = sort_tsunami_features(tsunami_features)[:20]

    payload = {
        "updated": data.get("updated") if isinstance(data, dict) else None,
        "features": tsunami_features,
        "count": len(tsunami_features),
        "source": "nws"
    }
    tsunami_cache["cached_at"] = now
    tsunami_cache["payload"] = payload
    return jsonify(payload)


@app.route("/weather-alerts")
def weather_alerts():
    area = (request.args.get("area") or "").strip() or None
    point = (request.args.get("point") or "").strip() or None

    if area and point:
        return jsonify({
            "error": "Specify either area or point, not both",
            "features": [],
            "count": 0,
            "stale": False,
            "source": "invalid-request"
        }), 400

    payload, status_code = get_weather_alerts(area=area, point=point)
    return jsonify(payload), status_code


@app.route("/weather-current")
def weather_current():
    point = (request.args.get("point") or "").strip()
    payload, status_code = get_weather_current(point)
    return jsonify(payload), status_code


@app.route("/spectrogram-proxy")
def spectrogram_proxy():
    source = (request.args.get("source") or "").strip()
    force_raw = normalize_text(request.args.get("force") or "0")
    force_refresh = force_raw in {"1", "true", "yes", "on"}
    insecure_raw = normalize_text(request.args.get("insecure") or "0")
    allow_insecure = insecure_raw in {"1", "true", "yes", "on"}
    ttl = to_int(request.args.get("ttl")) or SPECTROGRAM_PROXY_CACHE_TTL_SECONDS

    payload, error, status_code, stale = get_spectrogram_image(
        source,
        force_refresh=force_refresh,
        ttl_seconds=ttl,
        allow_insecure=allow_insecure
    )
    if not payload:
        return jsonify({"error": error or "Unable to fetch spectrogram image", "source": source}), status_code

    response = Response(payload.get("bytes") or b"", mimetype=payload.get("content_type") or "image/jpeg")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Spectrogram-Source"] = source
    response.headers["X-Spectrogram-Stale"] = "1" if stale else "0"
    response.headers["X-Spectrogram-Insecure-TLS"] = "1" if payload.get("insecure_tls") else "0"
    if error:
        response.headers["X-Spectrogram-Error"] = error
    return response


@app.route("/spectrogram-generated")
def spectrogram_generated():
    """Backward-compatible single-series GOES chart endpoint."""
    force_raw = normalize_text(request.args.get("force") or "0")
    force_refresh = force_raw in {"1", "true", "yes", "on"}
    ttl = to_int(request.args.get("ttl")) or SPECTROGRAM_GENERATED_CACHE_TTL_SECONDS

    payload, error, status_code = get_generated_spectrogram(force_refresh=force_refresh, ttl_seconds=ttl)
    if not payload:
        return jsonify({"error": error or "Unable to generate spectrogram"}), status_code

    response = Response(payload.get("bytes") or b"", mimetype=payload.get("content_type") or "image/jpeg")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Spectrogram-Source"] = payload.get("source", "noaa_goes")
    response.headers["X-Spectrogram-Stale"] = "1" if payload.get("stale") else "0"
    if error:
        response.headers["X-Spectrogram-Error"] = error
    return response


@app.route("/spectrogram-intensity")
def spectrogram_intensity():
    source = (request.args.get("source") or "").strip()
    force_raw = normalize_text(request.args.get("force") or "0")
    force_refresh = force_raw in {"1", "true", "yes", "on"}
    insecure_raw = normalize_text(request.args.get("insecure") or "0")
    allow_insecure = insecure_raw in {"1", "true", "yes", "on"}
    ttl = to_int(request.args.get("ttl")) or SPECTROGRAM_PROXY_CACHE_TTL_SECONDS

    analysis = {
        "band_min_hz": request.args.get("band_min_hz"),
        "band_max_hz": request.args.get("band_max_hz"),
        "spectrogram_top_hz": request.args.get("spectrogram_top_hz"),
        "spectrogram_bottom_hz": request.args.get("spectrogram_bottom_hz"),
        "plot_left_ratio": request.args.get("plot_left_ratio"),
        "plot_right_ratio": request.args.get("plot_right_ratio"),
        "plot_top_ratio": request.args.get("plot_top_ratio"),
        "plot_bottom_ratio": request.args.get("plot_bottom_ratio")
    }

    payload, error, status_code, stale = get_spectrogram_image(
        source,
        force_refresh=force_refresh,
        ttl_seconds=ttl,
        allow_insecure=allow_insecure
    )
    if not payload:
        return jsonify({
            "ok": False,
            "error": error or "Unable to fetch spectrogram image",
            "source": source
        }), status_code

    intensity, intensity_error = compute_spectrogram_band_intensity(payload.get("bytes") or b"", analysis=analysis)
    if intensity is None:
        return jsonify({
            "ok": False,
            "error": intensity_error or "Unable to derive intensity",
            "source": source,
            "stale": bool(stale),
            "insecure_tls": bool(payload.get("insecure_tls"))
        }), 200

    return jsonify({
        "ok": True,
        "intensity": intensity,
        "source": source,
        "stale": bool(stale),
        "insecure_tls": bool(payload.get("insecure_tls")),
        "observed_at": datetime.now(timezone.utc).isoformat()
    })


geomagnetic_chart_cache = {"cached_at": 0, "payload": None}
GEOMAGNETIC_CHART_CACHE_TTL_SECONDS = 300


@app.route("/geomagnetic-chart")
def geomagnetic_chart():
    """Dual-series 24-hour chart: NOAA GOES Hn (nT, teal) + Schumann SRI (amber)."""
    force_raw = normalize_text(request.args.get("force") or "0")
    force_refresh = force_raw in {"1", "true", "yes", "on"}
    now = time.time()

    if (geomagnetic_chart_cache.get("payload")
            and not force_refresh
            and (now - geomagnetic_chart_cache.get("cached_at", 0) <= GEOMAGNETIC_CHART_CACHE_TTL_SECONDS)):
        cached = geomagnetic_chart_cache["payload"]
        resp = Response(cached["bytes"], mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Chart-Source"] = "cache"
        return resp

    # Fetch GOES magnetometer data (primary → secondary fallback)
    mag_data, mag_err = get_goes_magnetometer_data("primary", "1-day")
    if mag_err:
        mag_data, mag_err = get_goes_magnetometer_data("secondary", "1-day")

    # Load stored SRI trend history
    sri_points = load_schumann_sri_for_chart(hours=24)

    if not mag_data and not sri_points:
        if geomagnetic_chart_cache.get("payload"):
            cached = geomagnetic_chart_cache["payload"]
            resp = Response(cached["bytes"], mimetype="image/jpeg")
            resp.headers["X-Chart-Stale"] = "1"
            return resp
        return jsonify({"error": mag_err or "No data available for chart"}), 502

    chart_bytes, chart_err = generate_dual_geomagnetic_chart(mag_data or [], sri_points)

    if chart_err or not chart_bytes:
        if geomagnetic_chart_cache.get("payload"):
            cached = geomagnetic_chart_cache["payload"]
            resp = Response(cached["bytes"], mimetype="image/jpeg")
            resp.headers["X-Chart-Stale"] = "1"
            return resp
        return jsonify({"error": chart_err or "Failed to render chart"}), 502

    geomagnetic_chart_cache["cached_at"] = now
    geomagnetic_chart_cache["payload"] = {"bytes": chart_bytes}

    resp = Response(chart_bytes, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Chart-Source"] = "live"
    resp.headers["X-Chart-Sri-Points"] = str(len(sri_points))
    resp.headers["X-Chart-Goes-Points"] = str(len(mag_data) if mag_data else 0)
    return resp


@app.route("/geomagnetic-series")
def geomagnetic_series():
    """JSON timeseries for frontend composite chart (GOES Hn)."""
    hours = to_int(request.args.get("hours")) or 24
    component_raw = (request.args.get("component") or "Hn").strip()
    component = {
        "hn": "Hn",
        "hp": "Hp",
        "he": "He",
        "total": "total"
    }.get(component_raw.lower(), "Hn")

    points, error, source = load_goes_series_for_chart(hours=hours, component=component)
    status = 200
    return jsonify({
        "component": component,
        "hours": hours,
        "source": source,
        "count": len(points),
        "error": error,
        "points": points
    }), status


@app.route("/schumann-response")
def schumann_response():
    upstream_urls = iter_configured_schumann_urls()
    value_path = os.environ.get("SCHUMANN_VALUE_PATH", "").strip() or None
    derived_enabled = normalize_text(os.environ.get("SCHUMANN_DERIVED_ENABLED", "1")) not in {"0", "false", "off", "no"}
    upstream_error = None
    attempted_sources = []

    for upstream in upstream_urls:
        attempted_sources.append(upstream)
        try:
            data = fetch_json(upstream, timeout=8)
            parsed = adapt_schumann_payload(data, source_hint=upstream, value_path=value_path)
            if parsed:
                parsed["mode"] = "live"
                parsed["attempted_sources"] = attempted_sources
                return jsonify(parsed)
            upstream_error = "Upstream payload did not contain a usable numeric Schumann value"
        except RuntimeError as err:
            upstream_error = str(err)

    if derived_enabled:
        try:
            derived = derive_schumann_response()
            derived["attempted_sources"] = attempted_sources
            if upstream_error:
                derived["upstream_error"] = upstream_error
            return jsonify(derived)
        except RuntimeError as err:
            upstream_error = str(err)

    if os.path.exists(LOCAL_SCHUMANN_PATH):
        try:
            with open(LOCAL_SCHUMANN_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            parsed = adapt_schumann_payload(data, source_hint="local-file", value_path=value_path)
            if parsed:
                parsed["mode"] = "local-file"
                parsed["attempted_sources"] = attempted_sources
                if upstream_error:
                    parsed["upstream_error"] = upstream_error
                return jsonify(parsed)
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "Invalid local Schumann file format"}), 500

    return jsonify({
        "error": "Schumann source unavailable",
        "hint": "Set SCHUMANN_API_URL(S), keep SCHUMANN_DERIVED_ENABLED=1, or provide data/schumann_response.json",
        "attempted_sources": attempted_sources,
        "upstream_error": upstream_error
    }), 503


@app.route("/coherence-anomaly-index")
def coherence_anomaly_index():
    """
    Compute Coherence Anomaly Index (CAI) — multi-domain anomaly convergence metric.
    
    Returns 0-100 score reflecting cross-domain signal agreement.
    States: baseline, watch, elevated, anomalous
    """
    try:
        # Fetch current state from all sources
        sri_data = derive_schumann_response()
        sri_value = sri_data.get("value", 0)
        
        # Extract component values for baseline tracking
        kp = sri_data.get("components", {}).get("kp", {}).get("raw", 0)
        lightning_density = sri_data.get("components", {}).get("lightning_density", {}).get("weighted", 0)
        
        # Prepare CAI context (partial — full version needs live Schumann measurements)
        cai_ctx = {
            "sri_value": sri_value,
            "sri_z": 0.5,  # Would come from baseline tracker in production
            "kp": kp,
            "plasma": sri_data.get("components", {}).get("plasma_density", {}).get("raw", 0),
            "xray": sri_data.get("components", {}).get("xray_flux", {}).get("raw", 1e-7),
            "schumann_intensity": 0.5,  # Would come from live spectrogram
            "schumann_baseline": 0.4,
            "schumann_deviation": 0.25,  # Would be computed from actual data
            "spectrogram_available": False,  # Flag for data availability
            "spectrogram_health": 0.7,
            "lightning_density_score": lightning_density,
            "geo_mag_local_delta": 0.0,
            "infrasound_delta": 0.0,
            "pressure_anomaly": 0.0,
            "optical_flash": 0.0,
            "neo_score": 0,
            "data_freshness_score": 0.9,
        }
        
        # Compute CAI
        cai = compute_cai(cai_ctx, update_baselines=True)
        
        return jsonify({
            "cai": cai,
            "sri_snapshot": {
                "value": sri_value,
                "components": {k: v.get("weighted", v.get("raw", 0)) 
                             for k, v in sri_data.get("components", {}).items()}
            }
        })
    
    except Exception as err:
        return jsonify({
            "error": f"CAI computation failed: {str(err)}",
            "cai": {"score": 0, "state": "baseline"}
        }), 500


@app.route("/solar-composite")
def solar_composite():
    force_raw = normalize_text(request.args.get("force") or "0")
    force_refresh = force_raw in {"1", "true", "yes", "on"}

    try:
        payload = get_solar_composite(force_refresh=force_refresh)
        return jsonify(payload)
    except RuntimeError as err:
        cached = solar_cache.get("payload")
        if cached:
            stale = dict(cached)
            stale["stale"] = True
            stale["error"] = str(err)
            return jsonify(stale), 200
        return jsonify({"error": str(err), "source": "solar-composite"}), 503


@app.route("/trend-history", methods=["GET", "POST"])
def trend_history():
    if request.method == "GET":
        return jsonify(load_trend_history_file())

    payload = request.get_json(silent=True) or {}
    normalized = make_history_payload(payload)
    normalized["saved_at"] = int(time.time() * 1000)

    try:
        save_trend_history_file(normalized)
    except OSError as err:
        return jsonify({"error": f"Unable to write trend history: {err}"}), 500

    return jsonify({"ok": True, "saved_at": normalized["saved_at"]})


@app.route("/correlation-events", methods=["GET", "POST"])
def correlation_events():
    if request.method == "GET":
        start_ms = to_int(request.args.get("start"))
        end_ms = to_int(request.args.get("end"))
        limit = to_int(request.args.get("limit")) or 500
        events = load_correlation_events(start_ms=start_ms, end_ms=end_ms, limit=limit)
        return jsonify({"count": len(events), "events": events})

    payload = request.get_json(silent=True)
    raw_events = payload if isinstance(payload, list) else [payload]
    normalized_events = []

    for idx, raw in enumerate(raw_events):
        normalized = normalize_correlation_event(raw, fallback_index=idx)
        if normalized:
            normalized_events.append(normalized)

    if not normalized_events:
        return jsonify({"error": "No valid correlation events in payload", "written": 0}), 400

    try:
        append_correlation_events(normalized_events)
    except OSError as err:
        return jsonify({"error": f"Unable to write correlation events: {err}", "written": 0}), 500

    return jsonify({"ok": True, "written": len(normalized_events)})

if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_API_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_API_PORT", "5000"))
    app.run(host=host, port=port)
