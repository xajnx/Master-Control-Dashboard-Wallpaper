# Changelog

## 2026-04-20

### Atmospheric driver integration
- Added lightning density ingestion module and integrated lightning as an atmospheric driver component in derived Schumann response calculations.
- Exposed lightning contribution in Schumann component output (`lightning_density`) including normalized score, strike count, and flash-rate metadata.

### Event Anomaly index and Schumann UX
- Added `GET /coherence-anomaly-index` endpoint for the Coherence/Event Anomaly score and feature breakdown.
- Embedded compact Lightning and Event Anomaly chips in the Schumann panel without altering the grid layout.
- Added conditional top-contributor rendering for elevated/anomalous states.
- Added clickable Schumann anomaly diagnostics drawer and persisted open/closed preference in localStorage.

### System monitor telemetry expansion
- Extended `/system` payload and widget with RAM used/total values, network RX/TX rate, optional WiFi signal level, and optional CPU temperature display.

### Sanitized template updates
- Renamed bundled sanitized-template background images to generic names (`bg1.jpg`, `bg2.jpg`, `bg3.jpg`).
- Updated sanitized-template dashboard/config/export references to use generic background names.

## 2026-04-10

### Schumann panel UX polish
- Restored explicit Space Weather role coupling by showing `Response` as a top-right role badge in the Schumann section header (matching Solar `Driver`).
- Moved the live Schumann response metrics block into the Schumann header control area so chart context and current state are visible together.
- Repositioned the SRI threshold legend above the Schumann composite chart and tightened chart spacing to reduce unnecessary scrolling.
- Disabled Schumann widget internal vertical scroll overflow for the current layout pass to keep the panel visually stable.

### Schumann chart and data integrity
- Kept Schumann composite chart rendering focused on chart, generated, and Tomsk modes while preserving clear source separation.
- Preserved safeguards that prevent generated fallback frames from polluting persisted ELF observations.

### Backend/source resilience updates
- Added optional host-scoped insecure TLS fallback for spectrogram proxy fetches (`allowInsecureTomsk`) with explicit response/header signaling.
- Added `/solar-composite` and GOES geomagnetic endpoints to support unified driver/response rendering and improved fallback behavior.
- Expanded cache-based degradation handling for quakes, tsunami, alerts, volcano, and NEO paths to reduce blank-widget states during upstream outages.

## 2026-04-04

### Dashboard and UX updates
- Reworked the dashboard grid from equal-size panels to a fixed asymmetric layout so high-density widgets receive more space.
- Added a 1366x768 optimization profile for improved wall-display readability.
- Reduced text density and spacing across widgets to improve scanability.
- Added compact panel styling for lower-density widgets.
- Tuned NEO and Alerts row density to fit more useful information per panel.

### Schumann widget improvements
- Consolidated Schumann into a scroll-aware structure and reduced overflow/clipping risk.
- Moved Schumann timing/source/status details into a single compact status row.
- Reduced Schumann spectrogram image height for better chart/image coexistence.
- Added in-widget spectrogram diagnostics messages for source/load failures.
- Updated chart rendering logic to draw even with a single point, preventing blank charts right after restart.

### Reliability and health telemetry
- Added startup self-check summary indicators (API/config/source availability).
- Added per-widget stale-state visual highlighting after extended no-success windows.
- Extended health detail output to include last known successful update context.

### API and backend behavior
- Updated CORS handling to better support local dashboard runtime contexts.
- Improved NEO endpoint degradation behavior for upstream rate-limit scenarios.
- Improved Schumann error reporting and diagnostics pathways.

### Configuration
- Added/updated local Schumann source configuration and fallback behavior for spectrogram feeds.
