"""The listener. Reads the port from the environment, binds every interface, and starts.

Never set the port in the image. The platform injects PORT and the code defaults to 8080; an
image-level default would beat the injected value and break the readiness probe.
"""

from __future__ import annotations

from fastapi import FastAPI

from .app import create_app
from .config import load_config
from .store import JsonStore


def build() -> FastAPI:
    """Resolve the environment, seed the store, and return the wired app.

    Boot fails closed and loudly: an unresolvable configuration raises before the app binds,
    so a misconfiguration surfaces as a start-up error rather than a silent runtime fault.
    """
    config = load_config()
    store = JsonStore(config.data_dir)
    try:
        store.seed()
        storage_state = "accepted"
    except OSError:
        # The pod may still serve liveness and diagnostics, which is what makes a storage
        # fault diagnosable rather than a silent kill. One decisive line records it.
        storage_state = "refused"
    # One decisive boot line to stdout, which is where the platform collects pod logs.
    print(
        f"pree boot: build={config.build_id} env={config.environment} "
        f"port={config.port} data_dir={config.data_dir} storage={storage_state}",
        flush=True,
    )
    return create_app(config, store)


app = build()
