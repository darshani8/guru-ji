from __future__ import annotations

from app.api.dependencies import build_runtime
from app.config.settings import AppSettings


def main() -> None:
    runtime = build_runtime(AppSettings.from_env())
    try:
        print(f"database backend: {runtime.store.backend_name}")
        print(f"database ready: {runtime.store.ping()}")
        print("control-plane migrations applied")
    finally:
        runtime.store.close()


if __name__ == "__main__":
    main()
