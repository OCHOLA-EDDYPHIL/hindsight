"""Initialize durable checkpoint and chat-history storage for the agent."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.agent import setup_agent_storage  # noqa: E402


def main() -> None:
    setup_agent_storage()
    print("agent persistence storage: ready")


if __name__ == "__main__":
    main()
