"""Opt-in browser acceptance test for the deployed or local incident cockpit."""

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from hindsight.demo_state import COMPROMISED_GUIDANCE_CONTENT, DEMO_NAMESPACE

BASE_URL = os.environ.get("HINDSIGHT_BROWSER_BASE_URL")
OPERATOR_USERNAME = os.environ.get("HINDSIGHT_OPERATOR_USERNAME")
OPERATOR_PASSWORD = os.environ.get("HINDSIGHT_OPERATOR_PASSWORD")
LOCAL_AUTH_AUTO = os.environ.get("HINDSIGHT_LOCAL_AUTH_AUTO") == "1"
EXPECTED_DEPLOYED_REVISION = os.environ.get("HINDSIGHT_EXPECTED_DEPLOYED_REVISION")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID")

requires_browser = pytest.mark.skipif(
    not BASE_URL or (not LOCAL_AUTH_AUTO and (not OPERATOR_USERNAME or not OPERATOR_PASSWORD)),
    reason="browser URL and operator identity are not configured",
)

requires_hosted_remediation = pytest.mark.skipif(
    os.environ.get("RUN_HOSTED_ACCEPTANCE") != "1"
    or not BASE_URL
    or not OPERATOR_USERNAME
    or not OPERATOR_PASSWORD
    or not EXPECTED_DEPLOYED_REVISION
    or not GITHUB_RUN_ID,
    reason="hosted browser workflow identity and operator credentials are not configured",
)

REMEDIATION_REPORT = (
    "Controlled payments incident: current payments.retry_fanout is elevated. Inspect the "
    "current CloudWatch telemetry. It contradicts the recalled legacy guidance that says to "
    "scale payment workers while processor timeouts and retry fanout remain elevated. Remove "
    "that unsafe recalled guidance from governed memory with a verbatim citation and bounded "
    "operator review."
)
REMEDIATION_REPORT_CONTRACT_VERSION = 1
REMEDIATION_REPORT_SHA256 = hashlib.sha256(REMEDIATION_REPORT.encode("utf-8")).hexdigest()


def test_reset_installs_exact_replay_identity_before_loading_fresh_state():
    source = (Path(__file__).parents[1] / "frontend/src/hooks/use-cockpit.ts").read_text()
    reset = source.split("const resetDemo = useCallback", 1)[1].split(
        "const poisonDemo = useCallback", 1
    )[0]

    assert "updateNamespace(payload.namespace, false);" in reset
    assert "subscribeSocket(payload.namespace);" in reset
    assert "scenarioId: payload.scenario_id" in reset
    assert 'historyMode: "push"' in reset
    assert reset.index("updateNamespace(payload.namespace, false);") < reset.index(
        "subscribeSocket(payload.namespace);"
    )
    assert reset.index("subscribeSocket(payload.namespace);") < reset.index("await loadIncidents(")
    assert reset.index("await loadIncidents(") < reset.index("await loadScenario(")


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


def test_explicit_replay_identity_renders_before_incident_defaults_are_loaded():
    source = (Path(__file__).parents[1] / "frontend/src/hooks/use-cockpit.ts").read_text()
    startup = source.split("const retryInitialLoad = useCallback", 1)[1].split(
        "useEffect(() =>", 1
    )[0]

    exact_scenario = startup.split(
        "} else if (hasScenario && location.scenarioId) {", 1
    )[1].split("} else if (hasNamespace) {", 1)[0]
    explicit_namespace = startup.split("} else if (hasNamespace) {", 1)[1].split(
        "} else {", 1
    )[0]

    assert "scenarioId: location.scenarioId" in exact_scenario
    assert "void loadIncidents(null, false);" in exact_scenario
    assert "namespace: location.namespace" in explicit_namespace
    assert "void loadIncidents(null, false);" in explicit_namespace
    assert "await loadSnapshot(location.asOf, location.namespace);" in explicit_namespace
    assert "explicitNamespace" not in startup


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


def test_browser_operation_receipt_excludes_unrestricted_payloads():
    secret = "unrestricted-model-or-credential-material"

    def run(status: str, approved: bool) -> dict[str, Any]:
        return {
            "run_id": f"run-{status}",
            "decision_id": f"decision-{status}",
            "status": status,
            "plan": secret,
            "proposed_action": secret,
            "reflected_memory_id": None,
            "reads": [
                {
                    "memory_id": "memory-1",
                    "downstream_lineage_edges": 1,
                    "justification": secret,
                }
            ],
            "action_trace": {
                "selection": {"fingerprint": "a" * 64},
                "recommendation": {"id": f"recommendation:{'b' * 64}"},
                "approval": {"approved": approved},
                "execution": {"status": "recommendation_approved"},
                "reasoning_steps": [{"decision": {"rationale": secret}}],
            },
        }

    receipt = _browser_operation_receipt(
        operation_id="operation-1",
        observed=[
            {
                "id": "operation-1",
                "operation_type": "rewind",
                "status": "completed",
                "text": secret,
            }
        ],
        persisted={
            "id": "operation-1",
            "operation_type": "rewind",
            "status": "completed",
            "reason": secret,
            "request_payload": {"user_input": secret},
            "invalidated_memory_ids": ["memory-1"],
            "restored_memory_ids": [],
            "events": [{"sequence": 1, "status": "completed", "metadata": secret}],
            "effects": [
                {
                    "sequence": 1,
                    "effect_type": "closed",
                    "source_memory_id": "memory-1",
                    "result_memory_id": None,
                    "namespace": "safe-namespace",
                    "metadata": secret,
                }
            ],
        },
        signature={
            "namespace": "safe-namespace",
            "operation_id": "operation-1",
            "invalidated_memory_ids": ["memory-1"],
            "bad": run("rejected", False),
            "corrected": run("completed", True),
        },
        capture_errors=[{"stage": "database", "type": "capture_failed", "detail": secret}],
    )

    assert set(receipt) == {
        "operation_id",
        "observed",
        "persisted",
        "signature",
        "capture_errors",
    }
    assert set(receipt["observed"][0]) == {"id", "operation_type", "status"}
    assert set(receipt["persisted"]) == {
        "id",
        "operation_type",
        "status",
        "invalidated_memory_ids",
        "restored_memory_ids",
        "events",
        "effects",
    }
    assert set(receipt["signature"]["corrected"]) == {
        "run_id",
        "decision_id",
        "status",
        "reflected_memory_id",
        "selection_fingerprint",
        "recommendation_id",
        "approval_approved",
        "execution_status",
        "read_memory_ids",
        "read_count",
        "downstream_lineage_edge_count",
    }
    assert receipt["capture_errors"] == [{"stage": "database", "type": "capture_failed"}]
    assert secret not in json.dumps(receipt, sort_keys=True)


def test_controlled_observation_receipt_excludes_raw_cloudwatch_payloads():
    secret = "unrestricted-cloudwatch-material"
    receipt = _controlled_observation_receipt(
        [
            {
                "id": "diagnostic:run-1:1",
                "tool": "aws_cloudwatch_diagnostics",
                "query_key": "payments.retry_fanout",
                "status": "completed",
                "request": secret,
            }
        ],
        [
            {
                "schema_version": 1,
                "tool_call_id": "diagnostic:run-1:1",
                "tool": "aws_cloudwatch_diagnostics",
                "query_key": "payments.retry_fanout",
                "status": "available",
                "account_id": secret,
                "region": secret,
                "metric": {
                    "namespace": "Hindsight/ControlledIncidentTelemetry",
                    "name": "RetryFanout",
                    "dimensions": [{"name": secret, "value": secret}],
                    "statistic": "Maximum",
                    "period_seconds": 60,
                },
                "window": {
                    "start": "2026-08-11T12:00:00Z",
                    "end": "2026-08-11T12:15:00Z",
                    "seconds": 900,
                },
                "datapoints": [
                    {
                        "timestamp": "2026-08-11T12:14:00Z",
                        "value": 8.0,
                        "raw": secret,
                    }
                ],
                "datapoint_count": 1,
                "truncated": False,
                "raw_request": secret,
            }
        ],
    )

    assert receipt == {
        "completed_tool_calls": [
            {
                "id": "diagnostic:run-1:1",
                "query_key": "payments.retry_fanout",
                "status": "completed",
            }
        ],
        "elevated_observations": [
            {
                "query_key": "payments.retry_fanout",
                "metric_namespace": "Hindsight/ControlledIncidentTelemetry",
                "metric_name": "RetryFanout",
                "statistic": "Maximum",
                "window_start": "2026-08-11T12:00:00Z",
                "window_end": "2026-08-11T12:15:00Z",
                "window_seconds": 900,
                "period_seconds": 60,
                "unit": "Count",
                "datapoint_count": 1,
                "maximum_value": 8.0,
            }
        ],
    }
    assert secret not in json.dumps(receipt, sort_keys=True)


def test_remediation_screenshot_is_element_scoped(monkeypatch, tmp_path):
    calls = []

    class Driver:
        def execute_script(self, script, element):
            calls.append((script, element))

    class Element:
        def screenshot(self, path):
            Path(path).write_bytes(b"bounded-element-image")
            return True

    element = Element()
    monkeypatch.setenv("HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR", str(tmp_path))

    _capture_remediation_screenshot(Driver(), "bounded.png", element)

    assert calls and calls[0][1] is element
    assert (tmp_path / "bounded.png").read_bytes() == b"bounded-element-image"


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
        wait.until(expected.presence_of_element_located((By.ID, "memories")))
        wait.until(expected.presence_of_element_located((By.ID, "startRun")))
        wait.until_not(
            lambda browser: browser.find_element(By.ID, "startRun").get_attribute("disabled")
        )

        walkthrough = driver.find_element(By.ID, "operatorWalkthrough")
        assert not walkthrough.is_displayed()
        walkthrough_toggle = driver.find_element(By.ID, "walkthroughToggle")
        assert walkthrough_toggle.get_attribute("aria-expanded") == "false"
        walkthrough_toggle.click()
        wait.until(
            lambda browser: (
                browser.find_element(By.ID, "walkthroughToggle").get_attribute("aria-expanded")
                == "true"
                and browser.find_element(By.ID, "operatorWalkthrough").is_displayed()
            )
        )
        walkthrough = driver.find_element(By.ID, "operatorWalkthrough")
        assert "Reset the replay" in walkthrough.text
        assert "Inspect history" in walkthrough.text
        driver.find_element(By.ID, "walkthroughToggle").click()
        wait.until_not(
            lambda browser: (
                browser.find_element(By.ID, "walkthroughToggle").get_attribute("aria-expanded")
                == "true"
                or browser.find_element(By.ID, "operatorWalkthrough").is_displayed()
            )
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
        selected_action = driver.find_element(
            By.CSS_SELECTOR, ".outcome-current .action-execution"
        ).text.casefold()
        assert any(
            action_kind in selected_action
            for action_kind in ("recommendation", "governed-memory retraction")
        )
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
        _assert_no_browser_errors(driver)

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
        _assert_no_browser_errors(driver)
    finally:
        _write_browser_evidence(driver, operation_id=operation_id, signature=signature)
        driver.quit()


@requires_hosted_remediation
def test_operator_can_approve_model_selected_governed_memory_retraction():
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as expected
    from selenium.webdriver.support.ui import WebDriverWait

    driver = _start_firefox_driver()
    driver.set_script_timeout(120)
    wait = WebDriverWait(driver, 150)
    try:
        driver.set_window_size(1440, 1000)
        driver.get(_browser_url(namespace=DEMO_NAMESPACE))
        wait.until(expected.presence_of_element_located((By.ID, "memories")))
        _capture_console_errors(driver)
        wait.until(lambda browser: "Live" in browser.find_element(By.ID, "connection").text)

        driver.find_element(By.ID, "identityButton").click()
        driver.find_element(By.ID, "identitySignIn").click()
        _complete_operator_sign_in(driver, wait)
        wait.until(expected.presence_of_element_located((By.ID, "startRun")))

        previous_namespace = driver.find_element(By.ID, "namespace").text
        driver.find_element(By.ID, "resetDemo").click()
        namespace = wait.until(lambda browser: _ready_reset_namespace(browser, previous_namespace))
        driver.find_element(By.ID, "poisonDemo").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text == "2 live · 0 invalid"
        )

        report = driver.find_element(By.ID, "incidentInput")
        report.clear()
        report.send_keys(REMEDIATION_REPORT)
        driver.find_element(By.ID, "startRun").click()
        _wait_for_run_status(driver, expected_status="awaiting approval", timeout=150)
        wait.until(
            lambda browser: (
                "governed-memory retraction"
                in browser.find_element(
                    By.CSS_SELECTOR, ".outcome-current .action-execution"
                ).text.casefold()
            )
        )

        awaiting = _awaiting_remediation_identity(namespace=namespace)
        remediation_run_id = awaiting["run_id"]
        target_memory_id = awaiting["target_memory_id"]
        expected_effect_labels = [
            *[f"Close memory {memory_id}" for memory_id in awaiting["close_memory_ids"]],
            *[
                "Resolve review "
                f"{resolution['id']} for memory {resolution['semantic_memory_id']} "
                f"as {resolution['status']}"
                for resolution in awaiting["review_resolutions"]
            ],
        ]
        current_execution = driver.find_element(
            By.CSS_SELECTOR, ".outcome-current .action-execution"
        )
        current_diagnostic = driver.find_element(
            By.CSS_SELECTOR, ".outcome-current .action-observation"
        )
        current_citations = driver.find_element(
            By.CSS_SELECTOR, ".outcome-current .decision-citations"
        )
        _require(
            current_execution.get_attribute("data-execution-status") == "awaiting_approval",
            "browser did not render the awaiting remediation execution state",
        )
        _require(
            "governed-memory retraction" in current_execution.text.casefold(),
            "browser did not render governed remediation controls",
        )
        assert [
            item.text
            for item in current_execution.find_elements(
                By.CSS_SELECTOR, '[aria-label="Approval-bound retraction effects"] li'
            )
        ] == expected_effect_labels
        approval = driver.find_element(By.ID, "approvalActions")
        _require(approval.is_displayed(), "browser did not display remediation approval controls")
        _require(
            target_memory_id in approval.text,
            "browser approval controls did not bind the remediation target",
        )
        _require(
            driver.find_element(By.ID, "rejectRun").text == "Reject retraction",
            "browser did not render the bounded rejection control",
        )
        approve = driver.find_element(By.ID, "approveRun")
        _require(approve.text == "Approve retraction", "browser approval label was incorrect")
        _require(not approve.get_attribute("disabled"), "browser remediation approval was disabled")
        _capture_remediation_screenshot(
            driver,
            "governed-remediation-awaiting-action.png",
            current_execution,
        )
        _capture_remediation_screenshot(
            driver,
            "governed-remediation-awaiting-diagnostic.png",
            current_diagnostic,
        )
        _capture_remediation_screenshot(
            driver,
            "governed-remediation-awaiting-citations.png",
            current_citations,
        )
        _capture_remediation_screenshot(
            driver,
            "governed-remediation-approval-controls.png",
            approval,
        )

        approve.click()
        _wait_for_run_status(driver, expected_status="completed", timeout=150)

        def invalidated_target(browser):
            row = browser.find_element(By.CSS_SELECTOR, f'[data-memory-id="{target_memory_id}"]')
            return row if "invalidated" in row.get_attribute("class").split() else False

        target_row = wait.until(invalidated_target)
        _require(
            "Completed governed-memory retraction"
            in driver.find_element(By.CSS_SELECTOR, ".outcome-current h3").text,
            "browser did not render terminal remediation completion",
        )
        receipt = _assert_completed_governed_remediation(
            namespace=namespace,
            run_id=remediation_run_id,
        )
        operation_id = receipt["execution"]["operation_id"]
        current_execution = driver.find_element(
            By.CSS_SELECTOR, ".outcome-current .action-execution"
        )
        _require(
            current_execution.get_attribute("data-execution-status") == "completed",
            "browser did not render the completed remediation execution state",
        )
        assert [
            item.text
            for item in current_execution.find_elements(
                By.CSS_SELECTOR, '[aria-label="Approval-bound retraction effects"] li'
            )
        ] == expected_effect_labels
        _require(
            f"Operation {operation_id}: completed".casefold()
            in current_execution.text.casefold(),
            "browser did not render the completed remediation operation identity",
        )
        wait.until(
            expected.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    f'#operations [data-operation-id="{operation_id}"]'
                    '[data-operation-type="retraction"]'
                    '[data-operation-status="completed"]',
                )
            )
        )
        _require(
            target_row.get_attribute("data-memory-id") == target_memory_id,
            "browser invalidated ledger row did not match the remediation target",
        )
        _capture_remediation_screenshot(
            driver,
            "governed-remediation-completed-action.png",
            current_execution,
        )
        _capture_remediation_screenshot(
            driver,
            "governed-remediation-invalidated-ledger.png",
            target_row,
        )
        _assert_no_browser_errors(driver)
        _write_governed_remediation_receipt(receipt)
    finally:
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


def _wait_for_run_status(driver, *, expected_status: str, timeout: float) -> None:
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    observed = ""

    def reached(browser):
        nonlocal observed
        observed = browser.find_element(By.ID, "runStatus").text.strip().lower()
        if observed in {"failed", "rejected"} and observed != expected_status:
            raise AssertionError(f"agent run reached unexpected terminal status: {observed}")
        return observed == expected_status

    try:
        WebDriverWait(driver, timeout).until(reached)
    except TimeoutException as exc:
        raise AssertionError(
            f"agent run did not reach {expected_status!r} before its retry budget: {observed}"
        ) from exc


def _capture_remediation_screenshot(driver, name: str, element) -> None:
    directory = _browser_evidence_directory()
    _require(directory is not None, "browser remediation evidence directory is not configured")
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
        element,
    )
    path = directory / name
    captured = element.screenshot(str(path))
    _require(bool(captured), "browser remediation screenshot capture failed")
    _require(
        path.is_file() and path.stat().st_size > 0,
        "browser remediation screenshot was not persisted",
    )


def _awaiting_remediation_identity(*, namespace: str) -> dict[str, Any]:
    from hindsight.db import connect, database_url
    from hindsight.runs import get_run

    with connect(
        database_url(),
        application_name="hindsight-browser-remediation-awaiting",
    ) as conn:
        row = conn.execute(
            """
                SELECT id FROM agent_runs
                WHERE namespace = %s
                ORDER BY created_at DESC
                LIMIT 1
            """,
            (namespace,),
        ).fetchone()
    _require(row is not None, "awaiting remediation run was not persisted")
    run = get_run(run_id=str(row[0]), db_url=database_url())
    _require(run is not None, "awaiting remediation run could not be loaded")
    _require(
        run["status"] == "awaiting_approval",
        "remediation run did not pause for operator approval",
    )
    _require(
        run["user_input"] == REMEDIATION_REPORT,
        "remediation run did not preserve the governed prompt contract",
    )
    trace = run["action_trace"]
    action = trace["remediation_action"]
    _require(trace["schema_version"] == 3, "remediation trace schema was not current")
    _require(
        trace["mode"] == "governed_memory_remediation",
        "model did not select governed memory remediation",
    )
    _require(
        action["name"] == "retract_recalled_memory",
        "model did not select controlled memory retraction",
    )
    _require(
        trace["execution"]["status"] == "awaiting_approval",
        "remediation execution did not remain approval-bound",
    )
    preview = trace["preview"]
    return {
        "run_id": str(run["id"]),
        "target_memory_id": str(action["target_memory_id"]),
        "close_memory_ids": [str(value) for value in preview["effects"]["close_memory_ids"]],
        "review_resolutions": [dict(value) for value in preview["effects"]["review_resolutions"]],
    }


def _assert_completed_governed_remediation(*, namespace: str, run_id: str) -> dict[str, Any]:
    from psycopg.rows import dict_row

    from hindsight.agent_decision import (
        agent_decision_from_payload,
        diagnostic_observation_fingerprint,
        memory_selection_fingerprint,
        remediation_action_id,
    )
    from hindsight.db import connect, database_url
    from hindsight.operations import get_operation
    from hindsight.runs import get_run
    from hindsight.server_tenants import PUBLIC_DEMO_TENANT_ID

    run = get_run(run_id=run_id, db_url=database_url())
    _require(run is not None, "completed remediation run was not persisted")
    _require(run["status"] == "completed", "remediation run did not complete")
    _require(
        run["user_input"] == REMEDIATION_REPORT,
        "completed remediation run did not preserve the governed prompt contract",
    )
    events = run["events"]
    awaiting_events = [
        event
        for event in events
        if event["status"] == "awaiting_approval" and event["metadata"].get("action_trace")
    ]
    completion_events = [
        event
        for event in events
        if event["phase"] == "completion" and event["status"] == "completed"
    ]
    approval_events = [
        event for event in events if event["phase"] == "approval" and event["status"] == "resuming"
    ]
    _require(
        len(awaiting_events) == len(completion_events) == len(approval_events) == 1,
        "remediation run did not persist one awaiting, approval, and completion event",
    )
    awaiting_trace = awaiting_events[0]["metadata"]["action_trace"]
    trace = completion_events[0]["metadata"]["action_trace"]
    for key in (
        "schema_version",
        "mode",
        "selection",
        "reasoning_steps",
        "tool_calls",
        "observations",
        "observation_fingerprint",
        "remediation_action",
        "preview",
    ):
        if _canonical_sha256(trace[key]) != _canonical_sha256(awaiting_trace[key]):
            raise AssertionError(f"terminal remediation {key} binding changed")

    selection = trace["selection"]
    action = trace["remediation_action"]
    preview_trace = trace["preview"]
    execution = trace["execution"]
    approval = trace["approval"]
    target_id = str(action["target_memory_id"])
    actor = str(approval["actor"])
    actor_id = actor.rsplit(":", 1)[-1]
    selected_ids = [str(value) for value in selection["memory_ids"]]
    _require(
        selection["provider"] == "gemini" and bool(selection["model"]),
        "remediation selection was not produced by Gemini",
    )
    _require(
        re.fullmatch(r"[0-9a-f]{64}", selection["fingerprint"]) is not None,
        "remediation selection fingerprint is invalid",
    )
    _require(
        re.fullmatch(r"[0-9a-f]{64}", trace["observation_fingerprint"]) is not None,
        "remediation observation fingerprint is invalid",
    )
    _require(
        diagnostic_observation_fingerprint(trace["observations"])
        == trace["observation_fingerprint"],
        "remediation observation fingerprint could not be reproduced",
    )
    diagnostics = _controlled_observation_receipt(trace["tool_calls"], trace["observations"])

    terminal_payload = trace["reasoning_steps"][-1]["decision"]
    try:
        model_decision = agent_decision_from_payload(terminal_payload)
    except Exception:
        raise AssertionError("terminal model decision failed schema validation") from None
    _require(
        model_decision.next_step_kind == "remediation_action",
        "terminal model decision did not select remediation",
    )
    _require(
        model_decision.remediation_action is not None,
        "terminal model decision omitted remediation action",
    )
    _require(
        model_decision.remediation_action.name == "retract_recalled_memory",
        "terminal model decision selected the wrong remediation action",
    )
    _require(
        model_decision.remediation_action.target_memory_id == target_id,
        "terminal model decision changed the remediation target",
    )
    _require(
        action["status"] == "awaiting_approval",
        "sealed remediation proposal status changed after execution",
    )
    citations = [
        citation
        for citation in model_decision.recalled_memory_citations
        if citation.memory_id == target_id
    ]
    _require(len(citations) == 1, "terminal model decision did not cite the target exactly once")
    quote = citations[0].quote
    _require(
        len(" ".join(quote.split())) >= 12,
        "terminal model citation was not a meaningful verbatim quote",
    )
    _require(action["target_excerpt"] == quote, "remediation target citation binding changed")
    _require(
        action["id"]
        == remediation_action_id(
            run_id=run_id,
            decision=model_decision,
            selection_fingerprint=selection["fingerprint"],
            observation_fingerprint=trace["observation_fingerprint"],
        ),
        "remediation action identity could not be reproduced",
    )

    with connect(
        database_url(),
        application_name="hindsight-browser-remediation-receipt",
    ) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM memory_decisions WHERE id = %s",
                (run["decision_id"],),
            )
            decision = dict(cur.fetchone())
            cur.execute(
                "SELECT * FROM memory_operation_previews WHERE id = %s",
                (preview_trace["id"],),
            )
            preview = dict(cur.fetchone())
            cur.execute(
                """
                    SELECT id, tenant_id, status, policy, selected_strategy,
                           embedding_profile_id, returned_memory_ids
                    FROM memory_retrievals
                    WHERE decision_id = %s
                    ORDER BY started_at, id
                """,
                (run["decision_id"],),
            )
            retrievals = [dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT * FROM semantic_memories WHERE id = ANY(%s)",
                (selected_ids,),
            )
            memories = {str(row["id"]): dict(row) for row in cur.fetchall()}
            cur.execute(
                "SELECT id, tenant_id, retrieval_id, memory_id, rank "
                "FROM memory_reads WHERE retrieval_id = ("
                "SELECT id FROM memory_retrievals WHERE decision_id = %s "
                "ORDER BY started_at, id LIMIT 1"
                ") ORDER BY rank",
                (run["decision_id"],),
            )
            reads = [dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT id, tenant_id, role, status FROM product_principal_roles WHERE id = %s",
                (actor_id,),
            )
            actor_mapping_row = cur.fetchone()
            actor_mapping = dict(actor_mapping_row) if actor_mapping_row is not None else None
            cur.execute(
                "SELECT tenant_id FROM incidents WHERE id = %s",
                (run["incident_id"],),
            )
            incident_tenant = str(cur.fetchone()["tenant_id"])
            cur.execute(
                "SELECT count(*) AS count FROM agent_reflections WHERE run_id = %s",
                (run_id,),
            )
            reflection_count = int(cur.fetchone()["count"])
            cur.execute(
                "SELECT count(*) AS count FROM semantic_memories WHERE producer_decision_id = %s",
                (run["decision_id"],),
            )
            produced_count = int(cur.fetchone()["count"])
            cur.execute(
                "SELECT id FROM semantic_memories "
                "WHERE namespace = %s AND t_invalid IS NOT NULL ORDER BY id",
                (namespace,),
            )
            namespace_invalidated = [str(row["id"]) for row in cur.fetchall()]

    _require(bool(retrievals), "remediation decision did not persist a memory retrieval")
    retrieval = retrievals[0]
    retrieval_ids = [str(value) for value in retrieval["returned_memory_ids"]]
    _require(
        retrieval["status"] == "succeeded"
        and retrieval["policy"] == "semantic_strict"
        and retrieval["selected_strategy"] == "semantic_vector",
        "remediation selection did not use successful strict semantic retrieval",
    )
    embedding_profile_id = str(retrieval["embedding_profile_id"] or "")
    _require(bool(embedding_profile_id), "remediation retrieval omitted its embedding profile")
    _require(
        retrieval_ids == selected_ids,
        "ordered retrieval results do not match the remediation selection",
    )
    _require(
        selected_ids == [str(read["memory_id"]) for read in reads],
        "ordered memory reads do not match the remediation selection",
    )
    _require(
        len(memories) == len(selected_ids) and set(memories) == set(selected_ids),
        "remediation selection memories are incomplete",
    )
    _require(target_id in memories, "remediation target was not part of the recalled selection")
    target = memories[target_id]
    _require(
        target["content"] == COMPROMISED_GUIDANCE_CONTENT,
        "remediation target was not the controlled compromised guidance",
    )
    _require(
        target["writer"] == "demo.fixture-import"
        and target["source_ref"] == "demo:stale-runbook-import",
        "remediation target provenance did not match the controlled fixture",
    )
    _require(
        " ".join(quote.split()) in " ".join(target["content"].split()),
        "terminal model citation was not verbatim controlled target content",
    )
    _require(preview["operation_type"] == "retraction", "preview was not a retraction")
    _require(
        preview["actor"] == f"agent.run:{run_id}",
        "preview actor was not bound to the agent run",
    )
    _require(str(preview["id"]) == preview_trace["id"], "preview identity binding changed")
    _require(
        preview["fingerprint"] == preview_trace["fingerprint"],
        "preview fingerprint binding changed",
    )
    _require(
        preview["expires_at"].isoformat() == preview_trace["expires_at"],
        "preview expiry binding changed",
    )
    request_payload = dict(preview["request_payload"])
    effects = dict(preview["effect_payload"])
    close_ids = [str(value) for value in effects["close_memory_ids"]]
    review_resolutions = [dict(value) for value in effects["review_resolutions"]]
    _require(request_payload["root_memory_id"] == target_id, "preview root target changed")
    _require(request_payload["namespace"] == namespace, "preview namespace binding changed")
    _require(
        request_payload["authorized_namespaces"] == [namespace],
        "preview authorization exceeded the fresh namespace",
    )
    _require(
        _canonical_sha256(close_ids)
        == _canonical_sha256(preview_trace["effects"]["close_memory_ids"]),
        "preview close effects changed after approval",
    )
    _require(
        _canonical_sha256(review_resolutions)
        == _canonical_sha256(preview_trace["effects"]["review_resolutions"]),
        "preview review effects changed after approval",
    )
    _require(
        preview_trace["effect_count"] == len(close_ids) + len(review_resolutions),
        "preview effect count did not match its bounded effects",
    )
    _require(1 <= preview_trace["effect_count"] <= 10, "preview mutation count was not bounded")
    _require(close_ids == [target_id], "preview did not close exactly the selected target")
    _require(not review_resolutions, "controlled retraction unexpectedly resolved reviews")

    _require(
        re.fullmatch(
            r"product:operator:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}",
            actor,
        )
        is not None,
        "approval actor was not an opaque product operator identity",
    )
    _require(actor != preview["actor"], "approval actor was not independent of preview creation")
    _require(actor_mapping is not None, "approval actor did not resolve to a product principal")
    _require(
        str(actor_mapping["id"]) == actor_id
        and actor_mapping["role"] == "operator"
        and actor_mapping["status"] == "active",
        "approval actor did not resolve to an active operator mapping",
    )
    bindings = {
        "approved": True,
        "actor": actor,
        "remediation_action_id": action["id"],
        "selection_fingerprint": selection["fingerprint"],
        "observation_fingerprint": trace["observation_fingerprint"],
        "preview_id": preview_trace["id"],
        "preview_fingerprint": preview_trace["fingerprint"],
    }
    expected_approval = {**bindings, "disposition": "approved"}
    _require(set(approval) == set(expected_approval), "approval binding fields changed")
    for key, value in expected_approval.items():
        _require(approval.get(key) == value, f"approval {key} binding changed")
    approval_event_metadata = approval_events[0]["metadata"]
    _require(
        set(approval_event_metadata) == set(bindings),
        "approval event binding fields changed",
    )
    for key, value in bindings.items():
        _require(
            approval_event_metadata.get(key) == value,
            f"approval event {key} binding changed",
        )

    _require(execution["status"] == "completed", "remediation execution did not complete")
    _require(
        execution["mode"] == "governed_memory_remediation",
        "remediation execution mode changed",
    )
    operation = get_operation(
        operation_id=str(execution["operation_id"]),
        db_url=database_url(),
    )
    _require(operation is not None, "remediation operation was not persisted")
    _require(
        operation["status"] == execution["operation_status"] == "completed",
        "remediation operation did not complete",
    )
    _require(operation["operation_type"] == "retraction", "operation was not a retraction")
    _require(operation["actor"] == actor, "operation actor did not match approval actor")
    _require(
        str(operation["preview_id"]) == preview_trace["id"],
        "operation preview identity binding changed",
    )
    _require(
        operation["preview_fingerprint"] == preview_trace["fingerprint"],
        "operation preview fingerprint binding changed",
    )
    _require(str(operation["root_memory_id"]) == target_id, "operation target changed")
    _require(operation["namespace"] == namespace, "operation namespace binding changed")
    _require(
        re.fullmatch(r"agent-remediation:[0-9a-f]{64}", operation["idempotency_key"]) is not None,
        "operation idempotency identity was invalid",
    )
    attempt_count = int(operation["attempt_count"])
    _require(1 <= attempt_count <= 3, "operation attempt count exceeded its retry bound")
    _require(
        [str(value) for value in operation["invalidated_memory_ids"]] == close_ids,
        "operation invalidations did not match the approved preview",
    )
    _require(not operation["restored_memory_ids"], "retraction unexpectedly restored memories")
    event_receipt = [
        {"sequence": int(event["sequence"]), "status": str(event["status"])}
        for event in operation["events"]
    ]
    _require(
        [
            {"sequence": int(event["sequence"]), "status": str(event["status"])}
            for event in execution["events"]
        ]
        == event_receipt,
        "terminal trace operation events changed",
    )
    event_statuses = [event["status"] for event in event_receipt]
    _require(
        bool(event_statuses)
        and event_statuses[0] == "queued"
        and event_statuses[-1] == "completed"
        and event_statuses.count("leased") == attempt_count
        and event_statuses.count("retrying") == attempt_count - 1
        and set(event_statuses) <= {"queued", "leased", "retrying", "completed"},
        "controlled remediation operation exceeded its bounded retry sequence",
    )
    _require(
        [event["sequence"] for event in event_receipt]
        == list(range(1, len(event_receipt) + 1)),
        "controlled remediation operation event sequence was not contiguous",
    )
    effect_receipt = [_operation_effect_receipt(effect) for effect in operation["effects"]]
    _require(
        [_operation_effect_receipt(effect) for effect in execution["effects"]] == effect_receipt,
        "terminal trace operation effects changed",
    )
    _require(
        [effect["source_memory_id"] for effect in effect_receipt] == close_ids,
        "operation effects did not close the approved memory set",
    )
    _require(
        all(
            effect["effect_type"] == "closed"
            and effect["result_memory_id"] is None
            and effect["namespace"] == namespace
            for effect in effect_receipt
        ),
        "operation effects were not exact same-namespace closures",
    )

    selection_projection = []
    for memory_id in selected_ids:
        memory = memories[memory_id]
        projected = dict(memory)
        if memory_id in close_ids:
            _require(
                memory["t_invalid"] is not None
                and memory["invalidated_at"] is not None
                and memory["invalidated_by"] == actor,
                "closed selection memory was not invalidated by the approved operation actor",
            )
            projected["t_invalid"] = None
        else:
            _require(
                memory["t_invalid"] is None,
                "a non-target selection memory changed after model selection",
            )
        projected["embedding_profile_id"] = embedding_profile_id
        selection_projection.append(projected)
    _require(
        memory_selection_fingerprint(selection_projection) == selection["fingerprint"],
        "remediation selection fingerprint could not be reproduced",
    )

    _require(run["action_approved"] is True, "remediation run was not marked approved")
    _require(
        run["created_at"] is not None and run["completed_at"] is not None,
        "completed remediation run omitted terminal timestamps",
    )
    _require(
        run["provider"] == "gemini" and bool(run["model"]),
        "remediation run was not Gemini-backed",
    )
    _require(
        run["reflected_memory_id"] is None,
        "governed remediation unexpectedly produced a reflected memory",
    )
    _require(
        decision["status"] == "sealed" and decision["sealed_at"] is not None,
        "remediation decision was not sealed",
    )
    _require(
        reflection_count == produced_count == 0,
        "governed remediation unexpectedly produced semantic reflection",
    )
    _require(
        target["t_invalid"] is not None and target["invalidated_at"] is not None,
        "controlled remediation target remained current",
    )
    _require(target["invalidated_by"] == actor, "target invalidation actor changed")
    _require(
        target["invalidation_reason"] == operation["reason"],
        "target invalidation reason did not match the operation",
    )
    _require(
        sorted(namespace_invalidated) == sorted(close_ids),
        "fresh namespace contained unexpected invalidated memories",
    )

    tenant_values = {
        str(run["tenant_id"]),
        str(decision["tenant_id"]),
        str(preview["tenant_id"]),
        str(operation["tenant_id"]),
        str(retrieval["tenant_id"]),
        str(actor_mapping["tenant_id"]),
        incident_tenant,
        *[str(event["tenant_id"]) for event in events],
        *[str(item["tenant_id"]) for item in retrievals],
        *[str(read["tenant_id"]) for read in reads],
        *[str(memory["tenant_id"]) for memory in memories.values()],
        *[str(event["tenant_id"]) for event in operation["events"]],
        *[str(effect["tenant_id"]) for effect in operation["effects"]],
    }
    _require(
        tenant_values == {PUBLIC_DEMO_TENANT_ID},
        "remediation evidence crossed the public demo tenant boundary",
    )

    return {
        "schema_version": 1,
        "kind": "governed_memory_remediation",
        "workflow": _hosted_workflow_binding(),
        "tenant_binding": {
            "tenant_id": PUBLIC_DEMO_TENANT_ID,
            "all_equal": True,
        },
        "run": {
            "id": run_id,
            "decision_id": str(run["decision_id"]),
            "namespace": namespace,
            "status": "completed",
            "created_at": str(run["created_at"]),
            "completed_at": str(run["completed_at"]),
            "provider": str(run["provider"]),
            "model": str(run["model"]),
        },
        "prompt_contract": {
            "version": REMEDIATION_REPORT_CONTRACT_VERSION,
            "sha256": REMEDIATION_REPORT_SHA256,
        },
        "selection": {
            "fingerprint": str(selection["fingerprint"]),
            "memory_ids": selected_ids,
            "retrieval_id": str(retrieval["id"]),
            "embedding_profile_id": embedding_profile_id,
        },
        "diagnostics": {
            "observation_fingerprint": str(trace["observation_fingerprint"]),
            **diagnostics,
        },
        "remediation_action": {
            "id": str(action["id"]),
            "name": "retract_recalled_memory",
            "target_memory_id": target_id,
            "verbatim_quote": quote,
        },
        "preview": {
            "id": str(preview["id"]),
            "fingerprint": str(preview["fingerprint"]),
            "expires_at": preview["expires_at"].isoformat(),
            "effect_count": int(preview_trace["effect_count"]),
            "close_memory_ids": close_ids,
            "review_resolutions": review_resolutions,
        },
        "approval": {**bindings, "disposition": "approved"},
        "execution": {
            "operation_id": str(operation["id"]),
            "operation_type": "retraction",
            "operation_status": "completed",
            "actor": actor,
            "preview_id": str(operation["preview_id"]),
            "preview_fingerprint": str(operation["preview_fingerprint"]),
            "attempt_count": attempt_count,
            "events": event_receipt,
            "effects": effect_receipt,
            "invalidated_memory_ids": close_ids,
            "restored_memory_ids": [],
        },
        "ledger": {
            "target": {
                "memory_id": target_id,
                "content": COMPROMISED_GUIDANCE_CONTENT,
                "source_ref": "demo:stale-runbook-import",
                "writer": "demo.fixture-import",
                "invalidated": True,
                "invalidated_by": actor,
            },
            "namespace_invalidated_memory_ids": namespace_invalidated,
        },
        "terminal_decision": {
            "status": "sealed",
            "sealed_at": decision["sealed_at"].isoformat(),
        },
        "semantic_reflection": {
            "run_reflected_memory_id": None,
            "reflection_count": 0,
            "produced_memory_count": 0,
        },
        "screenshots": [
            "governed-remediation-awaiting-action.png",
            "governed-remediation-awaiting-diagnostic.png",
            "governed-remediation-awaiting-citations.png",
            "governed-remediation-approval-controls.png",
            "governed-remediation-completed-action.png",
            "governed-remediation-invalidated-ledger.png",
        ],
    }


def _controlled_observation_receipt(
    tool_calls: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    allowed_query_keys = {
        "payments.checkout_latency_ms",
        "payments.processor_queue_depth",
        "payments.retry_fanout",
    }
    completed_calls = [
        call
        for call in tool_calls
        if call.get("tool") == "aws_cloudwatch_diagnostics" and call.get("status") == "completed"
    ]
    _require(bool(completed_calls), "remediation run did not complete CloudWatch diagnostics")
    _require(
        all(call.get("query_key") in allowed_query_keys for call in completed_calls),
        "remediation run used an unconfigured diagnostic query",
    )
    completed = [
        {
            "id": str(call["id"]),
            "query_key": str(call["query_key"]),
            "status": "completed",
        }
        for call in completed_calls
    ]
    target_calls = [
        call for call in completed_calls if call.get("query_key") == "payments.retry_fanout"
    ]
    _require(len(target_calls) == 1, "retry fanout diagnostics did not complete exactly once")
    target_observations = [
        observation
        for observation in observations
        if observation.get("query_key") == "payments.retry_fanout"
        and observation.get("status") == "available"
    ]
    _require(
        len(target_observations) == 1,
        "retry fanout diagnostics did not return one available observation",
    )
    observation = target_observations[0]
    _require(
        observation.get("tool_call_id") == target_calls[0]["id"],
        "retry fanout observation did not bind to its completed tool call",
    )
    _require(
        observation.get("schema_version") == 1
        and observation.get("tool") == "aws_cloudwatch_diagnostics",
        "retry fanout observation did not match the diagnostic schema",
    )
    metric = observation.get("metric")
    window = observation.get("window")
    _require(isinstance(metric, dict), "retry fanout observation omitted metric metadata")
    _require(isinstance(window, dict), "retry fanout observation omitted its bounded window")
    _require(
        metric.get("namespace") == "Hindsight/ControlledIncidentTelemetry"
        and metric.get("name") == "RetryFanout"
        and metric.get("statistic") == "Maximum"
        and metric.get("period_seconds") == 60,
        "retry fanout observation did not match the controlled metric contract",
    )
    _require(window.get("seconds") == 900, "retry fanout observation window was not bounded")
    window_start = str(window.get("start") or "")
    window_end = str(window.get("end") or "")
    try:
        parsed_start = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat(window_end.replace("Z", "+00:00"))
    except ValueError:
        raise AssertionError("retry fanout observation window timestamps were invalid") from None
    _require(
        (parsed_end - parsed_start).total_seconds() == 900,
        "retry fanout observation timestamps did not match its bounded window",
    )
    datapoints = observation.get("datapoints")
    _require(isinstance(datapoints, list) and bool(datapoints), "retry fanout had no datapoints")
    try:
        normalized_datapoints = [
            (
                datetime.fromisoformat(str(point["timestamp"]).replace("Z", "+00:00")),
                float(point["value"]),
            )
            for point in datapoints
            if isinstance(point, dict)
        ]
    except (KeyError, TypeError, ValueError):
        raise AssertionError("retry fanout datapoints failed normalized validation") from None
    values = [value for _timestamp, value in normalized_datapoints]
    datapoint_count = int(observation.get("datapoint_count") or 0)
    _require(
        len(values) == len(datapoints) == datapoint_count,
        "retry fanout datapoint count did not match its normalized values",
    )
    _require(
        all(parsed_start <= timestamp <= parsed_end for timestamp, _value in normalized_datapoints),
        "retry fanout datapoints escaped the bounded observation window",
    )
    _require(not observation.get("truncated"), "retry fanout observation was truncated")
    maximum_value = max(values)
    _require(maximum_value >= 8.0, "retry fanout did not meet the controlled elevated floor")
    elevated = [
        {
            "query_key": "payments.retry_fanout",
            "metric_namespace": "Hindsight/ControlledIncidentTelemetry",
            "metric_name": "RetryFanout",
            "statistic": "Maximum",
            "window_start": window_start,
            "window_end": window_end,
            "window_seconds": 900,
            "period_seconds": 60,
            "unit": "Count",
            "datapoint_count": datapoint_count,
            "maximum_value": maximum_value,
        }
    ]
    return {
        "completed_tool_calls": completed,
        "elevated_observations": elevated,
    }


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _operation_effect_receipt(effect: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence": int(effect["sequence"]),
        "effect_type": str(effect["effect_type"]),
        "source_memory_id": str(effect["source_memory_id"])
        if effect.get("source_memory_id")
        else None,
        "result_memory_id": str(effect["result_memory_id"])
        if effect.get("result_memory_id")
        else None,
        "namespace": str(effect["namespace"]) if effect.get("namespace") else None,
    }


def _hosted_workflow_binding() -> dict[str, Any]:
    repository = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    deployed_sha = (os.environ.get("HINDSIGHT_EXPECTED_DEPLOYED_REVISION") or "").strip()
    workflow_ref = (os.environ.get("GITHUB_WORKFLOW_REF") or "").strip()
    run_id = (os.environ.get("GITHUB_RUN_ID") or "").strip()
    run_attempt = (os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip()
    assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
    assert re.fullmatch(r"[0-9a-f]{40}", deployed_sha)
    assert os.environ.get("GITHUB_SHA") == deployed_sha
    assert workflow_ref == (f"{repository}/.github/workflows/live-acceptance.yml@refs/heads/main")
    assert run_id.isdigit() and int(run_id) > 0
    assert run_attempt.isdigit() and int(run_attempt) > 0
    return {
        "repository": repository,
        "workflow_ref": workflow_ref,
        "run_id": int(run_id),
        "run_attempt": int(run_attempt),
        "deployed_sha": deployed_sha,
    }


def _write_governed_remediation_receipt(receipt: dict[str, Any]) -> None:
    serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if OPERATOR_PASSWORD and OPERATOR_PASSWORD in serialized:
        raise AssertionError("remediation receipt contained a forbidden credential")
    if OPERATOR_USERNAME and OPERATOR_USERNAME in serialized:
        raise AssertionError("remediation receipt contained a forbidden operator login")
    directory = _browser_evidence_directory()
    _require(directory is not None, "browser remediation evidence directory is not configured")
    (directory / "governed-remediation-receipt.json").write_text(
        serialized,
        encoding="utf-8",
    )


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


def _assert_no_browser_errors(driver) -> None:
    try:
        console_errors = driver.execute_script("return window.__HINDSIGHT_CONSOLE_ERRORS || [];")
        visible_errors = driver.execute_script("return window.__HINDSIGHT_VISIBLE_ERRORS || [];")
    except Exception:
        raise AssertionError("browser error evidence could not be read") from None
    if console_errors:
        raise AssertionError("browser console error observed")
    if visible_errors:
        raise AssertionError("browser visible error observed")


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
    except Exception:  # noqa: BLE001 - evidence capture must not mask the test failure
        capture_errors.append({"stage": "screenshot", "type": "capture_failed"})
    try:
        console_errors = driver.execute_script("return window.__HINDSIGHT_CONSOLE_ERRORS || [];")
    except Exception:  # noqa: BLE001 - evidence capture must not mask the test failure
        capture_errors.append({"stage": "console", "type": "capture_failed"})
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
                    "operation_type": element.get_attribute("data-operation-type"),
                    "status": element.get_attribute("data-operation-status"),
                }
            )
    except Exception:  # noqa: BLE001 - evidence capture must not mask the test failure
        capture_errors.append({"stage": "operations", "type": "capture_failed"})
    persisted = None
    if operation_id and os.environ.get("DATABASE_URL"):
        from hindsight.operations import get_operation

        try:
            persisted = get_operation(operation_id=operation_id)
        except Exception:  # noqa: BLE001 - evidence capture must not mask the test failure
            capture_errors.append({"stage": "database", "type": "capture_failed"})
    (directory / "operation.json").write_text(
        json.dumps(
            _browser_operation_receipt(
                operation_id=operation_id,
                observed=observed,
                persisted=persisted,
                signature=signature,
                capture_errors=capture_errors,
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _browser_operation_receipt(
    *,
    operation_id: str | None,
    observed: list[dict[str, Any]],
    persisted: dict[str, Any] | None,
    signature: dict[str, Any] | None,
    capture_errors: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "observed": [
            {
                "id": item.get("id"),
                "operation_type": item.get("operation_type"),
                "status": item.get("status"),
            }
            for item in observed
        ],
        "persisted": _persisted_operation_receipt(persisted),
        "signature": _signature_receipt(signature),
        "capture_errors": [
            {"stage": item["stage"], "type": item["type"]} for item in capture_errors
        ],
    }


def _persisted_operation_receipt(
    operation: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if operation is None:
        return None
    return {
        "id": str(operation["id"]),
        "operation_type": str(operation["operation_type"]),
        "status": str(operation["status"]),
        "invalidated_memory_ids": [
            str(value) for value in operation.get("invalidated_memory_ids") or []
        ],
        "restored_memory_ids": [str(value) for value in operation.get("restored_memory_ids") or []],
        "events": [
            {"sequence": int(event["sequence"]), "status": str(event["status"])}
            for event in operation.get("events") or []
        ],
        "effects": [_operation_effect_receipt(effect) for effect in operation.get("effects") or []],
    }


def _signature_receipt(signature: dict[str, Any] | None) -> dict[str, Any] | None:
    if signature is None:
        return None

    def run_receipt(run: dict[str, Any]) -> dict[str, Any]:
        trace = run["action_trace"]
        reads = run.get("reads") or []
        return {
            "run_id": str(run["run_id"]),
            "decision_id": str(run["decision_id"]),
            "status": str(run["status"]),
            "reflected_memory_id": run.get("reflected_memory_id"),
            "selection_fingerprint": str(trace["selection"]["fingerprint"]),
            "recommendation_id": str(trace["recommendation"]["id"]),
            "approval_approved": bool(trace["approval"]["approved"]),
            "execution_status": str(trace["execution"]["status"]),
            "read_memory_ids": [str(read["memory_id"]) for read in reads],
            "read_count": len(reads),
            "downstream_lineage_edge_count": sum(
                int(read["downstream_lineage_edges"]) for read in reads
            ),
        }

    return {
        "namespace": str(signature["namespace"]),
        "operation_id": str(signature["operation_id"]),
        "invalidated_memory_ids": [str(value) for value in signature["invalidated_memory_ids"]],
        "bad": run_receipt(signature["bad"]),
        "corrected": run_receipt(signature["corrected"]),
    }


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
