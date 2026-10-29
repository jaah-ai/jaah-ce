# codex-acp — patched pin

`packaging/platform-pins.toml` pins `@agentclientprotocol/codex-acp` with `patches = [...]`, applied by the runner
CLI installer exactly as the [pi-acp](../pi-acp/README.md) patches are (integrity first, no fuzz, a patch that does not
apply refuses the install). Nothing is published to npm.

## `0001-config-projects-win.patch`

Upstream codex-acp 2.2.2 builds each session's `thread/start` config from `CODEX_CONFIG` and then overwrites its
`projects` table, marking every session root (the cwd and each additional directory) `trusted`. The Codex adapter
runs the fleet harness under `bypass_hook_trust` (`apps/jaahd/agents/codex.py`, the ACP runtime block), which would
also run a repository's own `.codex/hooks.json` in a trusted project. With the patch a `projects` entry in
`CODEX_CONFIG` wins over that forced trust, so the adapter's `untrusted` roots keep the repository's `.codex/` layer
unloaded (its `AGENTS.md` still loads). The adapter turns the bypass on only when the installed build carries this
patch (`_acp_engine_fences_repo_hooks`). Test: `test_a_repo_hooks_file_does_not_run_under_codex_acp` in
the daemon's `test_codex_acp_hooks.py`, which drives the installed codex-acp.

## Bumping or dropping it

- **codex-acp bump:** regenerate the patch against the new tarball's `dist/index.js` (`npm pack
  @agentclientprotocol/codex-acp@<v>`, edit, `diff -u` with `a/` and `b/` paths), regenerate the lock
  (`runner_cli_pins.py lock @agentclientprotocol/codex-acp`), and run `test_runner_cli_pins_table.py`
  and `test_codex_acp_hooks.py`.
- **Drop it** when an upstream release lets a configured `projects` entry stand, or stops forcing trust: remove the
  file and the `patches` line, and keep the hooks test green against the unpatched build.
