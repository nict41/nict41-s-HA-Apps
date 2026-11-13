"""Animated tube widgets: a registry of animators keyed by widget type.

An animator draws one frame of a widget into a small region of its 135x240 tube.
The controller (controller._anim_tick) calls `frame` when the widget's interval
has passed and uploads the returned region with a partial upload (send_rect), so
only small parts of a tube move - never a frame-by-frame effect across all six
tubes (one full tube upload takes ~218 ms on the clock).

Nothing is registered here yet; the ticker and analogue clock widgets register
their animators in their own modules. Tests use a fake animator.
"""

from __future__ import annotations

from typing import Any, Protocol

from PIL import Image


class Animator(Protocol):
    def interval(self, w: dict) -> float:
        """Seconds between frames; 0 (or negative) means the widget is not animated."""
        ...

    async def frame(self, w: dict, ctx, t: float, bg) -> tuple[Image.Image, tuple[int, int, int, int]] | None:
        """The next frame: an image plus the (x, y, w, h) rectangle it covers inside
        the 135x240 tube. `t` is seconds since the screen became active, `bg` the
        screen's background colour. Return None to skip this frame."""
        ...


# widget type -> Animator
ANIMATORS: dict[str, Animator] = {}


def register(widget_type: str, animator: Animator) -> None:
    """Register `animator` for widgets of `widget_type` (replacing any previous one)."""
    ANIMATORS[widget_type] = animator


def animator_for(widget: Any) -> Animator | None:
    """The animator registered for a widget's type, if any."""
    if not isinstance(widget, dict):
        return None
    return ANIMATORS.get(str(widget.get("type")))
