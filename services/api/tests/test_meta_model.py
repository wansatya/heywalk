"""Unit tests for the Muse Spark integration (AGENTS.md section 6).

Covers prompt building, structured-output parsing/validation, the live HTTP
client (via httpx.MockTransport -- no network), retry/timeout behavior, the
dev mock labels, and the generation-handler write guards.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.db import Store
from app.models.schemas import (
    FixResult,
    GeneratedFile,
    GenerationResult,
)
from app.services import generation as generation_service
from app.services import meta_model
from app.services.files import FilePathError
from app.services.meta_model import (
    DevMockMetaModelClient,
    FixEvidence,
    GenerationContext,
    HttpMetaModelClient,
    MetaModelNotConfiguredError,
    MetaModelResponseError,
    build_client,
    build_fix_messages,
    build_generate_messages,
    build_interpret_messages,
    extract_chat_text,
    extract_json_object,
    parse_fix_result,
    parse_generation_result,
    parse_interpretation,
)


@pytest.fixture()
def settings():
    return Settings(
        db_path=":memory:",
        model_api_key="[redacted]",
        meta_api_base_url="https://api.meta.ai/v1",
        muse_model="muse-spark-1.3",
        model_timeout_s=5.0,
        cors_origins=("*",),
    )


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(meta_model, "BACKOFF_BASE_S", 0.0)


def _generation_payload(**overrides):
    payload = {
        "summary": "Proportional go-to-goal with obstacle stop.",
        "assumptions": ["Flat arena", "Goal published on /goal_pose"],
        "files": [
            {"path": "controller.py", "content": "# node\nprint('go')\n"}
        ],
        "expected_behavior": "Drives to goal, stops at obstacles.",
        "safety_considerations": "Speeds clamped; estop halts motion.",
        "objectives": [
            {"type": "go_to", "target": None, "target_body": None}
        ],
    }
    payload.update(overrides)
    return payload


def _fix_payload(**overrides):
    payload = {
        "failure_hypothesis": "Turn gain too low near obstacles.",
        "evidence": ["collision at t=12.4s"],
        "proposed_change": "Raise avoidance gain.",
        "updated_files": [
            {"path": "controller.py", "content": "# fixed\nprint('go')\n"}
        ],
        "expected_improvement": "Clears the obstacle before contact.",
    }
    payload.update(overrides)
    return payload


def _interpret_payload(**overrides):
    payload = {
        "goal": "Reach the green target.",
        "safety_requirements": ["Stop on emergency-stop signal"],
        "assumptions": ["Static obstacles"],
        "needs_clarification": False,
        "clarification_question": "",
        "suggested_task": "Navigate to the green target; avoid obstacles.",
    }
    payload.update(overrides)
    return payload


def _chat_response(text: str, finish_reason: str | None = None) -> dict:
    choice: dict = {
        "index": 0,
        "message": {"role": "assistant", "content": text},
    }
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {
        "id": "chatcmpl-test",
        "model": "muse-spark-1.3",
        "choices": [choice],
    }


# ---- JSON extraction ----


def test_extract_json_object_raw():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_fenced():
    assert (
        extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    )


def test_extract_json_object_embedded_in_prose():
    assert (
        extract_json_object('Here it is:\n{"a": 1}\nDone.') == {"a": 1}
    )


def test_extract_json_object_rejects_garbage():
    with pytest.raises(MetaModelResponseError):
        extract_json_object("no json here")


def test_extract_json_object_rejects_empty():
    with pytest.raises(MetaModelResponseError):
        extract_json_object("   ")


def test_extract_json_object_rejects_non_object():
    with pytest.raises(MetaModelResponseError):
        extract_json_object("[1, 2, 3]")


# ---- structured-output parsing ----


def test_parse_generation_result_valid():
    result = parse_generation_result(_generation_payload())
    assert result.summary.startswith("Proportional")
    assert len(result.files) == 1
    assert result.files[0].path == "controller.py"


def test_parse_generation_result_from_text():
    result = parse_generation_result(json.dumps(_generation_payload()))
    assert isinstance(result, GenerationResult)


def test_parse_generation_result_missing_key():
    payload = _generation_payload()
    del payload["summary"]
    with pytest.raises(MetaModelResponseError, match="schema validation"):
        parse_generation_result(payload)


def test_parse_generation_result_rejects_empty_files():
    with pytest.raises(MetaModelResponseError, match="no files"):
        parse_generation_result(_generation_payload(files=[]))


def test_parse_generation_result_rejects_empty_path():
    payload = _generation_payload(
        files=[{"path": "  ", "content": "x = 1\n"}]
    )
    with pytest.raises(MetaModelResponseError, match="empty path"):
        parse_generation_result(payload)


def test_parse_generation_result_objectives():
    payload = _generation_payload()
    payload["objectives"] = [
        {"type": "kick", "target": None, "target_body": None},
        {"type": "go_to", "target": None, "target_body": None},
    ]
    result = parse_generation_result(
        payload,
        task_description="Kick the ball away from you, then walk to the flag.",
    )
    assert result.objectives is not None
    assert [o.type for o in result.objectives] == ["kick", "go_to"]
    single = _generation_payload()
    single["objectives"] = [{"type": "touch", "target": [1.9, 0.0]}]
    result = parse_generation_result(single)
    assert result.objectives is not None
    assert result.objectives[0].type == "touch"
    assert result.objectives[0].target == [1.9, 0.0]
    assert parse_generation_result(_generation_payload()).objectives is not None
    # Legacy singular shape still parses as one phase.
    legacy = _generation_payload()
    legacy["objectives"] = None
    legacy["objective"] = {"type": "go_to", "target": None, "target_body": None}
    parsed = parse_generation_result(legacy)
    assert parsed.objectives is not None
    assert [o.type for o in parsed.objectives] == ["go_to"]


def test_parse_generation_result_requires_objectives():
    """Omission must repair, not silently grade every task as flag runs."""
    payload = _generation_payload()
    del payload["objectives"]
    with pytest.raises(MetaModelResponseError, match="omitted the required"):
        parse_generation_result(payload)
    payload["objectives"] = None
    with pytest.raises(MetaModelResponseError, match="omitted the required"):
        parse_generation_result(payload)
    payload["objectives"] = []
    with pytest.raises(MetaModelResponseError, match="omitted the required"):
        parse_generation_result(payload)


def test_parse_generation_result_kick_task_must_declare_kick():
    """A kick task without a kick phase fails with a repair pointer."""
    payload = _generation_payload()
    payload["objectives"] = [
        {"type": "go_to", "target": None, "target_body": None}
    ]
    with pytest.raises(MetaModelResponseError, match="kick"):
        parse_generation_result(payload, task_description="Kick the ball away from you, then stop.")
    payload["objectives"] = [
        {"type": "kick", "target": None, "target_body": None},
        {"type": "go_to", "target": None, "target_body": None},
    ]
    result = parse_generation_result(
        payload, task_description="Kick the ball away from you, then walk to the flag."
    )
    assert result.objectives is not None
    assert [o.type for o in result.objectives] == ["kick", "go_to"]
    # "Walk to the ball" is go_to with a prop target, not a kick.
    payload["objectives"] = [
        {"type": "go_to", "target": None, "target_body": "env_ball"}
    ]
    result = parse_generation_result(
        payload, task_description="Walk to the ball and stop."
    )
    assert result.objectives is not None
    assert result.objectives[0].type == "go_to"


def test_parse_generation_result_rejects_bad_objective():
    payload = _generation_payload()
    payload["objectives"] = [{"type": "fly", "target": None}]
    with pytest.raises(MetaModelResponseError, match="unknown objectives"):
        parse_generation_result(payload)
    payload["objectives"] = [{"type": "go_to", "target": [9.0, 0.0]}]
    with pytest.raises(MetaModelResponseError, match="outside"):
        parse_generation_result(payload)
    payload["objectives"] = [{"type": "wander", "target": [0.5, 0.0]}]
    with pytest.raises(MetaModelResponseError, match="wander"):
        parse_generation_result(payload)
    payload["objectives"] = [
        {"type": "wander", "target": None, "target_body": None},
        {"type": "go_to", "target": None, "target_body": None},
    ]
    with pytest.raises(MetaModelResponseError, match="must be last"):
        parse_generation_result(payload)


def test_parse_generation_result_kick_and_target_body_objective():
    payload = _generation_payload()
    payload["objectives"] = [
        {"type": "kick", "target": None, "target_body": None}
    ]
    result = parse_generation_result(payload)
    assert result.objectives is not None
    assert result.objectives[0].type == "kick"
    payload["objectives"] = [
        {
            "type": "go_to",
            "target": None,
            "target_body": "env_box_2",
        }
    ]
    result = parse_generation_result(payload)
    assert result.objectives is not None
    assert result.objectives[0].target_body == "env_box_2"


def test_parse_generation_result_rejects_bad_target_body():
    payload = _generation_payload()
    payload["objectives"] = [
        {
            "type": "go_to",
            "target": None,
            "target_body": "env_somewhere",
        }
    ]
    with pytest.raises(MetaModelResponseError, match="env_box_1"):
        parse_generation_result(payload)
    payload["objectives"] = [
        {
            "type": "kick",
            "target": None,
            "target_body": "env_ball",
        }
    ]
    with pytest.raises(MetaModelResponseError, match="go_to"):
        parse_generation_result(payload)
    payload["objectives"] = [{"type": "kick", "target": [0.5, 0.0]}]
    with pytest.raises(MetaModelResponseError, match="kick"):
        parse_generation_result(payload)


def test_generate_messages_microduck_declare_objective():
    """The duck brief makes the task text pick the run objectives."""
    context = GenerationContext(
        task_description="Walk to the wall and touch it.",
        existing_files=_composed_files(),
    )
    user = build_generate_messages(context)[1]["content"]
    assert '"objectives"' in user
    for kind in ("go_to", "touch", "wander", "kick"):
        assert kind in user
    assert "last phase" in user
    assert "/objective_state" in user


def test_generate_messages_microduck_name_the_props_and_the_gait():
    """'The box in front of you' must map onto a real body id, not a guess."""
    from app.simulator.microduck import prop_body_names

    context = GenerationContext(
        task_description="Kick the ball, then go to the box behind you.",
        existing_files=_composed_files(),
    )
    user = build_generate_messages(context)[1]["content"]
    for body in prop_body_names():
        assert body in user
    assert "target_body" in user
    assert "dynamic" in user
    # The spawn frame is what makes "in front" / "behind" resolvable.
    assert "+x" in user and "behind" in user
    # The controller is an adaptation of the installed scripted gait.
    assert "scripted gait" in user
    assert "adapt it to THIS task" in user


def test_parse_fix_result_valid():
    result = parse_fix_result(_fix_payload())
    assert isinstance(result, FixResult)
    assert result.updated_files[0].path == "controller.py"


def test_parse_fix_result_rejects_empty_updates():
    with pytest.raises(MetaModelResponseError, match="no files"):
        parse_fix_result(_fix_payload(updated_files=[]))


def test_parse_fix_result_missing_key():
    payload = _fix_payload()
    del payload["proposed_change"]
    with pytest.raises(MetaModelResponseError, match="schema validation"):
        parse_fix_result(payload)


def test_parse_interpretation_valid():
    result = parse_interpretation(_interpret_payload())
    assert result.goal == "Reach the green target."
    assert result.needs_clarification is False


def test_parse_interpretation_missing_goal():
    payload = _interpret_payload()
    del payload["goal"]
    with pytest.raises(MetaModelResponseError, match="schema validation"):
        parse_interpretation(payload)


# ---- chat payload extraction ----


def test_extract_chat_text_normal():
    assert extract_chat_text(_chat_response("hello")) == "hello"


def test_extract_chat_text_content_parts():
    payload = {
        "choices": [
            {"message": {"content": [{"type": "text", "text": "a"},
                                     {"type": "text", "text": "b"}]}}
        ]
    }
    assert extract_chat_text(payload) == "ab"


def test_extract_chat_text_no_choices():
    with pytest.raises(MetaModelResponseError, match="no choices"):
        extract_chat_text({"choices": []})


def test_extract_chat_text_empty():
    with pytest.raises(MetaModelResponseError, match="empty"):
        extract_chat_text(_chat_response("  "))


# ---- prompt builders ----


def test_generate_messages_cover_contract():
    context = GenerationContext(
        task_description="Reach the goal.",
        existing_files=[{"path": "controller.py", "content": "# old"}],
    )
    messages = build_generate_messages(context)
    assert [m["role"] for m in messages] == ["system", "user"]
    user = messages[1]["content"]
    assert "Reach the goal." in user
    assert "/cmd_vel" in user and "/emergency_stop" in user
    assert "controller.py" in user
    assert "NO time.sleep" in user


def _composed_files() -> list[dict[str, str]]:
    from app.simulator.microduck import compose_scene_xml

    return [
        {"path": "controller.py", "content": "# controller"},
        {"path": "robot.xml", "content": compose_scene_xml()},
    ]


def test_generate_messages_microduck_hide_legacy_nav():
    """Official-duck projects get joint grading, never the 2D paragraph."""
    context = GenerationContext(
        task_description="Navigate microduck to the flag.",
        existing_files=_composed_files(),
    )
    user = build_generate_messages(context)[1]["content"]
    assert "official MicroDuck" in user
    assert "STAND home pose" in user
    assert "left_hip_yaw=+0.0000" in user
    assert "/joint_commands" in user and "/cmd_vel" in user
    assert "Legacy 2D" not in user


def test_generate_messages_microduck_scope_to_controller_only():
    """Installed-duck prompts forbid re-emitting the giant robot.xml."""
    context = GenerationContext(
        task_description="Walk to the flag.",
        existing_files=_composed_files(),
    )
    user = build_generate_messages(context)[1]["content"]
    assert "exactly ONE file (controller.py)" in user
    assert "must not be rewritten" in user
    bare = build_generate_messages(
        GenerationContext(task_description="t", existing_files=[])
    )[1]["content"]
    assert "exactly ONE file" not in bare


def test_fix_messages_microduck_hide_legacy_nav():
    evidence = FixEvidence(
        task_description="t",
        controller_files=_composed_files(),
        run_status="failed",
        failure_summary="fell",
        exit_code=0,
        recent_logs=[],
        last_state=None,
        evaluation=None,
        metrics=None,
    )
    user = build_fix_messages(evidence)[1]["content"]
    assert "official MicroDuck" in user
    assert "Legacy 2D" not in user


def test_fix_messages_bound_evidence():
    evidence = FixEvidence(
        task_description="t",
        controller_files=[{"path": "controller.py", "content": "x"}],
        run_status="failed",
        failure_summary="collision",
        exit_code=1,
        recent_logs=["y" * 5000 for _ in range(200)],
        last_state={"t": 1.0},
        evaluation={"success": False},
        metrics={"collision_count": 2},
    )
    messages = build_fix_messages(evidence)
    user = messages[1]["content"]
    assert len(user) <= meta_model.MAX_EVIDENCE_CHARS + 20000
    assert "run_status: failed" in user
    assert "collision" in user
    assert "truncated" in user


def test_interpret_messages_carry_instruction():
    messages = build_interpret_messages("Go there fast.")
    assert messages[1]["content"].endswith("Go there fast.")


def test_json_schemas_match_pydantic_contracts():
    assert set(meta_model.GENERATION_JSON_SCHEMA["required"]) == {
        "summary", "assumptions", "files", "expected_behavior",
        "safety_considerations", "objectives",
    }
    assert set(meta_model.FIX_JSON_SCHEMA["required"]) == {
        "failure_hypothesis", "evidence", "proposed_change",
        "updated_files", "expected_improvement",
    }
    assert set(meta_model.INTERPRET_JSON_SCHEMA["required"]) == {
        "goal", "safety_requirements", "assumptions",
        "needs_clarification", "clarification_question", "suggested_task",
    }


# ---- live HTTP client (mocked transport) ----


def _client_with(handler, settings):
    transport = httpx.MockTransport(handler)
    return HttpMetaModelClient(settings, transport=transport)


def test_http_client_sends_documented_request(settings):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_chat_response(
            json.dumps(_generation_payload())))

    client = _client_with(handler, settings)
    result = asyncio.run(
        client.generate_controller(GenerationContext("task", []))
    )
    assert result.files[0].path == "controller.py"
    assert seen["url"] == "https://api.meta.ai/v1/chat/completions"
    assert seen["auth"] == "Bearer [redacted]"
    assert seen["body"]["model"] == "muse-spark-1.3"
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert seen["body"]["messages"][0]["role"] == "system"


def test_http_client_retries_then_succeeds(settings):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(
            200, json=_chat_response(json.dumps(_fix_payload()))
        )

    client = _client_with(handler, settings)
    evidence = FixEvidence("t", [], "failed", None, 1, [], None, None, None)
    result = asyncio.run(client.propose_fix(evidence))
    assert result.proposed_change == "Raise avoidance gain."
    assert len(attempts) == 3


def test_http_client_retries_server_errors(settings):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200, json=_chat_response(json.dumps(_generation_payload()))
        )

    client = _client_with(handler, settings)
    asyncio.run(client.generate_controller(GenerationContext("t", [])))
    assert len(attempts) == 2


def test_http_client_no_retry_on_client_error(settings):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, text="bad request")

    client = _client_with(handler, settings)
    with pytest.raises(MetaModelResponseError, match="HTTP 400"):
        asyncio.run(client.generate_controller(GenerationContext("t", [])))
    assert len(attempts) == 1


def test_http_client_gives_up_after_max_attempts(settings):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, text="down")

    client = _client_with(handler, settings)
    with pytest.raises(MetaModelResponseError, match="3 attempts"):
        asyncio.run(client.generate_controller(GenerationContext("t", [])))
    assert len(attempts) == meta_model.MAX_ATTEMPTS


def test_http_client_repairs_malformed_output_once(settings):
    """A bad reply gets one automatic repair retry carrying the complaint."""
    bodies: list[dict] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        bodies.append(json.loads(request.content.decode()))
        if calls == 1:
            return httpx.Response(200, json=_chat_response("not json{{{"))
        return httpx.Response(
            200, json=_chat_response(json.dumps(_generation_payload()))
        )

    client = _client_with(handler, settings)
    result = asyncio.run(
        client.generate_controller(GenerationContext("t", []))
    )
    assert result.files[0].path == "controller.py"
    assert calls == 2
    repair_message = bodies[1]["messages"][-1]["content"]
    assert "could not be used" in repair_message
    assert "valid JSON" in repair_message


def test_http_client_repair_gives_up_with_diagnostics(settings):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(200, json=_chat_response("not json{{{" ))

    client = _client_with(handler, settings)
    with pytest.raises(MetaModelResponseError, match="unusable after 2 attempts"):
        asyncio.run(client.generate_controller(GenerationContext("t", [])))
    assert len(attempts) == 2


def test_http_client_truncated_reply_fails_fast(settings):
    """finish_reason=length is reported as truncation, not retried blindly."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(
            200,
            json=_chat_response('{"summary": "cut off', finish_reason="length"),
        )

    client = _client_with(handler, settings)
    with pytest.raises(MetaModelResponseError, match="truncated"):
        asyncio.run(client.generate_controller(GenerationContext("t", [])))
    assert len(attempts) == 1


def test_http_client_times_out_with_bounded_retries(settings):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.TimeoutException("slow", request=request)

    client = _client_with(handler, settings)
    with pytest.raises(MetaModelResponseError, match="timed out"):
        asyncio.run(client.generate_controller(GenerationContext("t", [])))
    assert len(attempts) == meta_model.MAX_ATTEMPTS


def test_http_client_errors_never_contain_key(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    client = _client_with(handler, settings)
    with pytest.raises(MetaModelResponseError) as excinfo:
        asyncio.run(client.generate_controller(GenerationContext("t", [])))
    assert "test-key" not in str(excinfo.value)


def test_http_client_interpret_task(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_chat_response(json.dumps(_interpret_payload()))
        )

    client = _client_with(handler, settings)
    result = asyncio.run(client.interpret_task("Go."))
    assert result.goal == "Reach the green target."


# ---- build_client ----


def test_build_client_requires_key(settings):
    settings_no_key = Settings(
        db_path=":memory:",
        model_api_key="",
        meta_api_base_url=settings.meta_api_base_url,
        muse_model=settings.muse_model,
        model_timeout_s=5.0,
        cors_origins=("*",),
    )
    with pytest.raises(MetaModelNotConfiguredError, match="MODEL_API_KEY"):
        build_client(settings_no_key)


def test_build_client_returns_live_client(settings):
    client = build_client(settings)
    assert isinstance(client, HttpMetaModelClient)
    assert client.model_name == "muse-spark-1.3"
    assert not isinstance(client, DevMockMetaModelClient)


# ---- dev mock ----


def test_dev_mock_labels_everything():
    mock = DevMockMetaModelClient()
    assert "NOT live" in mock.model_name
    generated = asyncio.run(
        mock.generate_controller(GenerationContext("task", []))
    )
    assert meta_model.DEV_MOCK_LABEL in generated.summary
    assert meta_model.DEV_MOCK_LABEL in generated.expected_behavior
    assert meta_model.DEV_MOCK_LABEL in generated.safety_considerations
    fixed = asyncio.run(
        mock.propose_fix(
            FixEvidence("t", [], "failed", None, 1, [], None, None, None)
        )
    )
    assert meta_model.DEV_MOCK_LABEL in fixed.failure_hypothesis
    interpreted = asyncio.run(mock.interpret_task("task"))
    assert meta_model.DEV_MOCK_LABEL in interpreted.goal


# ---- generation-handler write guards ----


class _EmptyClient:
    model_name = "test-empty"

    async def generate_controller(self, context):
        return GenerationResult(
            summary="s", assumptions=[], files=[],
            expected_behavior="", safety_considerations="",
        )

    async def propose_fix(self, evidence):
        return FixResult(
            failure_hypothesis="h", evidence=[], proposed_change="c",
            updated_files=[], expected_improvement="",
        )


class _TraversalClient:
    model_name = "test-traversal"

    async def generate_controller(self, context):
        return GenerationResult(
            summary="s", assumptions=[],
            files=[
                GeneratedFile(path="controller.py", content="# ok"),
                GeneratedFile(path="../evil.py", content="# bad"),
            ],
            expected_behavior="", safety_considerations="",
        )

    async def propose_fix(self, evidence):
        raise AssertionError("unused")


def _test_settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "t.db"),
        model_api_key="",
        meta_api_base_url="https://api.meta.ai/v1",
        muse_model="muse-spark-1.3",
        model_timeout_s=5.0,
        cors_origins=("*",),
    )


def test_handle_generate_rejects_empty_files(tmp_path):
    settings = _test_settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    with pytest.raises(MetaModelResponseError, match="no files"):
        asyncio.run(
            generation_service.handle_generate(
                store, settings, project["id"], None,
                client=_EmptyClient(),
            )
        )
    assert store.list_files(project["id"]) == []


def test_handle_generate_two_phase_write(tmp_path):
    settings = _test_settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    with pytest.raises(FilePathError):
        asyncio.run(
            generation_service.handle_generate(
                store, settings, project["id"], None,
                client=_TraversalClient(),
            )
        )
    # The valid first file must not be left behind after the bad one.
    assert store.list_files(project["id"]) == []


def test_handle_generate_dev_mock_end_to_end(tmp_path):
    settings = _test_settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    result = asyncio.run(
        generation_service.handle_generate(
            store, settings, project["id"], None,
            client=DevMockMetaModelClient(),
        )
    )
    assert "DEV MOCK" in result.summary
    stored = store.get_file(project["id"], "controller.py")
    assert stored is not None and stored["version"] == 1


def test_handle_fix_rejects_empty_updates(tmp_path):
    settings = _test_settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    run = store.create_run(
        project["id"], parent_run_id=None, controller_version=0,
        status="failed", world_config=None, seed=0,
    )
    with pytest.raises(MetaModelResponseError, match="no files"):
        asyncio.run(
            generation_service.handle_fix(
                store, settings, project["id"], run["id"],
                client=_EmptyClient(),
            )
        )


class _ExplodingClient:
    model_name = "test-exploding"

    async def generate_controller(self, context):
        raise MetaModelResponseError(
            "Model API returned an empty assistant message."
        )

    async def propose_fix(self, evidence):
        raise MetaModelResponseError(
            "Model API returned an empty assistant message."
        )


def test_handle_generate_attaches_trace_on_success(tmp_path):
    settings = _test_settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    result = asyncio.run(
        generation_service.handle_generate(
            store, settings, project["id"], None,
            client=DevMockMetaModelClient(),
        )
    )
    stages = [step.stage for step in result.trace]
    assert stages == ["prepare_prompt", "model_request", "validate_files"]
    assert all(step.status == "ok" for step in result.trace)
    assert result.model_name == DevMockMetaModelClient().model_name
    assert isinstance(result.latency_ms, int)


def test_handle_generate_attaches_trace_and_hint_on_error(tmp_path):
    settings = _test_settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    with pytest.raises(MetaModelResponseError) as excinfo:
        asyncio.run(
            generation_service.handle_generate(
                store, settings, project["id"], None,
                client=_ExplodingClient(),
            )
        )
    exc = excinfo.value
    assert exc.trace is not None
    stages = {step["stage"]: step["status"] for step in exc.trace}
    assert stages == {"prepare_prompt": "ok", "model_request": "error"}
    assert exc.hint is not None and "transient" in exc.hint


def test_hint_for_error_mapping():
    assert (
        meta_model.hint_for_error(MetaModelNotConfiguredError("x")) is None
    )
    assert "did not answer in time" in meta_model.hint_for_error(
        MetaModelResponseError("request timed out after 5s")
    )
    assert "Rate limited" in meta_model.hint_for_error(
        MetaModelResponseError("failed: HTTP 429: slow down.")
    )
    assert "JSON shape" in meta_model.hint_for_error(
        MetaModelResponseError("field 'files' is missing.")
    )
    assert (
        meta_model.hint_for_error(MetaModelResponseError("something odd"))
        is None
    )


def test_http_attempts_recorded_for_trace(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    settings = _test_settings(tmp_path)
    client = meta_model.HttpMetaModelClient(
        Settings(
            db_path=settings.db_path,
            model_api_key="key",
            meta_api_base_url="https://api.meta.ai/v1",
            muse_model="muse-spark-1.3",
            model_timeout_s=5.0,
            cors_origins=("*",),
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(MetaModelResponseError):
        asyncio.run(
            client.generate_controller(
                meta_model.GenerationContext(
                    task_description="t", existing_files=[]
                )
            )
        )
    assert [a["outcome"] for a in client.last_attempts] == ["http_500"] * 3
