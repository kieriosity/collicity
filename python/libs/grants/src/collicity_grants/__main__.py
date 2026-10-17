"""Write the JSON Schemas for manifests, grants and policy: ``python -m collicity_grants schemas``.

The schemas in ``schemas/`` at the repository root are generated from the models here; a test
fails when they are stale. Use ``--check`` to compare without writing.
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from collicity_grants.models import AdminPolicy, CapabilityGrant, ConnectorManifest

SCHEMAS: dict[str, type[BaseModel]] = {
    "connector-manifest.schema.json": ConnectorManifest,
    "capability-grant.schema.json": CapabilityGrant,
    "admin-policy.schema.json": AdminPolicy,
}


def render(model: type[BaseModel]) -> str:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m collicity_grants")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("schemas", help="write JSON Schemas to a directory")
    cmd.add_argument("directory", type=Path)
    cmd.add_argument("--check", action="store_true", help="fail if any schema is stale")
    args = parser.parse_args()

    stale: list[str] = []
    for name, model in SCHEMAS.items():
        path: Path = args.directory / name
        text = render(model)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    if stale:
        sys.exit(f"stale schemas: {', '.join(stale)}; run `just schemas`")


if __name__ == "__main__":
    main()
