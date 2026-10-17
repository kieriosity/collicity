"""The JSON Schemas in ``schemas/`` match the models they are generated from."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from collicity_grants.__main__ import SCHEMAS, render

SCHEMA_DIR = Path(__file__).parents[4] / "schemas"


@pytest.mark.parametrize(("name", "model"), SCHEMAS.items())
def test_committed_schema_is_current(name: str, model: type[BaseModel]):
    path = SCHEMA_DIR / name
    assert path.exists(), f"run `just schemas` to create {name}"
    assert path.read_text(encoding="utf-8") == render(model), f"run `just schemas`: {name} is stale"
