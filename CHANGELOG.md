# Changelog

## 2026-06-24

### UI and Admin Experience

- Reworked the dashboard into an operational view with health status, usage counters, model ranking, account status, key usage, request quality, and recent logs.
- Filled empty days in the 7-day request trend so the chart always shows a complete 7-day timeline.
- Softened dark UI blocks to better match the white, lightweight interface: primary buttons, chart bars, tooltips, and code blocks now use lighter styling.
- Improved interaction feedback across the UI:
  - page selection persists after refresh;
  - toast messages queue instead of overwriting each other;
  - refresh, save, create, import, test, and delete actions show in-progress states;
  - account weight and priority edits show an unsaved marker;
  - destructive confirmations include the target account or key name.
- Filled out the Settings page with backend parameters, runtime status, connection information, and a quick verification command.

### Account Import and Routing

- Added local Work Buddy auth discovery that returns safe metadata only, without exposing token contents.
- Added one-click local account import and custom auth directory/file scanning.
- Hid missing auth candidate directories from the UI once a valid local auth directory is found.
- Added account routing controls:
  - weight;
  - priority;
  - enable/disable;
  - single-account test request.
- Updated account selection to prefer higher priority accounts, then lower weighted load.

### Authentication

- Replaced manual Admin Token setup for local Web UI use with an HttpOnly same-origin admin cookie.
- Kept `/admin/*` protected: requests without a valid cookie or token still return `401`.
- Kept the manual Admin Token input as a fallback for remote or abnormal cases.

### Local Git Checkpoints

- `6679815` - Polish local gateway dashboard and account import
- `0e24894` - Add account routing controls and test action
- `9f86c55` - Use local admin cookie for web UI
- `85f045a` - Fill empty days in dashboard trend
- `bf78954` - Soften dark UI blocks
- `1049682` - Improve UI interaction feedback
- `0d44036` - Hide missing auth candidates when detected
- `e5b06ae` - Fill out settings page
