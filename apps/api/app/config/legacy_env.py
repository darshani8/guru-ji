"""Accept the environment variable names used before the service was renamed.

Settings are read as ``SAFFRON_*``. A deployment configured before the rename
still sets ``GURU_*``: each of those is copied to its ``SAFFRON_*`` name unless
that name is already set, so an existing task definition keeps working (and
keeps its production guards) until it is renamed. A ``SAFFRON_*`` value always
wins over its legacy spelling.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

PREFIX = "SAFFRON_"
LEGACY_PREFIX = "GURU_"


def adopt_legacy_names(environ: MutableMapping[str, str] | None = None) -> tuple[str, ...]:
    """Copy each ``GURU_*`` variable to its ``SAFFRON_*`` name where that is unset; returns the legacy names used."""

    env = os.environ if environ is None else environ
    adopted: list[str] = []
    for key in list(env):
        if not key.startswith(LEGACY_PREFIX):
            continue
        name = PREFIX + key[len(LEGACY_PREFIX):]
        if name not in env:
            env[name] = env[key]
            adopted.append(key)
    return tuple(sorted(adopted))


__all__ = ["LEGACY_PREFIX", "PREFIX", "adopt_legacy_names"]
