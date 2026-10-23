"""Fixtures for the dashboard views: `dash_db` (two seeded tenants, no gateway linked)."""

from __future__ import annotations

import pytest
from dash_seed import Seeded, make_tenants, seed_framework


@pytest.fixture
async def dash_db(db):
    """The per-test database with tenants a and b and their runs, events, audit records,
    repos and kill switches; the gateway is not linked (the contract views are empty)."""
    a, b = await make_tenants(db.owner)
    await seed_framework(db.owner, a, b)
    return db, Seeded(a=a, b=b)
