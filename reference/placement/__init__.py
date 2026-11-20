"""Fixed-data placement reference, with one-way inline-or-spill as the default.

Optional contracts are explicit imports: ``two_way.Service`` returns inline below
T/2, and ``recompute.Service`` recomputes eligible inline-input retries.
"""
from .protocol import Service, Store

__all__ = ['Service', 'Store']
