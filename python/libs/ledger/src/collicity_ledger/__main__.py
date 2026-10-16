"""Price catalog operations: ``python -m collicity_ledger publish-prices <file>``.

Publishes a catalog file as a new immutable price version, as migrator. Reads
COLLICITY_MIGRATOR_DATABASE_URL. Use ``--check`` to validate the file without publishing.
"""

import argparse
import json
import os
from pathlib import Path

from collicity_db import make_engine
from collicity_ledger.catalog import catalog_rows, publish


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m collicity_ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    publish_cmd = sub.add_parser("publish-prices", help="publish a price catalog file")
    publish_cmd.add_argument("file", type=Path)
    publish_cmd.add_argument("--check", action="store_true", help="validate only")
    args = parser.parse_args()

    document = json.loads(args.file.read_text(encoding="utf-8"))
    rows = catalog_rows(document)
    if args.check:
        print(f"{args.file}: {len(rows)} prices, valid")  # noqa: T201
        return

    url = os.environ.get("COLLICITY_MIGRATOR_DATABASE_URL")
    if not url:
        raise SystemExit("COLLICITY_MIGRATOR_DATABASE_URL is not set")
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            version_id = publish(conn, document)
    finally:
        engine.dispose()
    print(f"published price version {document['label']} as {version_id}")  # noqa: T201


if __name__ == "__main__":
    main()
