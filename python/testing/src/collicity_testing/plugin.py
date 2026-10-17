"""Pytest plugin: makes the shared fixtures available to every suite."""

from collicity_testing.ledger import prices
from collicity_testing.postgres import database

__all__ = ["database", "prices"]
