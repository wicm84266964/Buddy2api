# Issues #141 / #140 and PR #138 acceptance

Date: 2026-11-06 (Asia/Shanghai)
Branch: `codex/issues-141-138-140`
Base: `origin/main` (v2.2.18). Original PR #138 author attribution is retained.

## Completed changes

- #141: normalize empty continuation `id` / `type` to absent fields. Preserve real call IDs and keep rejecting invalid types, conflicting identities and incomplete calls.
- #138: retain failure streaks across cooldown expiry, decay after 30 minutes of inactivity, clear on success, and honor quota reset hints up to one hour. Fix short timezone offsets (`UTC+8`, `UTC+0`, `UTC-5`), invalid offsets, platform timestamp failures, concurrent cooldown shortening, and the 300-second ceiling for server errors.
- #140: add default-off, FIFO per-account pacing and real concurrency limits at every provider chat send location. Chat/Responses, JSON/SSE, account tests and retries share the same slots. Authentication, billing and check-in do not consume them.
- Add validated, persisted controls to the settings page: minimum start interval (0–600 seconds), maximum concurrent calls (0–100), queue timeout (1–600 seconds, default 120). Zero interval and concurrency disable limits. Each account permits up to 256 waiting calls.
- Return local queue failures as 429 (`local_rate_limit` for Chat, `rate_limit_exceeded` for Responses); SSE errors terminate the stream. Do not expire or penalize accounts for local queue errors.

## Interaction review and repairs

- Preserve a quota cooldown when another in-flight request reports a shorter failure.
- Skip queued requests whose account entered cooldown while they waited.
- Close context-rejected streams before retrying, so concurrency 1 cannot deadlock its own retry. Close replacement streams on disconnect too.
- Close TraeWork's already-peeked upstream on normal completion, early cancellation and even closure before the output wrapper starts; otherwise its concurrency slot could remain occupied.
- Keep local rate-limit details through cross-channel failover and translate them into the supported Responses error vocabulary.
- Reject invalid, fractional concurrency, non-finite and oversized numeric settings before any settings are written.

## Verification

- `python -m pytest -q --basetemp=.git/pytest-issues-final`: **790 passed**.
- Python compilation of changed modules, providers and tests: passed.
- `node tools/check_web_templates.js`: **7 templates compiled, 2 scripts parsed, 0 failures**.
- Whitespace check with `cr-at-eol` (the repository contains tracked CRLF files): passed.
- Tests cover parallel tool-call continuation, malformed identities, cooldown escalation/decay/reset hints, FIFO admission, independent accounts, live setting changes, cancellation, queue timeout/capacity, local 429 mapping, context recovery and TraeWork stream ownership.
- Real HTTPX clients with mock upstream transports verify that cancellation closes the response, releases the account and lets the next JSON/SSE request proceed.
- Browser verification used a separate empty SQLite database on port 8879: set interval 5.5 / concurrency 1 / queue timeout 60, save and reload; all values persisted and the form rendered correctly. The temporary server was stopped afterward. Existing account data and normal gateway settings were not changed.

## Manual acceptance

1. Start this branch normally and open Settings → 主动限流. Defaults should be 0 / 0 / 120.
2. Save 5 seconds / concurrency 1; reload to verify persistence. Run overlapping requests on one account: they should queue; requests on different accounts remain independent.
3. Cancel a running stream; the next queued request should proceed without a leaked slot.
4. Exercise a parallel tool-call stream with empty continuation IDs/types; real first-frame IDs should survive and the stream should complete.
5. Set both interval and concurrency to 0 and save to disable limits.

Limits are process-local, reset on restart and cannot guarantee avoiding vendor token/model/cumulative quotas. Vendor services were not called for this verification. Cross-channel failover disabled: local queue errors return directly. No remote PR was merged or release published as part of this acceptance branch.


## Release preparation follow-up

Before v2.2.19 publication, a clean-database CI run exposed a test isolation dependency: transport unit tests read the developer database's limiter settings, and the HTTP cancellation test also read its timeout. Shared tests now use isolated default limiter settings; persistence tests explicitly call the real reader with a stub store. The cancellation test stubs the timeout and bounds its initial wait. The complete 790-test suite was re-run against a new nonexistent database path.
