"""Agent Saffron application package."""

from .config.legacy_env import adopt_legacy_names

# Before any module reads its settings: a deployment still configured with the
# GURU_* names from before the rename keeps working.
adopt_legacy_names()
