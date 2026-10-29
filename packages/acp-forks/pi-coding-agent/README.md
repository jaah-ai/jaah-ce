# pi-coding-agent — patched pin

`packaging/platform-pins.toml` pins `@earendil-works/pi-coding-agent` with `patches = [...]`, applied the same way
as [pi-acp's](../pi-acp/README.md): integrity check first, then each patch exactly, then `npm ci` against the
committed lock. Nothing is published to npm.

## `0001-undici-8.12.patch`

pi-coding-agent 1.0.2 (and every release through 1.4.0) pins `undici` exactly at `8.10.2`, which carries
GHSA-xjj6-f33q-929c (HIGH, a denial of service on excessive content-encodings, fixed in 8.12.2) and fails the
gate's runner CLI lock audit. Pi uses only undici's stable surface (`Client`, `Pool`, `EnvHttpProxyAgent`,
`setGlobalDispatcher`, `install`), so the patch moves the pin to `8.12.3`, and the committed lock carries that
one entry changed — nothing else re-resolved.

## Bumping or dropping it

- **pi bump:** regenerate the patch against the new tarball's `package.json` (`npm pack`, edit, `diff -u` with
  `a/` and `b/` paths), regenerate the lock (`runner_cli_pins.py lock @earendil-works/pi-coding-agent`), and run
  `test_runner_cli_pins_table.py`, which applies every patch to its pinned tarball.
- **Drop it** when an upstream release pins undici 8.12.2 or later: remove this directory, its `patches` line
  and its assertion in that test.
