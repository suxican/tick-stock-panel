"""模型只能解释程序证据，不能增加价格、数量或伪造证据引用。"""
import json

import pytest

from app.services import holding_interpretation as module
from app.services.holding_analysis_store import ResearchError


@pytest.fixture
def report():
    return {"rows": [{"key": "h_a", "symbol": "600000.SH", "name": "测试",
                      "evidence": ["趋势走弱", "板块覆盖不足"], "warnings": [],
                      "plan": {"summary": "优先复核风险", "priority": "risk"}}]}


@pytest.mark.asyncio
async def test_valid_explanation_reuses_provider_and_omits_account_facts(monkeypatch, report):
    calls = []

    async def generate(messages, **kwargs):
        calls.append(messages)
        return json.dumps({"summary_focus": "data", "observations": [
            {"key": "h_a", "focus": "sector", "evidence_indices": [1]}]}, ensure_ascii=False)

    monkeypatch.setattr(module, "generate_ai_text", generate)
    report["account_id"] = "private-account"
    report["rows"][0]["qty"] = 50000
    result = await module.interpret_report(report)
    assert result["observations"][0]["evidence_indices"] == [1]
    assert "private-account" not in calls[0][1]["content"]
    assert "50000" not in calls[0][1]["content"]
    assert report["rows"][0]["plan"]["priority"] == "risk"


@pytest.mark.asyncio
@pytest.mark.parametrize("observation", [
    {"key": "unknown", "focus": "risk", "evidence_indices": [0]},
    {"key": "h_a", "focus": "risk", "evidence_indices": [9]},
    {"key": "h_a", "focus": "risk", "evidence_indices": [True]},
    {"key": "h_a", "focus": "risk", "evidence_indices": []},
    {"key": "h_a", "focus": "risk", "text": "支撑为999元", "evidence_indices": [0]},
    {"key": "h_a", "focus": "risk", "text": "跌破九元立即清仓", "evidence_indices": [0]},
])
async def test_fabricated_evidence_or_new_numbers_are_rejected(monkeypatch, report, observation):
    async def generate(*_a, **_kw):
        return json.dumps({"summary_focus": "risk", "observations": [observation]})

    monkeypatch.setattr(module, "generate_ai_text", generate)
    with pytest.raises(ResearchError):
        await module.interpret_report(report)


@pytest.mark.asyncio
async def test_failure_does_not_expose_provider_secrets(monkeypatch, report):
    async def generate(*_a, **_kw):
        raise ValueError("token=secret-key")

    monkeypatch.setattr(module, "generate_ai_text", generate)
    with pytest.raises(ResearchError) as error:
        await module.interpret_report(report)
    assert "secret-key" not in str(error.value)
