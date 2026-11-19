"""Local, engine-independent reference for exact item stages and reservations."""

if not __debug__:
    raise RuntimeError(
        "reference requires assertions enabled. "
        "Run Python without -O/-OO and unset PYTHONOPTIMIZE."
    )

from .model import (
    Batch, CapacityError, Item, MemorySource, Page, Pager, Plan, PlanError,
    ReadError, Reservation, Stage, StateError,
)

__all__ = [
    "Batch", "CapacityError", "Item", "MemorySource", "Page", "Pager", "Plan",
    "PlanError", "ReadError", "Reservation", "Stage", "StateError",
]
