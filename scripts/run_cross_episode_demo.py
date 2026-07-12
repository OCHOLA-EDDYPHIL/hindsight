"""Run the M4 cross-episode learning demo."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.cross_episode import CROSS_EPISODE_NAMESPACE, run_cross_episode_demo  # noqa: E402


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=CROSS_EPISODE_NAMESPACE)
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--keep-existing", action="store_true")
    args = parser.parse_args()

    result = run_cross_episode_demo(
        namespace=args.namespace,
        db_url=args.db_url,
        keep_existing=args.keep_existing,
    )
    payload = _jsonable(result)
    payload["comparison"] = {
        "episode_one_steps": result.episode_one.steps_to_resolution,
        "episode_two_steps": result.episode_two.steps_to_resolution,
        "steps_saved": result.steps_saved,
        "improvement_ratio": result.improvement_ratio,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    main()
