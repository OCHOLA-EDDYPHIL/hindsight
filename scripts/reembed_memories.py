"""Build and atomically activate a side-by-side semantic embedding profile."""

from __future__ import annotations

import argparse
import pathlib
import sys
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.embedding_index import (  # noqa: E402
    activate_profile,
    begin_profile_build,
    run_backfill_batch,
)
from hindsight.embeddings import embedding_provider_from_env  # noqa: E402
from hindsight.runtime import runtime_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-distance", type=float)
    parser.add_argument("--no-activate", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    settings = runtime_settings(use_cache=False)
    provider = embedding_provider_from_env(settings.provider_env)
    profile = begin_profile_build(
        provider=provider,
        max_distance=args.max_distance,
        db_url=settings.database_url,
    )
    worker_id = f"reembed-cli:{uuid4()}"
    total = 0
    while True:
        result = run_backfill_batch(
            provider=provider,
            worker_id=worker_id,
            limit=args.batch_size,
            max_distance=args.max_distance,
            db_url=settings.database_url,
        )
        total += result["completed"]
        if result["failed"]:
            raise RuntimeError(f"embedding backfill failed for {result['failed']} memories")
        if result["leased"] == 0:
            break
        print(f"embedding profile: processed {total}")
    if not args.no_activate:
        state = activate_profile(profile_id=str(profile["id"]), db_url=settings.database_url)
        print(f"embedding profile: active generation {state['generation']}")
    else:
        print(f"embedding profile: build complete {profile['id']}")


if __name__ == "__main__":
    main()
