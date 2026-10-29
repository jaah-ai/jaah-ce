"""The test suites' two published keys — stdlib-only, so the host-side, psycopg-free conftest can import them
beside `tests/conftest.py` and `app/fixture_db.py`. This file is their one home.

Both are refused outside a fixture env (`app/dev_bypass.py::is_fixture_env`), and on the community edition the
keypair always: "is this a key this repository publishes" has exactly two homes,
`app/credential_crypto.py::is_published_key` and `app/credential_keys.py::is_published_key`.

`FIXTURE_CREDENTIAL_KEY` is what `CREDENTIAL_ENCRYPTION_KEY` (`app/credential_crypto.py`) is set to wherever a
test process, the fixture-template build or the `./dc e2e-db` api must agree on the key the template's
`credentials.config` rows are sealed under: a readable 32-byte ASCII string, base64-encoded, so its text says
what it is (27 distinct bytes, over `credential_crypto._MIN_DISTINCT_KEY_BYTES`). Deliberately DISTINCT from
`credential_crypto`'s dev seed, which is refused as a current key everywhere.

`FIXTURE_LOCAL_PRIVATE_KEY` is what `CREDENTIAL_LOCAL_PRIVATE_KEY` (`app/credential_keys.py`) is set to
wherever a test process, the fixture-template build or the `./dc e2e-db` api must agree on the key the
template's tier-2 envelopes are wrapped under. Readable and published by design, like
`FIXTURE_CREDENTIAL_KEY`; deliberately DISTINCT from `credential_keys`' published dev keypair,
which reads as unset outside an affirmatively local process — the suites run with no `APP_ENV`.
RSA-4096, PKCS#8 DER, base64. Never a real deployment's key.
"""
from __future__ import annotations

import base64

FIXTURE_CREDENTIAL_KEY = base64.b64encode(b"pytest-credential-KEY 0x4m7q+zvb").decode()

FIXTURE_LOCAL_PRIVATE_KEY = (
    "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"
)
