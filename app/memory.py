"""Memoria en Render gratis (512 MB): glibc reserva un 'arena' de memoria por hilo y casi nunca la devuelve.
- limit_arenas(): máximo 2 arenas (se llama al arrancar).
- trim(): devuelve al sistema la memoria libre (se llama después de cada paso pesado: escenas, video final).
"""
from __future__ import annotations

import ctypes
import gc
import logging

log = logging.getLogger("memory")
_libc = None


def _c():
    global _libc
    if _libc is None:
        try:
            _libc = ctypes.CDLL("libc.so.6")
        except OSError:
            _libc = False
    return _libc


def limit_arenas(n: int = 2) -> None:
    c = _c()
    if c:
        try:
            c.mallopt(-8, n)        # M_ARENA_MAX
        except Exception:
            pass


def trim() -> None:
    gc.collect()
    c = _c()
    if c:
        try:
            c.malloc_trim(0)
        except Exception:
            pass
