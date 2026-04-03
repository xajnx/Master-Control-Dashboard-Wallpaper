from flask import Flask, jsonify, request
from flask_cors import CORS
import psutil
import time
import os
import json
from datetime import date, datetime, timezone
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from schumann_adapter import adapt_schumann_payload

app = Flask(__name__)


def parse_allowed_origins():
    configured = os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:8080,http://127.0.0.1:8080,http://localhost:5500,http://127.0.0.1:5500"
    )
    origins = [o.strip() for o in configured.split(",") if o.strip()]
    return origins


CORS(app, resources={
    r"/*": {
        "origins": parse_allowed_origins()
    }
})

start_time = time.time()
neo_cache = {}
tsunami_cache = {"cached_at": 0, "payload": None}
NEO_CACHE_TTL_SECONDS = 6 * 3600
TSUNAMI_CACHE_TTL_SECONDS = 120
LUNAR_DISTANCE_KM = 384400
NEAR_MISS_LD_THRESHOLD = 0.75
CLOSE_LD_THRESHOLD = 3.0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TREND_HISTORY_PATH = os.path.join(DATA_DIR, "trend_history.json")
LOCAL_SCHUMANN_PATH = os.path.join(DATA_DIR, "schumann_response.json")

TSUNAMI_EVENT_ALLOWLIST = {
    "tsunami warning",
    "tsunami watch"
}

SEVERITY_SCORE = {"extreme": 4, "severe": 3, "moderate": 2, "minor": 1, "unknown": 0}
URGENCY_SCORE = {"immediate": 4, "expected": 3, "future": 2, "past": 1, "unknown": 0}
CERTAINTY_SCORE = {"observed": 3, "likely": 2, "possible": 1, "unlikely": 0, "unknown": 0}

def get_uptime():
    uptime_seconds = time.time() - start_time
    hours = int(uptime_seconds // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    seconds = int(uptime_seconds % 60)
    return f"{hours}h {minutes}m {seconds}s"


def fetch_json(url, timeout=8):
    req = Request(url, headers={
        "User-Agent": "Mission-Control-Dashboard/1.0",
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


@app.route("/system")
def system():
    cpu = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent

    return jsonify({
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
        "uptime": get_uptime()
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

        return jsonify({"error": str(err), "asteroids": []}), 502

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


@app.route("/schumann-response")
def schumann_response():
    upstream = os.environ.get("SCHUMANN_API_URL", "").strip()
    value_path = os.environ.get("SCHUMANN_VALUE_PATH", "").strip() or None

    if upstream:
        try:
            data = fetch_json(upstream, timeout=8)
            parsed = adapt_schumann_payload(data, source_hint=upstream, value_path=value_path)
            if parsed:
                parsed["mode"] = "live"
                return jsonify(parsed)
        except RuntimeError as err:
            return jsonify({
                "error": str(err),
                "hint": "Set SCHUMANN_API_URL to a compatible JSON endpoint or provide data/schumann_response.json"
            }), 502

    if os.path.exists(LOCAL_SCHUMANN_PATH):
        try:
            with open(LOCAL_SCHUMANN_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            parsed = adapt_schumann_payload(data, source_hint="local-file", value_path=value_path)
            if parsed:
                parsed["mode"] = "local-file"
                return jsonify(parsed)
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "Invalid local Schumann file format"}), 500

    return jsonify({
        "error": "Schumann source unavailable",
        "hint": "Provide data/schumann_response.json with {value, unit, observed_at, source}"
    }), 503


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

if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_API_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_API_PORT", "5000"))
    app.run(host=host, port=port)
