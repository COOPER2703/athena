"""Adaptateur du magasin de Planifications (ADR-0028)."""

from __future__ import annotations

from server.adapters.schedule.store import SqliteScheduleStore

__all__ = ["SqliteScheduleStore"]
