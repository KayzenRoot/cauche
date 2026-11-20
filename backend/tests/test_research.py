import json

import httpx
import pytest

from app.research import EvidenceRequest, MODEL, ResearchError, inspect_kalshi

RULE = "The NOAA report is the resolution source."
TICKER = "TEST-26"


def market():
    return httpx.Response(200, json={"market": {
        "ticker": TICKER, "rules_primary": RULE, "rules_secondary": "",
        "close_time": "2026-11-20T13:00:00Z",
    }})


@pytest.mark.asyncio
async def test_public_rules_work_without_a_model_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def upstream(request):
        assert str(request.url) == "https://external-api.kalshi.com/trade-api/v2/markets/" + TICKER
        assert "Authorization" not in request.headers
        return market()

    result = await inspect_kalshi(EvidenceRequest(ticker="test-26"), transport=httpx.MockTransport(upstream))
    assert result["model_status"] == "NOT_CONFIGURED"
    assert result["decision_status"] == "REVIEW_REQUIRED"
    assert result["rules"]["primary"] == RULE
    assert result["extraction"] is None


@pytest.mark.asyncio
async def test_model_can_only_return_validated_cited_evidence(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")

    def upstream(request):
        if request.url.host == "external-api.kalshi.com":
            assert "Authorization" not in request.headers
            return market()
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        model_request = json.loads(request.content)
        assert model_request["model"] == MODEL == "deepseek-flash"
        assert "tools" not in model_request
        content = {
            "event": "Weather", "deadline": "2026-11-20T13:00:00Z", "resolution_source": "NOAA report",
            "ambiguous": False, "probability": 0.9, "facts": [
                {"source_id": "kalshi", "quote": RULE, "claim": "NOAA resolves the market."},
                {"source_id": "source-1", "quote": "not in evidence", "claim": "Fabricated claim."},
            ],
        }
        return httpx.Response(200, json={"choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(content)}},
        ]})

    result = await inspect_kalshi(EvidenceRequest(ticker=TICKER, sources=[{
        "url": "https://weather.gov/example", "published_at": "2020-01-01T08:00:00Z", "text": "Example.",
    }]), transport=httpx.MockTransport(upstream))
    assert result["model_status"] == "EXTRACTED"
    assert result["decision_status"] == "REVIEW_REQUIRED"
    assert result["extraction"]["ambiguous"] is True
    assert result["extraction"]["deadline"] is None
    assert "probability" not in result["extraction"]
    assert len(result["extraction"]["facts"]) == 1


@pytest.mark.asyncio
async def test_market_error_is_redacted():
    def upstream(_request):
        return httpx.Response(503, text="unexpected provider detail")

    with pytest.raises(ResearchError, match="KALSHI_RULES_UNAVAILABLE") as error:
        await inspect_kalshi(EvidenceRequest(ticker=TICKER), transport=httpx.MockTransport(upstream))
    assert error.value.status == 502



@pytest.mark.asyncio
async def test_public_only_inspection_skips_configured_provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")

    def upstream(request):
        assert request.url.host == "external-api.kalshi.com"
        return market()

    result = await inspect_kalshi(EvidenceRequest(ticker=TICKER, use_model=False),
                                  transport=httpx.MockTransport(upstream))
    assert result["model_status"] == "SKIPPED"
    assert result["extraction"] is None
