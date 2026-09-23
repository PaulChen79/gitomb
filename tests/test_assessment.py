import json

import httpx2
import pytest
from conftest import commit
from typesafe_sdk import TypeSafeClient

import gitomb.assessment as assessment
from gitomb.assessment import PURPOSES, evaluate, evidence
from gitomb.git import git
from gitomb.scan import scan


def payload():
    return {
        "model": "jev-test",
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "answers": {
            "purpose": {
                "type": "choice",
                "choice": "temporary-debug",
                "confidence": 0.9,
                "probabilities": {k: 1.0 if k == "temporary-debug" else 0.0 for k in PURPOSES},
            },
            "unfinished": {"type": "noul", "noul": 0.1},
            "insufficient_context": {"type": "noul", "noul": 0.1},
        },
    }


@pytest.fixture
def report(repo):
    git(repo, "checkout", "-b", "debug")
    commit(repo, "print('debug')\n", "Temporary logging")
    git(repo, "checkout", "main")
    return scan([repo])


def connect(monkeypatch, handler):
    def client(**kwargs):
        return TypeSafeClient(**kwargs, transport=httpx2.MockTransport(handler))

    monkeypatch.setattr(assessment, "TypeSafeClient", client)


def test_real_sdk_wire_contract_cache_and_no_automatic_delete(report, tmp_path, monkeypatch):
    calls = []

    def handler(request):
        assert request.url.path == "/v1/systemone"
        calls.append(json.loads(request.content))
        return httpx2.Response(200, json=payload())

    connect(monkeypatch, handler)
    evaluate(report, tmp_path, api_key="test-key", workers=1)
    assert len(calls) == 1
    state = calls[0]["state"]
    assert "repo" not in state and "patch" not in state
    assert set(calls[0]["questions"]) == {"purpose", "unfinished", "insufficient_context"}
    item = next(i for i in report.items if i.name == "debug")
    assert item.assessment.model == "jev-test"
    assert item.recommendation == "review"
    evaluate(report, tmp_path, api_key="test-key", workers=1)
    assert len(calls) == 1
    evaluate(report, tmp_path, api_key="test-key", workers=1, refresh=True)
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["unauthorized", "malformed", "unknown-purpose"])
def test_api_failure_falls_back_without_leaking_response(report, tmp_path, monkeypatch, mode):
    def handler(request):
        response = payload()
        if mode == "unauthorized":
            return httpx2.Response(401, json={"error": "SECRET-DO-NOT-LOG"})
        if mode == "malformed":
            response["answers"]["unfinished"]["noul"] = "not a number"
        else:
            response["answers"]["purpose"]["choice"] = "delete-everything"
        return httpx2.Response(200, json=response)

    connect(monkeypatch, handler)
    evaluate(report, tmp_path, api_key="test-key", workers=1)
    item = next(i for i in report.items if i.name == "debug")
    assert item.assessment is None
    assert item.ai_error
    assert item.recommendation == "review"
    assert "SECRET-DO-NOT-LOG" not in report.model_dump_json()


def test_optional_patch_excludes_sensitive_paths_and_redacts_content(repo):
    git(repo, "checkout", "-b", "debug")
    (repo / ".env").write_text("DO_NOT_SEND=this-should-never-leave\n")
    (repo / "credentials.json").write_text('{"key": "also-never-send"}')
    commit(repo, "api_key=should-be-redacted\nprint('diagnostic')\n")
    git(repo, "checkout", "main")
    item = next(i for i in scan([repo]).items if i.name == "debug")
    metadata = evidence(item, False)
    assert "patch" not in metadata
    state = evidence(item, True)
    assert "diagnostic" in state["patch"]
    assert "REDACTED" in state["patch"]
    assert "should-be-redacted" not in json.dumps(state)
    assert "this-should-never-leave" not in json.dumps(state)
    assert "also-never-send" not in json.dumps(state)
