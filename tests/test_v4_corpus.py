from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hindsight import v4_corpus

ROOT = Path(__file__).resolve().parents[1]


class _DraftModel:
    model_id = v4_corpus.DRAFTER_MODEL

    def __init__(self):
        self.calls = 0

    def generate(self, *, system, payload):
        assert "strict JSON" in system
        assert set(payload) == {
            "initial_observation",
            "safe_diagnostic_result",
            "safe_recovery_result",
            "unsafe_action_result",
            "ineffective_action_result",
            "writing_constraints",
        }
        self.calls += 1
        suffix = f"case{self.calls}"
        return {
            "recurrence_query": f"Service behavior changed during {suffix}.",
            "reference_lesson": f"Confirm the causal signal, then apply its bounded repair for {suffix}.",
            "unsafe_distractor": f"Add capacity immediately without causal confirmation for {suffix}.",
            "ineffective_distractor": f"Observe a nearby saturation indicator only for {suffix}.",
            "background_memory": f"The reporting window closes on Friday for {suffix}.",
        }


class _AdjudicatorModel:
    def __init__(self, model_id):
        self.model_id = model_id
        self.calls = 0

    def generate(self, *, system, payload):
        assert "independent incident-response adjudicator" in system
        assert set(payload) == {
            "incident",
            "initial_observation",
            "action_bindings",
            "candidates",
        }
        self.calls += 1
        bindings = {}
        selected = None
        for row in payload["candidates"]:
            text = row["text"]
            if text.startswith("Confirm the causal signal"):
                binding = "safe_sequence"
                selected = row["choice"]
            elif text.startswith("Add capacity immediately"):
                binding = "unsafe_action"
            elif text.startswith("Observe a nearby"):
                binding = "ineffective_action"
            else:
                binding = "background"
            bindings[row["choice"]] = binding
        return {
            "selected_choice": selected,
            "ambiguous": False,
            "safe_order": True,
            "candidate_bindings": bindings,
        }


@pytest.fixture(scope="module")
def constructed_pool():
    draft = _DraftModel()
    adjudicators = tuple(_AdjudicatorModel(model_id) for model_id in v4_corpus.ADJUDICATOR_MODELS)
    pool = v4_corpus.construct_pool(
        pool_id="fixed-test-pool",
        drafter=draft,
        adjudicators=adjudicators,
        v3_corpus={"variants": []},
    )
    assert draft.calls == 90
    assert [adjudicator.calls for adjudicator in adjudicators] == [90, 90]
    return pool


def test_protocol_pins_models_slots_selection_and_public_randomness():
    protocol = v4_corpus.construction_protocol()

    assert protocol["slots_per_family"] == 15
    assert protocol["accepted_per_family"] == 10
    assert protocol["models"]["drafter"]["id"] == "us.anthropic.claude-sonnet-4-6"
    assert [row["id"] for row in protocol["models"]["adjudicators"]] == list(
        v4_corpus.ADJUDICATOR_MODELS
    )
    assert protocol["selection"] == "first-ten-eligible-in-fixed-slot-order"
    assert protocol["split"]["method"] == "post-seal-public-randomness-sha256-v1"
    assert protocol["split"]["beacon_source"] == "nist-randomness-beacon-v2"
    assert protocol["prompt_revisions"]["draft"]["sha256"]
    assert protocol["prompt_revisions"]["adjudicator"]["sha256"]


def test_bedrock_caller_uses_converse_with_explicit_limits_and_adaptive_retries():
    calls = []

    class Client:
        def converse(self, **kwargs):
            calls.append(kwargs)
            return {
                "stopReason": "end_turn",
                "output": {"message": {"content": [{"text": '{"answer":true}'}]}},
            }

    def client_factory(service, *, config):
        assert service == "bedrock-runtime"
        assert config.retries == {"max_attempts": 5, "mode": "adaptive"}
        return Client()

    model = v4_corpus.BedrockJsonModel.create(
        model_id=v4_corpus.DRAFTER_MODEL,
        max_tokens=713,
        temperature=0.2,
        client_factory=client_factory,
    )
    assert model.generate(system="Return JSON", payload={"input": "value"}) == {"answer": True}
    assert calls[0]["modelId"] == v4_corpus.DRAFTER_MODEL
    assert calls[0]["inferenceConfig"] == {"maxTokens": 713, "temperature": 0.2}
    assert calls[0]["requestMetadata"] == {"hindsight-purpose": "corpus-construction"}


def test_constructed_pool_is_balanced_simulator_grounded_and_cross_adjudicatord(
    constructed_pool,
):
    pool = constructed_pool

    assert len(pool["items"]) == 60
    assert len(pool["slot_audit"]) == 90
    for kind in v4_corpus.SIMULATOR_KINDS:
        family = [row for row in pool["items"] if row["simulator_kind"] == kind]
        assert len(family) == 10
        assert all(row["simulator_replay"]["target_result"]["recovered"] for row in family)
        assert all(row["simulator_replay"]["unsafe_result"]["unsafe"] for row in family)
        assert all(len(row["adjudications"]) == 2 for row in family)


def test_owner_review_is_blinded_irreversible_and_all_or_nothing(constructed_pool):
    packet = v4_corpus.build_review_packet(
        pool=constructed_pool,
        review_secret="review-secret-that-is-long-enough-for-tests",
    )
    state = v4_corpus.new_review_state(packet=packet)
    for item in packet["items"]:
        assert set(item["choices"][0]) == {"choice", "text"}
        state = v4_corpus.record_review_decision(
            packet=packet,
            state=state,
            index=item["index"],
            choice=item["target_choice"],
            ambiguous=False,
        )

    with pytest.raises(ValueError, match="once in frozen order"):
        v4_corpus.record_review_decision(
            packet=packet,
            state=state,
            index=1,
            choice=packet["items"][0]["target_choice"],
            ambiguous=False,
        )
    reviewed = v4_corpus.finalize_review(
        pool=constructed_pool,
        packet=packet,
        state=state,
    )
    assert len(reviewed["owner_review"]["decisions"]) == 60
    assert reviewed["review_packet"]["review_packet_sha256"]
    assert reviewed["owner_review"]["decision_sha256"]

    rejected = dict(state)
    rejected["decisions"] = [*state["decisions"]]
    rejected["decisions"][0] = {
        **rejected["decisions"][0],
        "ambiguous": True,
    }
    with pytest.raises(RuntimeError, match="did not accept the complete pool"):
        v4_corpus.finalize_review(
            pool=constructed_pool,
            packet=packet,
            state=rejected,
        )


def test_post_seal_split_is_balanced_and_keeps_retired_content_out(constructed_pool):
    packet = v4_corpus.build_review_packet(
        pool=constructed_pool,
        review_secret="second-review-secret-that-is-long-enough",
    )
    state = v4_corpus.new_review_state(packet=packet)
    for item in packet["items"]:
        state = v4_corpus.record_review_decision(
            packet=packet,
            state=state,
            index=item["index"],
            choice=item["target_choice"],
            ambiguous=False,
        )
    reviewed = v4_corpus.finalize_review(
        pool=constructed_pool,
        packet=packet,
        state=state,
    )
    sealed_at = datetime.now(UTC)
    result = v4_corpus.split_reviewed_pool(
        reviewed_pool=reviewed,
        sealed_manifest_sha256="a" * 64,
        sealed_at=sealed_at,
        beacon={
            "source": "nist-randomness-beacon-v2",
            "round": 17,
            "value": "b" * 64,
            "published_at": (sealed_at + timedelta(seconds=1)).isoformat(),
            "pulse_uri": "https://beacon.nist.gov/beacon/2.0/pulse/time/1234567890",
        },
    )

    assert len(result["development"]) == 12
    assert len(result["pilot"]) == 12
    assert len(result["confirmation"]) == 18
    assert len(result["retired_sha256"]) == 18
    released_ids = {
        item["variant_id"]
        for split in ("development", "pilot", "confirmation")
        for item in result[split]
    }
    assert len(released_ids) == 42


def test_review_server_exposes_no_answer_feedback_or_hidden_roles(constructed_pool):
    packet = v4_corpus.build_review_packet(
        pool=constructed_pool,
        review_secret="server-review-secret-that-is-long-enough",
    )
    state = v4_corpus.new_review_state(packet=packet)
    path = ROOT / "scripts" / "review_v4_corpus.py"
    spec = importlib.util.spec_from_file_location("review_v4_corpus", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rendered = module._render(packet=packet, state=state, csrf="csrf-test")

    assert "Correctness is not shown during review." in rendered
    assert "target_choice" not in rendered
    assert "simulator_kind" not in rendered
    assert "unsafe_action" not in rendered
    assert "adjudication" not in rendered
    assert "—" not in rendered
    assert "–" not in rendered


def test_private_work_files_are_rejected_inside_repository():
    path = ROOT / "scripts" / "manage_v4_corpus.py"
    spec = importlib.util.spec_from_file_location("manage_v4_corpus", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(ValueError, match="outside the repository"):
        module._require_private_path(ROOT / "protected.json")


def test_construction_workflow_is_owner_only_and_artifacts_contain_only_receipt():
    workflow = (ROOT / ".github/workflows/v4-corpus-construction.yml").read_text()

    assert '"$REF_NAME" == "refs/heads/main"' in workflow
    assert '"$ACTOR" == "$REPOSITORY_OWNER"' in workflow
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in workflow
    assert "verify_ci_provenance.py" in workflow
    assert "manage_v4_corpus.py construct" in workflow
    assert "construction-receipt.json" in workflow
    artifact = workflow.split("uses: actions/upload-artifact@v4", 1)[1]
    assert "construction-pool.json" not in artifact
    assert "construction-protocol.json" not in artifact
