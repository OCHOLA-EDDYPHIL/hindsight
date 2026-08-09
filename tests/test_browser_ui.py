"""Opt-in browser acceptance test for the deployed or local incident cockpit."""

import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from hindsight.demo_state import DEMO_NAMESPACE

BASE_URL = os.environ.get("HINDSIGHT_BROWSER_BASE_URL")
OPERATOR_USERNAME = os.environ.get("HINDSIGHT_OPERATOR_USERNAME")
OPERATOR_PASSWORD = os.environ.get("HINDSIGHT_OPERATOR_PASSWORD")
LOCAL_AUTH_AUTO = os.environ.get("HINDSIGHT_LOCAL_AUTH_AUTO") == "1"

requires_browser = pytest.mark.skipif(
    not BASE_URL or (not LOCAL_AUTH_AUTO and (not OPERATOR_USERNAME or not OPERATOR_PASSWORD)),
    reason="browser URL and operator identity are not configured",
)


def test_reset_resubscribes_before_loading_the_fresh_namespace():
    source = (Path(__file__).parents[1] / "frontend/src/hooks/use-cockpit.ts").read_text()
    reset = source.split("const resetDemo = useCallback", 1)[1].split(
        "const poisonDemo = useCallback", 1
    )[0]

    assert "updateNamespace(payload.namespace);" in reset
    assert "subscribeSocket(payload.namespace);" in reset
    assert reset.index("updateNamespace(payload.namespace);") < reset.index(
        "subscribeSocket(payload.namespace);"
    )
    assert reset.index("subscribeSocket(payload.namespace);") < reset.index("await loadIncidents(")


def test_live_events_from_a_previous_namespace_are_ignored():
    source = (Path(__file__).parents[1] / "frontend/src/hooks/use-cockpit.ts").read_text()
    handler = source.split("const handleLiveEvent = useCallback", 1)[1].split(
        "const subscribeSocket", 1
    )[0]

    namespace_guard = "eventNamespace !== namespaceRef.current"
    tracker_observation = "realtimeTracker.current.observe"
    assert namespace_guard in handler
    assert handler.index(namespace_guard) < handler.index(tracker_observation)
    assert handler.index('snapshotView.current !== "live"') < handler.index(
        tracker_observation
    )


def test_explicit_namespace_renders_before_incident_defaults_are_loaded():
    source = (Path(__file__).parents[1] / "frontend/src/hooks/use-cockpit.ts").read_text()
    startup = source.split("const retryInitialLoad = useCallback", 1)[1].split(
        "useEffect(() =>", 1
    )[0]

    explicit_namespace = startup.split("} else if (explicitNamespace) {", 1)[1].split(
        "} else {", 1
    )[0]
    assert explicit_namespace.index("await loadSnapshot(") < explicit_namespace.index(
        "await loadIncidents(null, false);"
    )


def test_operation_polling_uses_deployed_retry_budget_and_preserves_last_status():
    hook = (Path(__file__).parents[1] / "frontend/src/hooks/use-cockpit.ts").read_text()
    surface = (Path(__file__).parents[1] / "frontend/src/components/cockpit.tsx").read_text()
    polling = hook.split("const waitForOperation = useCallback", 1)[1].split(
        "const executeRewind", 1
    )[0]

    assert "config.operationPollSeconds" in polling
    assert "last status" in polling
    assert polling.index(
        "includes(operation.status)) {\n          return operation;"
    ) < polling.index("const previous = snapshotRef.current;")
    assert "data-operation-id" in surface
    assert "data-operation-type" in surface
    assert "data-operation-status" in surface
    assert "setRewindAnchor(payload.rewind_anchor || null)" in hook


def test_reset_session_readiness_requires_new_namespace_and_known_good_snapshot():
    class Element:
        def __init__(self, text):
            self.text = text

    class Browser:
        def __init__(self, namespace, memory_count):
            self.values = {
                "namespace": Element(namespace),
                "memoryCount": Element(memory_count),
            }

        def find_element(self, _by, value):
            return self.values[value]

    previous = "live-browser:root"
    assert _ready_reset_namespace(Browser(previous, "1 live · 0 invalid"), previous) is False
    assert (
        _ready_reset_namespace(
            Browser("live-browser:root:session:new", "0 live · 0 invalid"),
            previous,
        )
        is False
    )
    assert (
        _ready_reset_namespace(
            Browser("live-browser:root:session:new", "1 live · 0 invalid"),
            previous,
        )
        == "live-browser:root:session:new"
    )


def test_firefox_startup_is_isolated_retried_and_evidenced(monkeypatch, tmp_path):
    from selenium import webdriver
    from selenium.webdriver.firefox import service as service_module

    attempts = []
    services = []
    sleeps = []
    sentinel = object()

    class FakeService:
        def __init__(self, *, log_output):
            self.log_output = log_output
            self.stopped = False
            services.append(self)

        def stop(self):
            self.stopped = True

    def start(*, options, service):
        attempts.append((tuple(options.arguments), service.log_output))
        if len(attempts) == 1:
            raise TimeoutError("session bootstrap timed out")
        return sentinel

    monkeypatch.setenv("HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(webdriver, "Firefox", start)
    monkeypatch.setattr(service_module, "Service", FakeService)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert _start_firefox_driver() is sentinel
    assert [arguments for arguments, _ in attempts] == [
        ("-headless", "-no-remote", "-new-instance"),
        ("-headless", "-no-remote", "-new-instance"),
    ]
    assert [Path(log).name for _, log in attempts] == [
        "geckodriver-attempt-1.log",
        "geckodriver-attempt-2.log",
    ]
    assert services[0].stopped is True
    assert services[1].stopped is False
    assert sleeps == [2]
    evidence = json.loads((tmp_path / "firefox-startup.json").read_text())
    assert evidence == {
        "failures": [
            {
                "attempt": 1,
                "error": "TimeoutError: session bootstrap timed out",
            }
        ]
    }


def test_firefox_uses_virtual_display_without_native_headless(monkeypatch):
    from selenium import webdriver
    from selenium.webdriver.firefox import service as service_module

    attempts = []
    sentinel = object()

    class FakeService:
        def __init__(self, *, log_output):
            self.log_output = log_output

    def start(*, options, service):
        attempts.append(tuple(options.arguments))
        return sentinel

    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(webdriver, "Firefox", start)
    monkeypatch.setattr(service_module, "Service", FakeService)

    assert _start_firefox_driver() is sentinel
    assert attempts == [("-no-remote", "-new-instance")]


def test_firefox_uses_remote_isolated_service_when_configured(monkeypatch, tmp_path):
    from selenium import webdriver

    attempts = []
    sleeps = []
    sentinel = object()

    def start(*, command_executor, options):
        attempts.append((command_executor, tuple(options.arguments)))
        if len(attempts) == 1:
            raise TimeoutError("remote session bootstrap timed out")
        return sentinel

    monkeypatch.setenv("HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("HINDSIGHT_SELENIUM_REMOTE_URL", "http://127.0.0.1:4444")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(webdriver, "Remote", start)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert _start_firefox_driver() is sentinel
    assert attempts == [
        ("http://127.0.0.1:4444", ("-no-remote", "-new-instance")),
        ("http://127.0.0.1:4444", ("-no-remote", "-new-instance")),
    ]
    assert sleeps == [2]
    evidence = json.loads((tmp_path / "firefox-startup.json").read_text())
    assert evidence["failures"][0]["error"].startswith("TimeoutError:")


@requires_browser
def test_operator_can_run_and_explain_signature_workflow():
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as expected
    from selenium.webdriver.support.ui import WebDriverWait

    driver = _start_firefox_driver()
    driver.set_script_timeout(120)
    wait = WebDriverWait(driver, 90)
    operation_id = None
    signature = None
    try:
        driver.set_window_size(1440, 1000)
        driver.get(_browser_url(namespace=DEMO_NAMESPACE))
        wait.until(expected.presence_of_element_located((By.ID, "memories")))
        _capture_console_errors(driver)
        wait.until(lambda browser: "Live" in browser.find_element(By.ID, "connection").text)
        assert not driver.find_elements(By.ID, "startRun")

        driver.find_element(By.ID, "identityButton").click()
        driver.find_element(By.ID, "identitySignIn").click()
        _complete_operator_sign_in(driver, wait)
        wait.until(expected.presence_of_element_located((By.ID, "startRun")))
        wait.until_not(
            lambda browser: browser.find_element(By.ID, "startRun").get_attribute("disabled")
        )

        walkthrough = driver.find_element(By.ID, "operatorWalkthrough")
        assert not walkthrough.is_displayed()
        driver.find_element(By.ID, "walkthroughToggle").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "operatorWalkthrough").is_displayed()
        )
        assert "Reset the replay" in walkthrough.text
        assert "Inspect history" in walkthrough.text
        driver.find_element(By.ID, "walkthroughToggle").click()
        wait.until_not(
            lambda browser: browser.find_element(By.ID, "operatorWalkthrough").is_displayed()
        )

        previous_namespace = driver.find_element(By.ID, "namespace").text
        driver.find_element(By.ID, "resetDemo").click()
        namespace = wait.until(lambda browser: _ready_reset_namespace(browser, previous_namespace))

        driver.find_element(By.ID, "poisonDemo").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text == "2 live · 0 invalid"
        )

        driver.find_element(By.ID, "startRun").click()
        wait.until(
            lambda browser: "awaiting approval" in browser.find_element(By.ID, "runStatus").text
        )
        wait.until(
            lambda browser: (
                "demo.fixture-import" in browser.find_element(By.ID, "influenceList").text
            )
        )
        bad_action = driver.find_element(By.ID, "proposedAction").text.strip()
        assert bad_action
        assert "Recommendation" in driver.find_element(By.CSS_SELECTOR, ".action-execution").text
        assert not driver.find_elements(By.CSS_SELECTOR, ".action-score")
        driver.find_element(By.ID, "rejectRun").click()
        wait.until(lambda browser: browser.find_element(By.ID, "runStatus").text == "rejected")
        assert not driver.find_elements(By.CSS_SELECTOR, ".action-score")
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text == "3 live · 0 invalid"
        )

        driver.find_element(By.ID, "previewRewind").click()
        wait.until(
            lambda browser: (
                "versions will close" in browser.find_element(By.ID, "rewindPreview").text
            )
        )
        driver.find_element(By.ID, "executeRewind").click()
        operation = wait.until(
            expected.presence_of_element_located(
                (By.CSS_SELECTOR, '#operations [data-operation-type="rewind"]')
            )
        )
        operation_id = operation.get_attribute("data-operation-id")
        assert operation_id
        operation_poll_seconds = driver.execute_script(
            "return Number(window.HINDSIGHT_CONFIG.operationPollSeconds || 600);"
        )
        _wait_for_completed_operation(driver, timeout=float(operation_poll_seconds) + 30)
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text == "1 live · 2 invalid"
        )
        wait.until(
            lambda browser: browser.find_element(By.ID, "executeRewind").text == "Execute rewind"
        )

        timeline = driver.find_element(By.ID, "timeline")
        assert not timeline.get_attribute("disabled")
        driver.execute_script(
            "arguments[0].value = 0; arguments[0].dispatchEvent(new Event('input'));",
            timeline,
        )
        wait.until(
            lambda browser: browser.find_element(By.ID, "beliefTitle").text == "Beliefs As Of"
        )
        wait.until(lambda browser: "0 invalid" in browser.find_element(By.ID, "memoryCount").text)
        assert not driver.find_elements(By.CSS_SELECTOR, ".memory.invalidated")

        driver.find_element(By.ID, "liveButton").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "beliefTitle").text == "Current Beliefs"
        )
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text == "1 live · 2 invalid"
        )

        driver.find_element(By.ID, "startRun").click()
        wait.until(
            lambda browser: "awaiting approval" in browser.find_element(By.ID, "runStatus").text
        )
        wait.until(lambda browser: "1 read" in browser.find_element(By.ID, "influenceCount").text)
        assert "demo.fixture-import" not in driver.find_element(By.ID, "influenceList").text
        corrected_action = driver.find_element(By.ID, "proposedAction").text.strip()
        assert corrected_action
        assert corrected_action != bad_action
        assert not driver.find_elements(By.CSS_SELECTOR, ".action-score")
        driver.find_element(By.ID, "approveRun").click()
        wait.until(lambda browser: browser.find_element(By.ID, "runStatus").text == "completed")
        assert not driver.find_elements(By.CSS_SELECTOR, ".action-score")
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text == "2 live · 2 invalid"
        )
        _assert_typed_reflection(namespace)
        signature = _assert_signature_trace(namespace=namespace, operation_id=operation_id)
        assert driver.execute_script("return window.__HINDSIGHT_CONSOLE_ERRORS || [];") == []
        assert driver.execute_script("return window.__HINDSIGHT_VISIBLE_ERRORS || [];") == []

        driver.find_element(By.ID, "signOutButton").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "identityLabel").text == "Sign in"
        )
        driver.get(_public_browser_url())
        wait.until(expected.presence_of_element_located((By.ID, "memories")))
        _capture_console_errors(driver)
        wait.until(
            lambda browser: (
                "resolved"
                in browser.find_element(By.CSS_SELECTOR, '[data-stage="influenced_decision_id"]')
                .get_attribute("class")
                .split()
            )
        )
        historical_evidence = driver.find_element(
            By.CSS_SELECTOR, ".outcome-historical .decision-citations"
        ).text
        current_evidence = driver.find_element(
            By.CSS_SELECTOR, ".outcome-current .decision-citations"
        ).text
        assert "demo.fixture-import" in historical_evidence
        assert "demo:stale-runbook-import" in historical_evidence
        assert "previously approved payment runbook" in historical_evidence
        assert "demo.seed" in current_evidence
        assert not driver.find_elements(By.CSS_SELECTOR, ".operator-console")
        assert driver.execute_script("return window.__HINDSIGHT_CONSOLE_ERRORS || [];") == []
        assert driver.execute_script("return window.__HINDSIGHT_VISIBLE_ERRORS || [];") == []
    finally:
        _write_browser_evidence(driver, operation_id=operation_id, signature=signature)
        driver.quit()


def _complete_operator_sign_in(driver, wait) -> None:
    from selenium.webdriver.common.by import By

    if not LOCAL_AUTH_AUTO:
        username = wait.until(
            lambda browser: _first_displayed(
                browser,
                By.CSS_SELECTOR,
                'input[name="username"], #signInFormUsername, input[type="email"]',
            )
        )
        password = wait.until(
            lambda browser: _first_displayed(
                browser,
                By.CSS_SELECTOR,
                'input[name="password"], #signInFormPassword, input[type="password"]',
            )
        )
        username.clear()
        username.send_keys(OPERATOR_USERNAME)
        password.send_keys(OPERATOR_PASSWORD)
        submit = wait.until(
            lambda browser: _first_displayed(
                browser,
                By.CSS_SELECTOR,
                'button[type="submit"], input[type="submit"]',
            )
        )
        submit.click()
    wait.until(lambda browser: browser.find_element(By.ID, "identityLabel").text == "Operator")


def _first_displayed(driver, by, selector):
    for element in driver.find_elements(by, selector):
        if element.is_displayed():
            return element
    return False


def _ready_reset_namespace(driver, previous_namespace: str) -> str | bool:
    from selenium.webdriver.common.by import By

    namespace = driver.find_element(By.ID, "namespace").text
    memory_count = driver.find_element(By.ID, "memoryCount").text
    if namespace == previous_namespace or "1 live" not in memory_count:
        return False
    return namespace


def _wait_for_completed_operation(driver, *, timeout: float) -> None:
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    observed = ""

    def completed(browser):
        nonlocal observed
        observed = browser.find_element(By.ID, "operations").text
        if " · conflict" in observed or " · failed" in observed:
            raise AssertionError(f"rewind reached a non-success terminal state: {observed}")
        return " · completed" in observed

    try:
        WebDriverWait(driver, timeout).until(completed)
    except TimeoutException as exc:
        raise AssertionError(
            f"rewind did not complete before its retry budget: {observed}"
        ) from exc


def _capture_console_errors(driver) -> None:
    driver.execute_script(
        """
        window.__HINDSIGHT_CONSOLE_ERRORS = [];
        window.__HINDSIGHT_VISIBLE_ERRORS = [];
        const record = (kind, value) => {
          window.__HINDSIGHT_CONSOLE_ERRORS.push({kind, value: String(value)});
        };
        window.addEventListener("error", (event) => record("error", event.message));
        window.addEventListener("unhandledrejection", (event) => record("rejection", event.reason));
        const originalError = console.error.bind(console);
        console.error = (...values) => {
          record("console.error", values.join(" "));
          originalError(...values);
        };
        const notice = document.getElementById("notice");
        new MutationObserver(() => {
          if (!notice.hidden && notice.classList.contains("error")) {
            window.__HINDSIGHT_VISIBLE_ERRORS.push(notice.textContent);
          }
        }).observe(notice, {attributes: true, childList: true, subtree: true});
        """
    )


def _write_browser_evidence(
    driver,
    *,
    operation_id: str | None,
    signature: dict | None,
) -> None:
    from selenium.webdriver.common.by import By

    directory_value = (os.environ.get("HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR") or "").strip()
    if not directory_value:
        return
    directory = Path(directory_value)
    directory.mkdir(parents=True, exist_ok=True)
    capture_errors = []
    try:
        driver.save_screenshot(str(directory / "operator-workflow.png"))
    except Exception as exc:  # noqa: BLE001 - evidence capture must not mask the test failure
        capture_errors.append(f"screenshot: {type(exc).__name__}: {exc}")
    try:
        console_errors = driver.execute_script("return window.__HINDSIGHT_CONSOLE_ERRORS || [];")
    except Exception as exc:  # noqa: BLE001 - evidence capture must not mask the test failure
        capture_errors.append(f"console: {type(exc).__name__}: {exc}")
        console_errors = []
    (directory / "browser-console.json").write_text(
        json.dumps(console_errors, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    observed = []
    try:
        for element in driver.find_elements(By.CSS_SELECTOR, "#operations [data-operation-id]"):
            observed.append(
                {
                    "id": element.get_attribute("data-operation-id"),
                    "status": element.get_attribute("data-operation-status"),
                    "text": element.text,
                }
            )
    except Exception as exc:  # noqa: BLE001 - evidence capture must not mask the test failure
        capture_errors.append(f"operations: {type(exc).__name__}: {exc}")
    persisted = None
    if operation_id and os.environ.get("DATABASE_URL"):
        from hindsight.operations import get_operation

        try:
            persisted = get_operation(operation_id=operation_id)
        except Exception as exc:  # noqa: BLE001 - evidence capture must not mask the test failure
            capture_errors.append(f"database: {type(exc).__name__}: {exc}")
    (directory / "operation.json").write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "observed": observed,
                "persisted": persisted,
                "signature": signature,
                "capture_errors": capture_errors,
            },
            default=str,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _assert_signature_trace(*, namespace: str, operation_id: str) -> dict:
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore

    with connect(database_url(), application_name="hindsight-browser-signature") as conn:
        run_rows = conn.execute(
            """
                SELECT run.id, run.decision_id, run.status, run.plan, run.proposed_action,
                       run.reflected_memory_id, read.id, read.memory_id,
                       memory.writer, read.rank, read.distance,
                       memory.source_ref, memory.justification,
                       (
                           SELECT count(*) FROM memory_lineage_edges AS edge
                           WHERE edge.parent_read_id = read.id
                       ) AS downstream_lineage_edges,
                       run.provider, run.model
                FROM agent_runs AS run
                LEFT JOIN memory_reads AS read ON read.decision_id = run.decision_id
                LEFT JOIN semantic_memories AS memory ON memory.id = read.semantic_memory_id
                WHERE run.namespace = %s
                ORDER BY run.created_at, read.rank
            """,
            (namespace,),
        ).fetchall()
        event_rows = conn.execute(
            """
                SELECT event.run_id, event.metadata
                FROM agent_run_events AS event
                JOIN agent_runs AS run ON run.id = event.run_id
                WHERE run.namespace = %s
                ORDER BY event.run_id, event.sequence
            """,
            (namespace,),
        ).fetchall()
        operation = conn.execute(
            """
                SELECT invalidated_memory_ids, restored_memory_ids, status
                FROM memory_operations
                WHERE id = %s
            """,
            (operation_id,),
        ).fetchone()

    grouped: dict[str, dict] = {}
    for row in run_rows:
        run_id = str(row[0])
        run = grouped.setdefault(
            run_id,
            {
                "run_id": run_id,
                "decision_id": row[1],
                "status": row[2],
                "plan": row[3],
                "proposed_action": row[4],
                "reflected_memory_id": str(row[5]) if row[5] else None,
                "provider": row[14],
                "model": row[15],
                "reads": [],
            },
        )
        if row[6] is not None:
            run["reads"].append(
                {
                    "read_id": str(row[6]),
                    "memory_id": str(row[7]),
                    "writer": row[8],
                    "rank": row[9],
                    "distance": row[10],
                    "source_ref": row[11],
                    "justification": row[12],
                    "downstream_lineage_edges": row[13],
                }
            )
    for run_id, metadata in event_rows:
        action_trace = (metadata or {}).get("action_trace")
        if action_trace and str(run_id) in grouped:
            grouped[str(run_id)]["action_trace"] = action_trace

    runs = list(grouped.values())
    assert len(runs) == 2
    bad, corrected = runs
    assert bad["status"] == "rejected"
    assert bad["provider"] == "gemini"
    assert bad["model"]
    assert bad["action_trace"]["mode"] == "recommendation_only"
    assert bad["action_trace"]["approval"]["approved"] is False
    assert bad["action_trace"]["execution"]["status"] == "not_executed"
    assert bad["action_trace"]["recommendation"]["id"].startswith("recommendation:")
    assert bad["action_trace"]["selection"]["fingerprint"]
    assert "score" not in bad["action_trace"]
    poison_read = next(read for read in bad["reads"] if read["writer"] == "demo.fixture-import")
    assert poison_read["source_ref"] == "demo:stale-runbook-import"
    assert poison_read["justification"]
    assert poison_read["downstream_lineage_edges"] >= 1
    assert corrected["status"] == "completed"
    assert corrected["provider"] == "gemini"
    assert corrected["model"]
    assert corrected["plan"]
    assert corrected["proposed_action"]
    assert corrected["proposed_action"] != bad["proposed_action"]
    assert corrected["action_trace"]["mode"] == "recommendation_only"
    assert corrected["action_trace"]["approval"]["approved"] is True
    assert corrected["action_trace"]["execution"]["status"] == "recommendation_approved"
    assert corrected["action_trace"]["recommendation"]["id"].startswith("recommendation:")
    assert (
        corrected["action_trace"]["recommendation"]["id"]
        != bad["action_trace"]["recommendation"]["id"]
    )
    assert (
        corrected["action_trace"]["selection"]["fingerprint"]
        != bad["action_trace"]["selection"]["fingerprint"]
    )
    assert "score" not in corrected["action_trace"]
    for run in (bad, corrected):
        assert 1 <= len(run["action_trace"]["tool_calls"]) <= 3
        assert len(run["action_trace"]["reasoning_steps"]) <= 4
        assert all(
            call["tool"] == "aws_cloudwatch_diagnostics"
            for call in run["action_trace"]["tool_calls"]
        )
        assert any(
            call["status"] == "completed"
            for call in run["action_trace"]["tool_calls"]
        )
        assert any(
            observation.get("status") == "available"
            and int(observation.get("datapoint_count") or 0) > 0
            for observation in run["action_trace"]["observations"]
        )
    assert any(read["writer"] == "demo.seed" for read in corrected["reads"])
    assert all(read["writer"] != "demo.fixture-import" for read in corrected["reads"])
    assert operation is not None and operation[2] == "completed"

    with MemoryStore(url=database_url()) as store:
        memories = store.list_current_semantic(namespace=namespace, limit=100)
        current_writers = {memory["writer"] for memory in memories}
        poison_id = next(
            read["memory_id"] for read in bad["reads"] if read["writer"] == "demo.fixture-import"
        )
        poison = store.audit_memory(memory_kind="semantic", memory_id=poison_id)

    assert "demo.seed" in current_writers
    assert poison is not None and poison["t_invalid"] is not None
    invalidated = {str(value) for value in operation[0]}
    assert poison_id in invalidated
    assert bad["reflected_memory_id"] in invalidated
    assert all(read["memory_id"] not in invalidated for read in corrected["reads"])
    return {
        "namespace": namespace,
        "operation_id": operation_id,
        "invalidated_memory_ids": sorted(invalidated),
        "bad": bad,
        "corrected": corrected,
    }


@requires_browser
def test_review_required_memory_renders_as_active_in_its_historical_snapshot():
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as expected
    from selenium.webdriver.support.ui import WebDriverWait

    namespace, cutoff = _prepare_review_required_fixture()
    driver = _start_firefox_driver()
    wait = WebDriverWait(driver, 90)
    try:
        driver.set_window_size(1440, 1000)
        driver.get(_browser_url(namespace=namespace))
        wait.until(expected.presence_of_element_located((By.ID, "memories")))
        wait.until(
            lambda browser: (
                browser.find_element(By.CSS_SELECTOR, ".memory-status").text == "review required"
            )
        )
        assert "1 live · 0 invalid" in driver.find_element(By.ID, "memoryCount").text

        driver.get(_browser_url(namespace=namespace, as_of=cutoff))
        wait.until(
            lambda browser: browser.find_element(By.ID, "beliefTitle").text == "Beliefs As Of"
        )
        wait.until(
            lambda browser: (
                browser.find_element(By.CSS_SELECTOR, ".memory-status").text == "current"
            )
        )
        assert "review required" not in driver.find_element(By.ID, "memories").text
        assert not driver.find_elements(By.CSS_SELECTOR, ".memory.invalidated")
    finally:
        driver.quit()


def _start_firefox_driver():
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    remote_url = (os.environ.get("HINDSIGHT_SELENIUM_REMOTE_URL") or "").strip()
    failures = []
    for attempt in range(1, 3):
        options = Options()
        arguments = ["-no-remote", "-new-instance"]
        if not os.environ.get("DISPLAY") and not remote_url:
            arguments.insert(0, "-headless")
        for argument in arguments:
            options.add_argument(argument)
        service = None
        try:
            if remote_url:
                driver = webdriver.Remote(command_executor=remote_url, options=options)
            else:
                service = Service(log_output=_geckodriver_log_path(attempt))
                driver = webdriver.Firefox(options=options, service=service)
        except Exception as exc:  # noqa: BLE001 - retry only wraps session bootstrap
            failures.append(
                {
                    "attempt": attempt,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if service is not None:
                try:
                    service.stop()
                except Exception as stop_exc:  # noqa: BLE001 - preserve startup failure
                    failures[-1]["cleanup_error"] = f"{type(stop_exc).__name__}: {stop_exc}"
            _write_firefox_startup_evidence(failures)
            if attempt == 2:
                raise
            time.sleep(2)
        else:
            _write_firefox_startup_evidence(failures)
            return driver
    raise AssertionError("Firefox driver startup exhausted its bounded retry")


def _geckodriver_log_path(attempt: int) -> str | None:
    directory = _browser_evidence_directory()
    if directory is None:
        return None
    return str(directory / f"geckodriver-attempt-{attempt}.log")


def _write_firefox_startup_evidence(failures: list[dict]) -> None:
    directory = _browser_evidence_directory()
    if directory is None:
        return
    (directory / "firefox-startup.json").write_text(
        json.dumps({"failures": failures}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _browser_evidence_directory() -> Path | None:
    directory_value = (os.environ.get("HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR") or "").strip()
    if not directory_value:
        return None
    directory = Path(directory_value)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _assert_typed_reflection(namespace: str) -> None:
    from hindsight.db import connect, database_url

    with connect(database_url(), application_name="hindsight-browser-acceptance") as conn:
        row = conn.execute(
            """
                SELECT run.decision_id, run.plan, run.proposed_action,
                       run.reflected_memory_id, reflection.plan,
                       reflection.proposed_action, reflection.action_approved,
                       reflection.semantic_memory_id, memory.content_schema,
                       memory.structured_payload, reflection.decision_id,
                       memory.producer_decision_id
                FROM agent_runs AS run
                JOIN agent_reflections AS reflection ON reflection.run_id = run.id
                JOIN semantic_memories AS memory
                    ON memory.id = reflection.semantic_memory_id
                WHERE run.namespace = %s AND run.status = 'completed'
                ORDER BY run.created_at DESC
                LIMIT 1
            """,
            (namespace,),
        ).fetchone()

    assert row is not None
    assert row[1] and row[1] == row[4]
    assert row[2] and row[2] == row[5]
    assert row[6] is True
    assert str(row[3]) == str(row[7])
    assert row[8] == "agent_reflection.v1"
    payload = dict(row[9])
    assert row[10] == row[0]
    assert row[11] == row[0]
    assert payload["plan"] == row[1]
    assert payload["proposed_action"] == row[2]
    assert payload["action_approved"] is True


def _prepare_review_required_fixture() -> tuple[str, str]:
    from hindsight.db import connect, database_url
    from hindsight.embeddings import embedding_provider_from_env
    from hindsight.gemini import gemini_pool_from_env
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import enqueue_operation, execute_operation, preview_supersession
    from hindsight.runtime import runtime_settings

    settings = runtime_settings(use_cache=False)
    pool = gemini_pool_from_env(settings.provider_env)
    provider = embedding_provider_from_env(settings.provider_env, gemini_pool=pool)
    token = uuid4().hex
    root_namespace = f"live-review-root:{token}"
    child_namespace = f"live-review-child:{token}"
    decision_id = f"live-review:{token}"
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        root = store.remember(
            memory_kind="semantic",
            namespace=root_namespace,
            content="Processor timeout policy begins remediation at twenty percent.",
            provenance=Provenance(
                "live.browser_acceptance",
                f"policy:{token}",
                "Seed the governed evolution fixture",
            ),
        )
        store.open_decision(
            decision_id=decision_id,
            actor="live.browser_acceptance",
            decision_kind="policy_derivation",
            purpose="Derive a remediation from the policy",
            namespace=child_namespace,
        )
        store.record_read(
            decision_id=decision_id,
            memory_kind="semantic",
            memory_id=str(root["id"]),
            reader="live.browser_acceptance",
            purpose="Derive a remediation from the policy",
        )
        child = store.remember(
            memory_kind="semantic",
            namespace=child_namespace,
            content="Scale retry workers when the processor threshold is reached.",
            provenance=Provenance(
                "live.browser_acceptance",
                f"derived:{token}",
                "Create a descendant requiring review after policy evolution",
            ),
            producer_decision_id=decision_id,
            parent_memory_ids=[str(root["id"])],
        )
    with connect(database_url(), application_name="hindsight-browser-acceptance") as conn:
        producer_status = conn.execute(
            "SELECT status FROM memory_decisions WHERE id = %s",
            (decision_id,),
        ).fetchone()[0]
        cutoff = conn.execute("SELECT now()").fetchone()[0]
    assert producer_status == "sealed"

    preview = preview_supersession(
        root_memory_id=str(root["id"]),
        intent="evolution",
        content="Processor timeout policy begins remediation at ten percent.",
        structured_payload={"threshold_percent": 10},
        actor="live.browser_acceptance",
        reason="Exercise review-required historical rendering",
        authorized_namespaces=[root_namespace, child_namespace],
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=preview["fingerprint"],
        idempotency_key=f"live-review:{token}",
        db_url=database_url(),
    )
    result = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="live-browser-acceptance",
        db_url=database_url(),
    )
    assert result["status"] == "completed"
    with MemoryStore(url=database_url()) as store:
        reviewed = store.audit_memory(memory_kind="semantic", memory_id=str(child["id"]))
    assert reviewed is not None and reviewed["trust_status"] == "review_required"
    return child_namespace, cutoff.isoformat()


def _browser_url(*, namespace: str, as_of: str | None = None) -> str:
    parts = urlsplit(BASE_URL)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["namespace"] = namespace
    if as_of is None:
        query.pop("as_of", None)
    else:
        query["as_of"] = as_of
    return urlunsplit(parts._replace(query=urlencode(query)))


def _public_browser_url() -> str:
    parts = urlsplit(BASE_URL)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.pop("namespace", None)
    query.pop("as_of", None)
    return urlunsplit(parts._replace(query=urlencode(query)))
