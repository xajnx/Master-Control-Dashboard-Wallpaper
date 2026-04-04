# Changelog

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
