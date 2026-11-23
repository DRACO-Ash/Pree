"""The diagnostics read-out, which is all that remains of this module.

Seventeen of this file's eighteen tests covered the storage proof: its timeout, its errno
reporting, its cache, its concurrency, and the EACCES path that a root-owned mount produces.
They are gone with the store, not weakened. The platform's file storage is a mountpoint-s3
mount with no POSIX advisory locking, so the store those tests protected could not have been
correct on it whatever they asserted; see `docs/APPSTORE-REDESIGN.md`.

What survives is the one property that was never about storage: the read-out an operator
reaches for during a deploy failure must answer the whole question at once, and must never
carry a secret value.
"""

from __future__ import annotations

from pathlib import Path

from pree import health
from tests.conftest import TEST_TOKEN, make_config


def test_diagnostics_reports_identity_and_never_a_secret_value(tmp_path: Path) -> None:
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    body = health.diagnostics(config.for_service())
    assert TEST_TOKEN not in str(body)
    assert body["team_token_length"] == len(TEST_TOKEN)
    assert body["identity_is_root"] == (body["identity_uid"] == 0)


def test_the_read_out_names_no_storage_field(tmp_path: Path) -> None:
    """A field describing a store that no longer exists is a claim, not a diagnosis.

    The read-out used to carry `data_dir`, `data_dir_is_absolute`, `data_dir_was_configured`,
    `storage_writable`, `storage_errno` and `storage_errno_name`. An operator reading a stale
    storage field on a stateless service would diagnose a fault that cannot occur, so their
    absence is asserted rather than assumed. This also fails if the store is ever reinstated
    without revisiting the platform constraint that removed it.
    """
    body = health.diagnostics(make_config(tmp_path).for_service())
    storage_fields = sorted(
        key for key in body if "storage" in key.lower() or "data_dir" in key.lower()
    )
    assert storage_fields == [], (
        f"the diagnostics read-out still names storage: {storage_fields}. Pree holds no state; "
        f"see docs/APPSTORE-REDESIGN.md before adding one back"
    )
    assert body, "the read-out is empty, so the assertion above would pass against nothing"
