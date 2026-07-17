"""Opt-in browser acceptance test for the deployed or local incident cockpit."""

import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

BASE_URL = os.environ.get("HINDSIGHT_BROWSER_BASE_URL")
OPERATOR_TOKEN = os.environ.get("HINDSIGHT_BROWSER_OPERATOR_TOKEN")

requires_browser = pytest.mark.skipif(
    not BASE_URL or not OPERATOR_TOKEN,
    reason="browser URL and operator token are not configured",
)
requires_hosted_acceptance = pytest.mark.skipif(
    os.environ.get("RUN_HOSTED_ACCEPTANCE") != "1",
    reason="hosted database acceptance is opt-in",
)


def test_reset_resubscribes_before_loading_the_fresh_namespace():
    source = (Path(__file__).parents[1] / "src/hindsight/web/app.js").read_text()
    reset = source.split("async function resetDemo()", 1)[1].split(
        "async function poisonDemo()", 1
    )[0]

    assert re.search(
        r"state\.namespace = payload\.namespace;\s+subscribeSocket\(\);",
        reset,
    )
    assert reset.index("subscribeSocket();") < reset.index("await Promise.all(")


def test_live_events_from_a_previous_namespace_are_ignored():
    source = (Path(__file__).parents[1] / "src/hindsight/web/app.js").read_text()
    handler = source.split("function handleLiveEvent(payload)", 1)[1].split(
        "function setBusy", 1
    )[0]

    assert "payload.namespace !== state.namespace" in handler


def test_operation_polling_uses_deployed_retry_budget_and_preserves_last_status():
    source = (Path(__file__).parents[1] / "src/hindsight/web/app.js").read_text()
    polling = source.split("async function waitForOperation(operationId)", 1)[1].split(
        "function connectEvents()", 1
    )[0]

    assert "config.operationPollSeconds" in polling
    assert "last status" in polling
    assert "data-operation-id" in source
    assert "data-operation-type" in source
    assert "data-operation-status" in source


@requires_browser
def test_operator_can_run_and_explain_signature_workflow():
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.support import expected_conditions as expected
    from selenium.webdriver.support.ui import WebDriverWait

    options = Options()
    options.add_argument("-headless")
    driver = webdriver.Firefox(options=options)
    driver.set_script_timeout(120)
    wait = WebDriverWait(driver, 90)
    operation_id = None
    try:
        driver.set_window_size(1440, 1000)
        driver.get(_browser_url(namespace=f"live-browser:{uuid4()}"))
        wait.until(expected.presence_of_element_located((By.ID, "memories")))
        _capture_console_errors(driver)
        wait.until(lambda browser: "Live" in browser.find_element(By.ID, "connection").text)
        assert driver.find_element(By.ID, "startRun").get_attribute("disabled")

        driver.find_element(By.ID, "operatorButton").click()
        driver.find_element(By.ID, "operatorToken").send_keys(OPERATOR_TOKEN)
        driver.find_element(By.CSS_SELECTOR, "#operatorForm button[type=submit]").click()
        wait.until_not(lambda browser: browser.find_element(By.ID, "startRun").get_attribute("disabled"))

        driver.find_element(By.ID, "resetDemo").click()
        wait.until(lambda browser: "1 live" in browser.find_element(By.ID, "memoryCount").text)

        driver.find_element(By.ID, "startRun").click()
        wait.until(lambda browser: "awaiting approval" in browser.find_element(By.ID, "runStatus").text)
        driver.find_element(By.ID, "approveRun").click()
        wait.until(lambda browser: browser.find_element(By.ID, "runStatus").text == "completed")
        wait.until(lambda browser: "1 read" in browser.find_element(By.ID, "influenceCount").text)
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text
            == "2 live · 0 invalid"
        )
        namespace = driver.find_element(By.ID, "namespace").text
        if os.environ.get("RUN_HOSTED_ACCEPTANCE") == "1":
            _assert_typed_reflection(namespace)

        if os.environ.get("RUN_HOSTED_ACCEPTANCE") == "1":
            changefeed_event = _poison_and_wait_for_changefeed_event(driver, namespace)
            assert changefeed_event["type"] == "memory"
            assert changefeed_event["namespace"] == namespace
            assert changefeed_event["data"]["memory"]["writer"] == "demo.poison"
            driver.find_element(By.ID, "liveButton").click()
        else:
            driver.find_element(By.ID, "poisonDemo").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text
            == "3 live · 0 invalid"
        )
        driver.find_element(By.ID, "previewRewind").click()
        wait.until(lambda browser: "versions will close" in browser.find_element(By.ID, "rewindPreview").text)
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
            lambda browser: "0 invalid"
            not in browser.find_element(By.ID, "memoryCount").text
        )

        timeline = driver.find_element(By.ID, "timeline")
        assert not timeline.get_attribute("disabled")
        driver.execute_script(
            "arguments[0].value = 0; arguments[0].dispatchEvent(new Event('input'));",
            timeline,
        )
        wait.until(lambda browser: browser.find_element(By.ID, "beliefTitle").text == "Beliefs As Of")
        wait.until(lambda browser: "0 invalid" in browser.find_element(By.ID, "memoryCount").text)
        assert not driver.find_elements(By.CSS_SELECTOR, ".memory.invalidated")

        driver.find_element(By.ID, "liveButton").click()
        wait.until(lambda browser: browser.find_element(By.ID, "beliefTitle").text == "Current Beliefs")
        wait.until(
            lambda browser: "0 invalid"
            not in browser.find_element(By.ID, "memoryCount").text
        )
    finally:
        _write_browser_evidence(driver, operation_id=operation_id)
        driver.quit()


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
        raise AssertionError(f"rewind did not complete before its retry budget: {observed}") from exc


def _capture_console_errors(driver) -> None:
    driver.execute_script(
        """
        window.__HINDSIGHT_CONSOLE_ERRORS = [];
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
        """
    )


def _write_browser_evidence(driver, *, operation_id: str | None) -> None:
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
        console_errors = driver.execute_script(
            "return window.__HINDSIGHT_CONSOLE_ERRORS || [];"
        )
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
                "capture_errors": capture_errors,
            },
            default=str,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@requires_browser
@requires_hosted_acceptance
def test_review_required_memory_renders_as_active_in_its_historical_snapshot():
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.support import expected_conditions as expected
    from selenium.webdriver.support.ui import WebDriverWait

    namespace, cutoff = _prepare_review_required_fixture()
    options = Options()
    options.add_argument("-headless")
    driver = webdriver.Firefox(options=options)
    wait = WebDriverWait(driver, 90)
    try:
        driver.set_window_size(1440, 1000)
        driver.get(_browser_url(namespace=namespace))
        wait.until(expected.presence_of_element_located((By.ID, "memories")))
        wait.until(
            lambda browser: browser.find_element(By.CSS_SELECTOR, ".memory-status").text
            == "review required"
        )
        assert "1 live · 0 invalid" in driver.find_element(By.ID, "memoryCount").text

        driver.get(_browser_url(namespace=namespace, as_of=cutoff))
        wait.until(lambda browser: browser.find_element(By.ID, "beliefTitle").text == "Beliefs As Of")
        wait.until(
            lambda browser: browser.find_element(By.CSS_SELECTOR, ".memory-status").text
            == "current"
        )
        assert "review required" not in driver.find_element(By.ID, "memories").text
        assert not driver.find_elements(By.CSS_SELECTOR, ".memory.invalidated")
    finally:
        driver.quit()


def _poison_and_wait_for_changefeed_event(driver, namespace: str) -> dict:
    result = driver.execute_async_script(
        """
        const namespace = arguments[0];
        const done = arguments[arguments.length - 1];
        const socket = new WebSocket(window.HINDSIGHT_CONFIG.websocketUrl);
        let settled = false;
        const finish = (value) => {
          if (settled) return;
          settled = true;
          clearTimeout(timeout);
          socket.close();
          done(value);
        };
        const timeout = setTimeout(
          () => finish({error: "managed changefeed event timed out"}),
          90000
        );
        socket.addEventListener("open", () => {
          socket.send(JSON.stringify({type: "subscribe", namespace, run_id: null}));
          setTimeout(async () => {
            try {
              const response = await fetch("/v1/demo/poison-rewind/poison", {
                method: "POST",
                credentials: "same-origin",
                headers: {"content-type": "application/json"},
                body: JSON.stringify({namespace})
              });
              if (!response.ok) finish({error: await response.text()});
            } catch (error) {
              finish({error: String(error)});
            }
          }, 1500);
        });
        socket.addEventListener("message", (event) => {
          const payload = JSON.parse(event.data);
          const memory = payload.data?.memory;
          if (payload.type === "memory" && payload.namespace === namespace
              && memory?.writer === "demo.poison") finish(payload);
        });
        socket.addEventListener("error", () => finish({error: "websocket delivery failed"}));
        """,
        namespace,
    )
    assert "error" not in result, result.get("error")
    return result


def _assert_typed_reflection(namespace: str) -> None:
    from hindsight.db import connect, database_url

    with connect(database_url(), application_name="hindsight-hosted-browser-acceptance") as conn:
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
                "live.hosted_acceptance",
                f"policy:{token}",
                "Seed the governed evolution fixture",
            ),
        )
        store.open_decision(
            decision_id=decision_id,
            actor="live.hosted_acceptance",
            decision_kind="policy_derivation",
            purpose="Derive a remediation from the policy",
            namespace=child_namespace,
        )
        store.record_read(
            decision_id=decision_id,
            memory_kind="semantic",
            memory_id=str(root["id"]),
            reader="live.hosted_acceptance",
            purpose="Derive a remediation from the policy",
        )
        child = store.remember(
            memory_kind="semantic",
            namespace=child_namespace,
            content="Scale retry workers when the processor threshold is reached.",
            provenance=Provenance(
                "live.hosted_acceptance",
                f"derived:{token}",
                "Create a descendant requiring review after policy evolution",
            ),
            producer_decision_id=decision_id,
            parent_memory_ids=[str(root["id"])],
        )
    with connect(database_url(), application_name="hindsight-hosted-browser-acceptance") as conn:
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
        actor="live.hosted_acceptance",
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
        worker_id="live-hosted-browser-acceptance",
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
