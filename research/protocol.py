"""Research protocol v1: one CALENDAR split shared by every cached profile.

The cached profiles overlap in time (1h/1y, 4h/5y, 1d/10y). Per-profile fraction
splits would put one profile's TRAIN inside another profile's holdout, so all
decisions use the same calendar boundaries:

    TRAIN       bars before 2023-09-01
    VALIDATION  2023-09-01 <= bar < 2025-03-01
    HOLDOUT     bars from 2025-03-01 onward (single use per frozen candidate)

Consequences: the 1h/1y profile (starts 2025-09) lies entirely in HOLDOUT and is
not used for development; development uses the 4h and daily profiles.

Disclosure: before this protocol was fixed, the unmodified ``live_default``
pipeline had been run once on per-profile fraction splits, which included part of
the post-2025-03 period (intraday and 4h). It is the baseline, not a candidate,
and no variant was evaluated on data at or after 2025-03-01 before freezing.
"""
from __future__ import annotations

from datetime import datetime

PROTOCOL = "v1"
VALIDATION_START = datetime(2023, 9, 1)
HOLDOUT_START = datetime(2025, 3, 1)

SEGMENTS = {
    "train": (None, VALIDATION_START),
    "validation": (VALIDATION_START, HOLDOUT_START),
    "development": (None, HOLDOUT_START),
    "holdout": (HOLDOUT_START, None),
}

DEVELOPMENT_PROFILES = ("swing_5y", "long_10y")
