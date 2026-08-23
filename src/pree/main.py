"""The listener. Reads the port from the environment, binds every interface, and starts.

Never set the port in the image. The platform injects PORT and the code defaults to 8080. An
image-level default does NOT beat the injected value, because a runtime value overrides image ENV;
it shadows the code's own default so that default is never reached in the container, and it asserts
a port the platform may not use.
"""

from __future__ import annotations

from fastapi import FastAPI

from .app import create_app
from .config import load_config
from .security import token_verifier
from .store import JsonStore, StoreError


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
    except (OSError, StoreError):
        # StoreError is caught alongside OSError deliberately. The store raises its own
        # error class for a corrupt or truncated snapshot, which is exactly the state a crash
        # mid-write or a rollback leaves behind. Catching only OSError let that escape during
        # module import, so gunicorn could not import the app and the pod never bound: no
        # liveness path, no diagnostics, just CrashLoopBackOff and one traceback. The pod must
        # stay up and diagnosable when storage is the thing that is broken.
        storage_state = "refused"
    # The BOUNDARY, taken BEFORE the boot line rather than after it. `for_service()` drops the
    # credential and `token_verifier` closes over it, so the app is built from a config with no
    # token field and a callable that does the compare, and nothing downstream can read the secret
    # whatever it is named.
    #
    # The boot line then reads its length from the token-free view rather than from the token. That
    # is not fussiness: this function was the ONLY reader of `config.team_token` outside the two
    # permitted ones, and every reader is a place the value can be passed somewhere else. Deriving
    # the length here instead leaves the credential with exactly three readers in the package,
    # which `test_the_credential_has_exactly_one_set_of_readers_across_the_whole_package` asserts.
    service = config.for_service()
    # One decisive boot line to stdout, which is where the platform collects pod logs.
    print(
        # auth and token length are server-side only, never the value. Without them a stale
        # token is invisible on a deployed pod: every client gets 401 and the only read-out
        # that would show it sits behind the very token that is wrong.
        f"pree boot: build={service.build_id} env={service.environment} "
        f"port={service.port} data_dir={service.data_dir} "
        f"data_dir_configured={service.data_dir_was_configured} "
        f"auth={'on' if service.auth_enabled else 'off'} "
        f"token_len={service.token_length} storage={storage_state}",
        flush=True,
    )
    return create_app(service, store, verify_token=token_verifier(config))


# No module-level `app = build()`. Booting on import means any import of this module reads the
# real environment and can raise, so a fail-closed configuration error became an import error:
# unimportable to a test collector, and to gunicorn a worker that dies before it can log why.
# The launch command calls the factory explicitly instead, which gunicorn supports.
