"""Load a FaultConfig from a YAML file, with CLI overrides applied on top.

Kept separate from faults.py so that neither the fault logic nor the CLI has
to know about YAML directly.
"""
from pathlib import Path

import yaml

from agentchaos.faults import FaultConfig

RATE_FIELDS = {"error_rate", "timeout_rate", "malformed_rate", "injection_rate", "ratelimit_rate"}
VALID_KEYS = set(FaultConfig.__dataclass_fields__)


def load_config(path: Path | None, overrides: dict | None = None) -> FaultConfig:
    """Build a FaultConfig from an optional YAML file, then apply `overrides`
    (e.g. CLI flags) on top - so a flag always wins over what's in the file.

    Validates as it goes: an unknown key is a clear error rather than being
    silently ignored (easy to typo `injection_rate` as `injecton_rate` and
    get a config that quietly does nothing), and every *_rate must be a
    probability between 0 and 1.
    """
    data: dict = {}
    if path is not None:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        unknown = set(data) - VALID_KEYS
        if unknown:
            raise ValueError(
                f"Unknown key(s) in {path}: {', '.join(sorted(unknown))}. "
                f"Valid keys are: {', '.join(sorted(VALID_KEYS))}."
            )

    merged = {**data, **(overrides or {})}

    for field in RATE_FIELDS:
        if field in merged and not (0.0 <= merged[field] <= 1.0):
            raise ValueError(f"{field} must be between 0 and 1, got {merged[field]}.")

    return FaultConfig(**merged)
