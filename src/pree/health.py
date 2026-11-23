"""Health and the diagnostics read-out.

Two probe shapes, deliberately distinct:

* Liveness. The conventional paths return 200 and touch nothing. Nothing they do can hang,
  because a liveness probe that hangs turns an infrastructure fault into a silent pod kill
  with no narrative to diagnose.
* Diagnostics. A secret-free read-out with every plausible field present at once, reporting
  each critical input as a boolean and a length, never a value.

There was a third: a storage proof on its own path, which performed a real write and raced a
hard timeout. It is gone with the store. The platform's file storage is a mountpoint-s3 mount
with no POSIX locking, so the store it proved could not have been correct there; see
`docs/APPSTORE-REDESIGN.md`. Nothing here touches a filesystem now, which is why the liveness
guarantee is structural rather than carefully maintained.
"""

from __future__ import annotations

import os
from typing import Any

from .config import ServiceConfig


def diagnostics(config: ServiceConfig) -> dict[str, Any]:
    """A secret-free read-out. Every critical input is a boolean and a length, never a value.

    Every plausible field is present at once, so one read-out answers the whole question
    rather than prompting a second round trip during a deploy failure.
    """
    # The LENGTH, carried as a precomputed fact on ServiceConfig. This module never held the
    # value and now cannot: the token field does not exist on the type it receives.
    token_length = config.token_length
    origin = config.allowed_origin
    return {
        "build_id": config.build_id,
        "environment": config.environment,
        "port": config.port,
        "auth_enabled": config.auth_enabled,
        "team_token_present": config.auth_enabled,
        "team_token_length": token_length,
        "allowed_origin_present": origin is not None,
        "allowed_origin_length": len(origin) if origin else 0,
        "allowed_origin_is_wildcard": origin == "*",
        "identity_uid": os.getuid(),
        "identity_gid": os.getgid(),
        "identity_is_root": os.getuid() == 0,
    }
