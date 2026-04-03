#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/sanitized-template"

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"
mkdir -p "${OUT_DIR}/assets"
mkdir -p "${OUT_DIR}/data"
mkdir -p "${OUT_DIR}/scripts"

cp "${ROOT_DIR}/dashboard.html" "${OUT_DIR}/dashboard.html"
cp "${ROOT_DIR}/system_api.py" "${OUT_DIR}/system_api.py"
cp "${ROOT_DIR}/schumann_adapter.py" "${OUT_DIR}/schumann_adapter.py"
cp "${ROOT_DIR}/requirements.txt" "${OUT_DIR}/requirements.txt"
cp "${ROOT_DIR}/.env.example" "${OUT_DIR}/.env.example"
cp "${ROOT_DIR}/config.example.json" "${OUT_DIR}/config.example.json"
cp "${ROOT_DIR}/README.md" "${OUT_DIR}/README.md"
cp "${ROOT_DIR}/scripts/export_sanitized.sh" "${OUT_DIR}/scripts/export_sanitized.sh"

if [[ -f "${ROOT_DIR}/assets/angel1.jpg" ]]; then cp "${ROOT_DIR}/assets/angel1.jpg" "${OUT_DIR}/assets/angel1.jpg"; fi
if [[ -f "${ROOT_DIR}/assets/angel2.jpg" ]]; then cp "${ROOT_DIR}/assets/angel2.jpg" "${OUT_DIR}/assets/angel2.jpg"; fi
if [[ -f "${ROOT_DIR}/assets/angel3.jpg" ]]; then cp "${ROOT_DIR}/assets/angel3.jpg" "${OUT_DIR}/assets/angel3.jpg"; fi

cat > "${OUT_DIR}/config.local.json" <<'JSON'
{
  "location": {
    "label": "Your City, ST",
    "lat": 0.0,
    "lon": 0.0
  },
  "radar": {
    "lat": 0.0,
    "lon": 0.0,
    "zoom": 4,
    "tileRadius": 1
  },
  "visuals": {
    "backgroundImages": [
      "/assets/angel1.jpg",
      "/assets/angel2.jpg",
      "/assets/angel3.jpg"
    ]
  }
}
JSON

cat > "${OUT_DIR}/data/schumann_response.example.json" <<'JSON'
{
  "value": 0.0,
  "unit": "a.u.",
  "observed_at": "2026-01-01T00:00:00Z",
  "source": "example-source"
}
JSON

echo "Sanitized export created at: ${OUT_DIR}"
