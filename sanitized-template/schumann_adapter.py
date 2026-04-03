from datetime import datetime, timezone


DEFAULT_VALUE_KEYS = (
    "value",
    "response",
    "amplitude",
    "latest",
    "current",
    "schumann",
    "schumann_response",
)

DEFAULT_TIME_KEYS = (
    "observed_at",
    "timestamp",
    "time",
    "ts",
    "date",
)


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _walk_path(data, path):
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _iso_utc_now():
    return datetime.now(timezone.utc).isoformat()


def _extract_from_mapping(mapping, keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping:
            return mapping.get(key), key
    return None


def _extract_value(data, value_path=None):
    # Explicit override path takes priority.
    if value_path:
        path_value = _walk_path(data, value_path)
        parsed = _to_float(path_value)
        if parsed is not None:
            return parsed, f"path:{value_path}"

    if isinstance(data, dict):
        direct = _extract_from_mapping(data, DEFAULT_VALUE_KEYS)
        if direct:
            val, key = direct
            parsed = _to_float(val)
            if parsed is not None:
                return parsed, f"root:{key}"

        # Common nested containers.
        for container in ("data", "current", "latest", "metrics", "measurement", "reading"):
            nested = data.get(container)
            if isinstance(nested, dict):
                hit = _extract_from_mapping(nested, DEFAULT_VALUE_KEYS)
                if hit:
                    val, key = hit
                    parsed = _to_float(val)
                    if parsed is not None:
                        return parsed, f"{container}:{key}"

        # Timeseries array patterns.
        for arr_key in ("samples", "readings", "series", "values", "data"):
            arr = data.get(arr_key)
            if isinstance(arr, list) and arr:
                for item in reversed(arr):
                    if isinstance(item, dict):
                        hit = _extract_from_mapping(item, DEFAULT_VALUE_KEYS)
                        if hit:
                            val, key = hit
                            parsed = _to_float(val)
                            if parsed is not None:
                                return parsed, f"{arr_key}[]:{key}"
                    else:
                        parsed = _to_float(item)
                        if parsed is not None:
                            return parsed, f"{arr_key}[]"

    if isinstance(data, list) and data:
        for item in reversed(data):
            parsed = _to_float(item)
            if parsed is not None:
                return parsed, "list[]"
            if isinstance(item, dict):
                hit = _extract_from_mapping(item, DEFAULT_VALUE_KEYS)
                if hit:
                    val, key = hit
                    parsed = _to_float(val)
                    if parsed is not None:
                        return parsed, f"list[]:{key}"

    return None, None


def _extract_time(data):
    if isinstance(data, dict):
        hit = _extract_from_mapping(data, DEFAULT_TIME_KEYS)
        if hit:
            val, key = hit
            if isinstance(val, str) and val.strip():
                return val, f"root:{key}"

        for container in ("data", "current", "latest", "measurement", "reading"):
            nested = data.get(container)
            if isinstance(nested, dict):
                hit = _extract_from_mapping(nested, DEFAULT_TIME_KEYS)
                if hit:
                    val, key = hit
                    if isinstance(val, str) and val.strip():
                        return val, f"{container}:{key}"

        for arr_key in ("samples", "readings", "series", "values", "data"):
            arr = data.get(arr_key)
            if isinstance(arr, list) and arr:
                for item in reversed(arr):
                    if not isinstance(item, dict):
                        continue
                    hit = _extract_from_mapping(item, DEFAULT_TIME_KEYS)
                    if hit:
                        val, key = hit
                        if isinstance(val, str) and val.strip():
                            return val, f"{arr_key}[]:{key}"

    return _iso_utc_now(), "generated"


def adapt_schumann_payload(data, source_hint="configured-source", value_path=None):
    if not isinstance(data, (dict, list)):
        return None

    value, value_source = _extract_value(data, value_path=value_path)
    if value is None:
        return None

    observed_at, time_source = _extract_time(data)

    unit = "a.u."
    if isinstance(data, dict):
        maybe_unit = data.get("unit") or data.get("units")
        if isinstance(maybe_unit, str) and maybe_unit.strip():
            unit = maybe_unit.strip()
        elif isinstance(data.get("data"), dict):
            nested_unit = data["data"].get("unit") or data["data"].get("units")
            if isinstance(nested_unit, str) and nested_unit.strip():
                unit = nested_unit.strip()

    return {
        "value": value,
        "unit": unit,
        "source": str(source_hint or "configured-source"),
        "observed_at": str(observed_at),
        "adapter": {
            "value_source": value_source,
            "time_source": time_source,
            "value_path": value_path or "auto"
        }
    }
