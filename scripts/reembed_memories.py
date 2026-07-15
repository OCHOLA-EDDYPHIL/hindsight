"""Build and atomically activate a side-by-side semantic embedding profile."""

from __future__ import annotations

import argparse
import pathlib
import sys
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.embedding_index import (  # noqa: E402
    EmbeddingCoverageError,
    activate_profile,
    begin_profile_build,
    run_backfill_batch,
)
from hindsight.embeddings import EmbeddingProvider, embedding_provider_from_env  # noqa: E402
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
    if profile["status"] == "active":
        if args.no_activate:
            print(f"embedding profile: build complete {profile['id']}")
        else:
            state = activate_profile(
                profile_id=str(profile["id"]),
                db_url=settings.database_url,
            )
            print(f"embedding profile: active generation {state['generation']}")
        return
    worker_id = f"reembed-cli:{uuid4()}"
    total, _ = _drain_backfill(
        provider=provider,
        worker_id=worker_id,
        batch_size=args.batch_size,
        max_distance=args.max_distance,
        db_url=settings.database_url,
    )
    if not args.no_activate:
        while True:
            try:
                state = activate_profile(
                    profile_id=str(profile["id"]),
                    db_url=settings.database_url,
                )
                break
            except EmbeddingCoverageError as activation_error:
                refreshed = begin_profile_build(
                    provider=provider,
                    max_distance=args.max_distance,
                    db_url=settings.database_url,
                )
                if refreshed["status"] == "active":
                    state = activate_profile(
                        profile_id=str(refreshed["id"]),
                        db_url=settings.database_url,
                    )
                    break
                try:
                    first_result = run_backfill_batch(
                        provider=provider,
                        worker_id=worker_id,
                        limit=args.batch_size,
                        max_distance=args.max_distance,
                        db_url=settings.database_url,
                    )
                except EmbeddingCoverageError:
                    raise activation_error
                if first_result["leased"] == 0:
                    raise activation_error
                total, _ = _drain_backfill(
                    provider=provider,
                    worker_id=worker_id,
                    batch_size=args.batch_size,
                    max_distance=args.max_distance,
                    db_url=settings.database_url,
                    total=total,
                    first_result=first_result,
                )
        print(f"embedding profile: active generation {state['generation']}")
    else:
        print(f"embedding profile: build complete {profile['id']}")


def _drain_backfill(
    *,
    provider: EmbeddingProvider,
    worker_id: str,
    batch_size: int,
    max_distance: float | None,
    db_url: str,
    total: int = 0,
    first_result: dict[str, int] | None = None,
) -> tuple[int, bool]:
    leased_any = False
    result = first_result
    while True:
        if result is None:
            result = run_backfill_batch(
                provider=provider,
                worker_id=worker_id,
                limit=batch_size,
                max_distance=max_distance,
                db_url=db_url,
            )
        if result["failed"]:
            raise RuntimeError(f"embedding backfill failed for {result['failed']} memories")
        if result["leased"] == 0:
            return total, leased_any
        leased_any = True
        total += result["completed"]
        print(f"embedding profile: processed {total}")
        result = None


if __name__ == "__main__":
    main()
