"""Opt-in browser acceptance test for the deployed or local incident cockpit."""

import json
import os
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


def test_reset_resubscribes_before_loading_the_fresh_namespace():
    source = (
        Path(__file__).parents[1] / "frontend/src/hooks/use-cockpit.ts"
    ).read_text()
    reset = source.split("const resetDemo = useCallback", 1)[1].split(
        "const poisonDemo = useCallback", 1
    )[0]

    assert "updateNamespace(payload.namespace);" in reset
    assert "subscribeSocket(payload.namespace);" in reset
    assert reset.index("updateNamespace(payload.namespace);") < reset.index(
        "subscribeSocket(payload.namespace);"
    )
    assert reset.index("subscribeSocket(payload.namespace);") < reset.index(
        "await loadIncidents("
    )


def test_live_events_from_a_previous_namespace_are_ignored():
    source = (
        Path(__file__).parents[1] / "frontend/src/hooks/use-cockpit.ts"
    ).read_text()
    handler = source.split("const handleLiveEvent = useCallback", 1)[1].split(
        "const subscribeSocket", 1
    )[0]

    assert "payload.namespace !== namespaceRef.current" in handler


def test_explicit_namespace_renders_before_incident_defaults_are_loaded():
    source = (
        Path(__file__).parents[1] / "frontend/src/hooks/use-cockpit.ts"
    ).read_text()
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
    surface = (
        Path(__file__).parents[1] / "frontend/src/components/cockpit.tsx"
    ).read_text()
    polling = hook.split("const waitForOperation = useCallback", 1)[1].split(
        "const executeRewind", 1
    )[0]

    assert "config.operationPollSeconds" in polling
    assert "last status" in polling
    assert polling.index('includes(operation.status)) {\n          return operation;') < polling.index(
        "const previous = snapshotRef.current;"
    )
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


@requires_browser
def test_cross_episode_lesson_identity_chain_is_inspectable():
    from hindsight.cross_episode import run_cross_episode_demo
    from hindsight.db import database_url
    from hindsight.server_tenants import public_demo_tenant_id
    from hindsight.tenant import tenant_scope
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.support.ui import WebDriverWait

    with tenant_scope(public_demo_tenant_id()):
        result = run_cross_episode_demo(
            db_url=database_url(),
            namespace=f"live-lesson-chain:{uuid4()}",
        )
    trace = result.lesson_trace
    options = Options()
    options.add_argument("-headless")
    driver = webdriver.Firefox(options=options)
    wait = WebDriverWait(driver, 90)
    try:
        driver.set_window_size(1440, 1000)
        driver.get(_browser_url(namespace=result.namespace))
        _capture_console_errors(driver)
        chain = wait.until(
            lambda browser: browser.find_element(By.ID, "lessonIdentityChain")
            if browser.find_element(By.ID, "lessonTraceStatus").text == "Identity complete"
            else False
        )
        expected = {
            "data-incident-id": str(trace["source_incident"]["id"]),
            "data-consolidation-id": str(trace["consolidation"]["job_id"]),
            "data-producer-decision-id": str(
                trace["consolidation"]["producer_decision_id"]
            ),
            "data-lesson-memory-id": str(trace["lesson"]["memory_id"]),
            "data-lesson-belief-id": str(trace["lesson"]["belief_id"]),
            "data-retrieval-id": str(trace["retrieval"]["retrieval_id"]),
            "data-read-id": str(trace["retrieval"]["read_id"]),
            "data-embedding-profile-id": str(trace["embedding_profile"]["id"]),
            "data-consumer-decision-id": str(
                trace["consumer_decision"]["decision_id"]
            ),
        }
        for attribute, value in expected.items():
            assert chain.get_attribute(attribute) == value
        assert chain.get_attribute("data-lineage-edge-ids") == ",".join(
            str(edge["id"]) for edge in trace["lineage_edges"]
        )
        assert "Version" in chain.text
        assert "verified edge" in chain.text
        assert "content" not in chain.text.lower()
        assert driver.execute_script("return window.__HINDSIGHT_CONSOLE_ERRORS || [];") == []
        assert driver.execute_script("return window.__HINDSIGHT_VISIBLE_ERRORS || [];") == []
    finally:
        driver.quit()


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
    signature = None
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

        walkthrough = driver.find_element(By.ID, "operatorWalkthrough")
        assert not walkthrough.is_displayed()
        driver.find_element(By.ID, "walkthroughToggle").click()
        wait.until(lambda browser: browser.find_element(By.ID, "operatorWalkthrough").is_displayed())
        assert "Reset the replay" in walkthrough.text
        assert "Inspect history" in walkthrough.text
        driver.find_element(By.ID, "walkthroughToggle").click()
        wait.until_not(lambda browser: browser.find_element(By.ID, "operatorWalkthrough").is_displayed())

        previous_namespace = driver.find_element(By.ID, "namespace").text
        driver.find_element(By.ID, "resetDemo").click()
        namespace = wait.until(
            lambda browser: _ready_reset_namespace(browser, previous_namespace)
        )

        driver.find_element(By.ID, "poisonDemo").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text
            == "2 live · 0 invalid"
        )

        driver.find_element(By.ID, "startRun").click()
        wait.until(lambda browser: "awaiting approval" in browser.find_element(By.ID, "runStatus").text)
        wait.until(
            lambda browser: "Poisoned memory"
            in browser.find_element(By.ID, "influenceList").text
        )
        bad_plan = driver.find_element(By.ID, "planText").text
        assert "certificate" in bad_plan.lower()
        driver.find_element(By.ID, "rejectRun").click()
        wait.until(lambda browser: browser.find_element(By.ID, "runStatus").text == "rejected")
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
            lambda browser: browser.find_element(By.ID, "memoryCount").text
            == "1 live · 2 invalid"
        )
        wait.until(
            lambda browser: browser.find_element(By.ID, "executeRewind").text
            == "Execute rewind"
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
            lambda browser: browser.find_element(By.ID, "memoryCount").text
            == "1 live · 2 invalid"
        )

        driver.find_element(By.ID, "startRun").click()
        wait.until(lambda browser: "awaiting approval" in browser.find_element(By.ID, "runStatus").text)
        wait.until(lambda browser: "1 read" in browser.find_element(By.ID, "influenceCount").text)
        assert "Poisoned memory" not in driver.find_element(By.ID, "influenceList").text
        corrected_plan = driver.find_element(By.ID, "planText").text
        assert "retry" in corrected_plan.lower()
        assert "certificate" not in corrected_plan.lower()
        driver.find_element(By.ID, "approveRun").click()
        wait.until(lambda browser: browser.find_element(By.ID, "runStatus").text == "completed")
        wait.until(
            lambda browser: browser.find_element(By.ID, "memoryCount").text
            == "2 live · 2 invalid"
        )
        _assert_typed_reflection(namespace)
        signature = _assert_signature_trace(namespace=namespace, operation_id=operation_id)
        assert driver.execute_script("return window.__HINDSIGHT_CONSOLE_ERRORS || [];") == []
        assert driver.execute_script("return window.__HINDSIGHT_VISIBLE_ERRORS || [];") == []
    finally:
        _write_browser_evidence(driver, operation_id=operation_id, signature=signature)
        driver.quit()


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
        raise AssertionError(f"rewind did not complete before its retry budget: {observed}") from exc


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
                SELECT run.id, run.decision_id, run.status, run.plan,
                       run.reflected_memory_id, read.id, read.memory_id,
                       memory.writer, read.rank, read.distance
                FROM agent_runs AS run
                LEFT JOIN memory_reads AS read ON read.decision_id = run.decision_id
                LEFT JOIN semantic_memories AS memory ON memory.id = read.semantic_memory_id
                WHERE run.namespace = %s
                ORDER BY run.created_at, read.rank
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
                "reflected_memory_id": str(row[4]) if row[4] else None,
                "reads": [],
            },
        )
        if row[5] is not None:
            run["reads"].append(
                {
                    "read_id": str(row[5]),
                    "memory_id": str(row[6]),
                    "writer": row[7],
                    "rank": row[8],
                    "distance": row[9],
                }
            )

    runs = list(grouped.values())
    assert len(runs) == 2
    bad, corrected = runs
    assert bad["status"] == "rejected"
    assert "certificate" in bad["plan"].lower()
    assert any(read["writer"] == "demo.poison" for read in bad["reads"])
    assert corrected["status"] == "completed"
    assert "retry" in corrected["plan"].lower()
    assert "certificate" not in corrected["plan"].lower()
    assert any(read["writer"] == "demo.seed" for read in corrected["reads"])
    assert all(read["writer"] != "demo.poison" for read in corrected["reads"])
    assert operation is not None and operation[2] == "completed"

    with MemoryStore(url=database_url()) as store:
        memories = store.list_current_semantic(namespace=namespace, limit=100)
        current_writers = {memory["writer"] for memory in memories}
        poison_id = next(
            read["memory_id"]
            for read in bad["reads"]
            if read["writer"] == "demo.poison"
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
