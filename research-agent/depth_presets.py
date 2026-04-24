"""
depth_presets.py — Pure-data lookup table for the Light/Medium/Heavy/Ultra
depth presets. Kept in its own module so the research worker can import it
and set os.environ BEFORE importing config (which freezes MAX_SEARCH_RESULTS
from env at module-load). No runtime behaviour lives here — just numbers.
"""
from __future__ import annotations

DEPTH_PRESETS: dict[str, dict[str, int]] = {
    "light":  {"search_results": 3,  "researcher_iter": 5,  "analyst_iter": 3,  "synth_iter": 3, "gap_passes": 0},
    "medium": {"search_results": 5,  "researcher_iter": 10, "analyst_iter": 6,  "synth_iter": 4, "gap_passes": 2},
    "heavy":  {"search_results": 10, "researcher_iter": 20, "analyst_iter": 10, "synth_iter": 6, "gap_passes": 3},
    "ultra":  {"search_results": 20, "researcher_iter": 40, "analyst_iter": 16, "synth_iter": 8, "gap_passes": 5},
}
DEFAULT_DEPTH: str = "medium"


def resolve(depth: str) -> dict[str, int]:
    """Return the preset dict for `depth`, falling back to the default."""
    return DEPTH_PRESETS.get((depth or "").lower()) or DEPTH_PRESETS[DEFAULT_DEPTH]
