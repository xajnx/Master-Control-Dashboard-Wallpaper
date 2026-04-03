# Mission Control Dashboard

A local situational-awareness dashboard with a static frontend and a Flask API proxy.

## Features

- System telemetry: CPU, memory, disk, uptime
- Earthquakes and critical tsunami alerts
- Solar activity: Kp index and solar wind plasma context
- Schumann response widget with strict source separation
- Volcano activity details from EONET
- Weather, AQI, radar, and severe weather alerts
- Persistent 24-hour trend history across restarts

## Requirements

- Python 3.10+
- A local static file server for `dashboard.html`
- Internet access for the public upstream APIs

## Project Layout

- `dashboard.html`: frontend dashboard
- `system_api.py`: local Flask proxy/API layer
- `schumann_adapter.py`: Schumann payload normalizer
- `.env.example`: backend environment template
- `config.example.json`: frontend runtime config template
- `config.local.json`: starter runtime config included in this sanitized export
- `data/`: persisted trend history and optional local Schumann fallback

## Install

1. Create and activate a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install Python dependencies.

```bash
pip install -r requirements.txt
```

3. Create local backend config.

```bash
cp .env.example .env
```

4. Review `config.local.json` and update the location, radar, and visuals values for your site.

## Configuration

### Backend config: `.env`

Available keys:

- `DASHBOARD_API_HOST`: Flask bind host. Default `127.0.0.1`
- `DASHBOARD_API_PORT`: Flask bind port. Default `5000`
- `CORS_ALLOWED_ORIGINS`: comma-separated frontend origins allowed to call the API
- `NASA_API_KEY`: optional NASA API key. `DEMO_KEY` works with rate limits
- `SCHUMANN_API_URL`: optional real-time Schumann JSON source
- `SCHUMANN_VALUE_PATH`: optional dot-path to the numeric value inside a custom Schumann payload

### Frontend config: `config.local.json`

Set these values for your local install:

- `location.label`: display label for the weather widget
- `location.lat` and `location.lon`: weather/AQI coordinates
- `radar.lat`, `radar.lon`, `radar.zoom`, `radar.tileRadius`: radar viewport settings
- `visuals.backgroundImages`: array of background image paths

## Run

Start the backend API:

```bash
python3 system_api.py
```

In a second terminal, serve the frontend folder:

```bash
python3 -m http.server 8080
```

Then open:

```text
http://localhost:8080/dashboard.html
```

The frontend talks to the Flask API on `http://127.0.0.1:5000` unless overridden in the app config.

## Schumann Response Setup

The Schumann widget is intentionally strict and does not substitute solar wind data.

Provide one of these:

- Set `SCHUMANN_API_URL` to a JSON endpoint containing a numeric response field
- Create `data/schumann_response.json` as a local fallback source

Example local fallback file:

```json
{
  "value": 12.3,
  "unit": "a.u.",
  "observed_at": "2026-04-03T12:34:56Z",
  "source": "my-source"
}
```

The backend uses `schumann_adapter.py` to normalize flat payloads, nested objects, and simple timeseries arrays. If your upstream JSON uses a custom field path, set `SCHUMANN_VALUE_PATH`, for example:

```text
data.current.value
```

If the remote source fails or returns an incompatible payload, the backend falls back to `data/schumann_response.json` when available.

## Persistence

- Trend history is written to `data/trend_history.json`
- The frontend restores persisted history through `/trend-history`
- This file is local runtime state and should not be committed

## Troubleshooting

- Weather offline: verify `location.lat` and `location.lon` in `config.local.json`
- Frontend cannot reach backend: confirm Flask is running on the host/port from `.env`
- Empty Schumann widget: configure `SCHUMANN_API_URL` or provide `data/schumann_response.json`
- CORS errors: add your frontend origin to `CORS_ALLOWED_ORIGINS`

## Security Notes

- The API binds to `127.0.0.1` by default
- CORS defaults to localhost-only origins
- No elevated privileges or sudo access are required
