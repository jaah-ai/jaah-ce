# pi-acp — patched pin

`packaging/platform-pins.toml` pins `pi-acp` with `patches = [...]`: the installer
(`apps/jaahd/runner_cli_pins.py`) verifies the upstream tarball's integrity, then applies each patch here to
its published tarball exactly (no fuzz) before installing; a patch that does not apply refuses the install.
Nothing is published to npm.

## `0001-surface-final-error.patch`

Upstream pi-acp 0.0.34 answers `session/prompt` with `end_turn` even when pi gave up on the model call, so a
429 or usage limit never reaches the host. With the patch, when the turn's last assistant message has
`stopReason: "error"` with a 429 or rate/usage-limit `errorMessage` (and the turn was not cancelled), the
prompt answers with a JSON-RPC error (code 429, `message` = pi's `errorMessage`) carrying
`data._meta["jaah.ai"] = {error_class: "usage_limit", resets_at}`. `resets_at` is a retry/reset time named in
the message (seconds, ISO or epoch next to "retry", "reset" or "until", never before now), else now + 1 hour:
pi 1.0.2 exposes no retry-after. Any other final error still ends the turn `end_turn`, as upstream does, so the
host's nudge recovers a transient failure.
The host's `classify_error` (`apps/jaahd/acp/events.py`) honours that `_meta` as it is, so spillover moves the
run. Replays: the daemon's `test_replay_pi.py`, recorded by `record_pi_rate_limit.py`.

## `0002-zod-4.patch`

Upstream pi-acp 0.0.34 declares `zod ^3.25.0`, and every zod 3 carries GHSA-mrhx-c7xc-m3w5 (HIGH, fixed only
in 4.6.6), which fails the gate's runner CLI lock audit. pi-acp's own `dist/` never imports zod; its only user is
`@agentclientprotocol/sdk`, whose peer range is `^3.25.0 || ^4.0.0`. The patch moves the dependency to
`^4.0.0`, so the committed lock resolves zod 4. Drop it when an upstream release declares zod 4.

## `0003-usage-cost.patch`

Upstream pi-acp 0.0.34 reports only context occupancy (`usage_update` `used`/`size`), though pi's
`get_session_stats` (read for it) carries the session's tokens and cost. With the patch:

- each `usage_update` also carries ACP's `cost: {amount: stats.cost, currency: "USD"}`, the session's running
  total, which the host reads as cumulative (`apps/jaahd/acp/events.py`, `usage_update`);
- the `session/prompt` result carries the turn's tokens in `_meta.quota.model_usage`, one row per model
  (`{model, token_count: {inputTokens, outputTokens, cachedReadTokens, cachedWriteTokens}}`), summed from the
  turn's assistant `message_end` usage — the shape claude-agent-acp and codex-acp report, read by
  `Normaliser._turn_usage` through the `PI` row's `usage_meta`. A turn that reported no usage carries no `_meta`.

`session.prompt` now resolves `{reason, usage}` (each turn its own counts, so a queued turn never takes another's).
A turn refused by 0001's usage-limit error carries no tokens. Replay: `test_closes_no_spend_reading` in
`test_replay_pi.py`, recorded by `record_pi_rate_limit.py` (`pi_usage_cost.json`).

## Bumping or dropping them

- **pi-acp bump:** regenerate each patch against the new tarball's file (`dist/index.js`, `package.json`;
  `npm pack pi-acp@<v>`, edit, `diff -u` with `a/` and `b/` paths), regenerate the lock
  (`runner_cli_pins.py lock pi-acp`), rerun the recorder, and run `test_runner_cli_pins_table.py`,
  which applies every patch to its pinned tarball.
- **Drop each on its own condition:** 0001 when an upstream release surfaces the final error (re-record the
  fixtures), 0002 when one declares zod 4, 0003 when one reports cost and per-turn tokens — remove its file and its entry in `patches`. Only once all are gone
  do this directory and the `patches` line go.
- **Upstream:** pi-acp issue [svkozak/pi-acp#135](https://github.com/svkozak/pi-acp/issues/135) asks for exactly
  this patch's behaviour (a JSON-RPC error on `stopReason: "error"` instead of `end_turn`), and PR
  [svkozak/pi-acp#136](https://github.com/svkozak/pi-acp/pull/136) implements it; it does not yet carry the
  `_meta["jaah.ai"]` usage-limit class and reset, so check `classify_error` against its error shape before dropping
  the patch. PR [svkozak/pi-acp#143](https://github.com/svkozak/pi-acp/pull/143) forwards pi's stderr, which would
  make `agents/pi_rpc.py`'s stderr relay (`JAAH_ENGINE_STDERR`, `acp/host.py`) redundant for pi.
