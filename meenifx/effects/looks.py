"""The Look group card.

A look is applied as several normal effect entries plus one of these cards. The card
renders nothing itself: the engine reads its Strength and scales the Mix of every entry
carrying the same ``look_id``, so the whole stack fades with one slider while each
effect inside stays editable on its own.
"""

from __future__ import annotations

from ..model.values import Value
from .base import LayerEffect, ParamSpec


class LookEffect(LayerEffect):
    key = "look"
    name = "Look"
    category = "Looks"

    PARAMS = (
        ParamSpec("strength", "Strength", "float", 0.0, 100.0, 100.0, step=1.0, unit="%",
                  help="Scales every effect in this look; 0% is the original picture"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.strength = Value(100.0)

    def apply_layer(self, layer):
        # The engine applies the strength to the stack's Mix values; nothing to draw.
        return layer


LOOK_EFFECTS = (LookEffect,)
