# Changelog

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
