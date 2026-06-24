# Changelog

## 2026-06-24

### Baseline Merge and Project Direction

- Compared the existing MIMO V2.5 PRO project with the GLM 5.2 gateway implementation and chose the GLM implementation as the main line because it had the packet-capture protocol context and had already been verified on a test machine.
- Promoted the GLM implementation into the formal project root and kept useful local project ideas where they fit the GLM codebase.
- Archived the local gateway baseline as the starting checkpoint for this merged local-only project.
- Added ignore rules for local distribution/runtime artifacts so packaged files, generated state, and local secrets do not become part of normal source changes.

### Protocol and Compatibility Work

- Added `reasoning_content` passthrough support for clients that expect reasoning-style fields.
- Added model alias mapping so common client model names can be mapped onto Work Buddy backend model IDs.
- Fixed alias permission checks so API Key model allowlists work against both requested aliases and resolved backend model IDs.
- Kept the API surface OpenAI-compatible for `/v1/models` and `/v1/chat/completions`.

### Naming and Documentation

- Renamed the project to `Buddy 2 API`.
- Added and refined Chinese and English READMEs.
- Later made README wording more direct for local-only use, after deciding not to publish the project due to compliance risk.
- Added this changelog to keep a project-level record beyond git commit messages.

### Dashboard and Productization

- Reworked the dashboard from a basic page into an operational view with:
  - gateway health status;
  - total and daily requests;
  - token and credit usage;
  - success/error/filter counts;
  - 7-day trend;
  - model ranking;
  - account status;
  - API Key usage;
  - recent request logs.
- Filled empty days in the 7-day request trend so it always displays a complete 7-day timeline.
- Softened dark UI blocks to better match the lightweight white interface.
- Improved interaction feedback across the UI:
  - page selection persists after refresh;
  - toast messages queue instead of overwriting each other;
  - refresh, save, create, import, test, and delete actions show in-progress states;
  - unsaved account routing edits are marked;
  - destructive confirmations include the target account or key name.
- Filled out the Settings page with backend parameters, runtime status, connection information, and a quick verification command.

### Account Import and Routing

- Added local Work Buddy auth discovery based on default auth locations and optional custom auth directory/file paths.
- Made discovery return safe metadata only, without exposing token contents.
- Added one-click local account import from the detected auth file.
- Added support for importing from a custom auth directory or a specific `.info` file.
- Hid missing auth candidate directories from the UI once a valid local auth directory is found.
- Added multi-account routing controls:
  - weight;
  - priority;
  - enable/disable;
  - single-account test request.
- Updated account selection to prefer higher priority accounts, then lower weighted load.

### Local Admin Authentication

- Replaced the awkward manual Admin Token workflow for local Web UI use with an HttpOnly same-origin admin cookie.
- Kept the manual Admin Token input as a fallback for remote or abnormal cases.
- Kept `/admin/*` protected: requests without a valid cookie or token still return `401`.

### Local OpenCode / OpenClaw Integration

- Verified the local gateway works as an OpenAI-compatible provider for OpenCode/OpenClaw-style clients.
- Removed the problematic OMO/oh-my-openagent plugin path from the local test setup after it caused blocked responses.
- Kept the native OpenCode flow as the tested integration path.

### Local Git Checkpoints

- `e1f75b6` - Archive local CodeBuddy gateway baseline
- `9bdfff7` - Ignore local distribution artifacts
- `4626521` - feat: add reasoning_content passthrough and model alias mapping
- `3214817` - fix: alias permission check and UI editability
- `8354732` - refactor: rename project to Buddy 2 API
- `5cb3937` - docs: add English README, refine Chinese README with use case hints
- `281f50d` - docs: use 'Claw' hint for product reference
- `6679815` - Polish local gateway dashboard and account import
- `0e24894` - Add account routing controls and test action
- `9f86c55` - Use local admin cookie for web UI
- `ea685bd` - docs: unify title to Buddy2api, sync EN/CN READMEs
- `85f045a` - Fill empty days in dashboard trend
- `bf78954` - Soften dark UI blocks
- `1049682` - Improve UI interaction feedback
- `0d44036` - Hide missing auth candidates when detected
- `e5b06ae` - Fill out settings page
