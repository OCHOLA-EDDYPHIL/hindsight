import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_ci_components", ROOT / "scripts/verify_ci_components.py"
)
assert SPEC is not None and SPEC.loader is not None
aggregate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aggregate)


def _selections(value: str = "true") -> dict[str, str]:
    return {selection: value for selection in set(aggregate.JOB_SELECTIONS.values())}


def _results(value: str = "success") -> dict[str, str]:
    return {
        **{job: value for job in aggregate.ALWAYS_REQUIRED},
        **{job: value for job in aggregate.JOB_SELECTIONS},
    }


def test_main_requires_every_selected_component_successful():
    assert aggregate.verify(
        event_name="push", selections=_selections(), results=_results()
    ) == []

    results = _results()
    results["main_qualification"] = "skipped"
    assert aggregate.verify(
        event_name="push", selections=_selections(), results=results
    ) == ["selected job main_qualification ended as skipped"]


def test_pull_request_accepts_only_explicitly_disabled_skips():
    selections = _selections()
    selections["frontend"] = "false"
    results = _results()
    results["frontend"] = "skipped"

    assert aggregate.verify(
        event_name="pull_request", selections=selections, results=results
    ) == []

    results["database"] = "skipped"
    assert aggregate.verify(
        event_name="pull_request", selections=selections, results=results
    ) == ["selected job database ended as skipped"]


def test_failures_cancellations_missing_results_and_invalid_selections_fail_closed():
    for result in ("failure", "cancelled", "missing"):
        results = _results()
        if result == "missing":
            results.pop("terraform")
        else:
            results["terraform"] = result
        assert aggregate.verify(
            event_name="pull_request", selections=_selections(), results=results
        )

    selections = _selections()
    selections["database"] = "unexpected"
    assert aggregate.verify(
        event_name="pull_request", selections=selections, results=_results()
    )


def test_always_required_jobs_cannot_be_skipped():
    results = _results()
    results["changes"] = "skipped"

    assert aggregate.verify(
        event_name="pull_request", selections=_selections("false"), results=results
    )[0] == "required job changes ended as skipped"


def test_documentation_only_accepts_every_component_skipped():
    selections = _selections("false")
    results = _results("skipped")
    results["changes"] = "success"

    assert aggregate.verify(
        event_name="push", selections=selections, results=results
    ) == []


def test_missing_or_extra_contract_keys_fail_closed():
    selections = _selections()
    results = _results()
    selections.pop("terraform")
    results["unexpected"] = "success"

    errors = aggregate.verify(
        event_name="pull_request", selections=selections, results=results
    )

    assert any("component selection keys differ" in error for error in errors)
    assert any("job result keys differ" in error for error in errors)
