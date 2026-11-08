"""Robot Foundry: image-to-robot generation pipeline.

The pipeline is deliberately split into independent stages so a better
reconstruction model can replace one stage without touching the IDE or the
simulator integration:

``segmentation -> reconstruction -> assembly -> physics -> MJCF -> validation``

Only the validation stage imports the physics engine, and it does so in a
short-lived subprocess. Everything else is pure Python data processing.
"""

from __future__ import annotations

from .store import FoundryStore

__all__ = ["FoundryStore"]
