"""MVP-01B: Ollama structured interpretation uses only one network call."""
import json
from io import BytesIO

import pytest

from persona_agent.conversation_contracts import ModelMessage,ModelUnavailable
from persona_agent.model_gateway import OllamaChatGateway


class FakeResponse:
    def __init__(self,payload):
        self.raw=BytesIO(payload)
    def __enter__(self):
        return self
    def __exit__(self,*args):
        return False
    def read(self,n=-1):
        return self.raw.read(n)


def _stub(monkeypatch,output):
    requests=[]
    def urlopen(req,timeout):
        requests.append(json.loads(req.data))
        return FakeResponse(json.dumps({
            "message":{"role":"assistant","content":output},
        }).encode())
    monkeypatch.setattr("persona_agent.model_gateway.urlopen",urlopen)
    return requests


def test_single_call_structured_response_preserves_spoken_text(monkeypatch):
    reply = {
        "reply":"That sounds worth exploring.",
        "intent":{
            "kind":"OBJECTIVE",
            "evidence_quote":"Maybe I should run regularly.",
            "confidence":.88,
            "reference":"NONE",
        },
    }
    calls=_stub(monkeypatch,json.dumps(reply))
    result=OllamaChatGateway(model="small").generate_interpreted([
        ModelMessage("system","Persona identity is persistent."),
        ModelMessage("user","Maybe I should run regularly."),
    ])
    assert len(calls)==1
    assert calls[0]["format"]=="json"
    assert calls[0]["stream"] is False
    assert len(calls[0]["messages"])==3
    assert result.text=="That sounds worth exploring."
    assert result.intent_candidate.kind=="OBJECTIVE"
    assert result.intent_candidate.evidence_quote=="Maybe I should run regularly."
    assert "confidence" not in result.text


def test_malformed_optional_model_intent_does_not_cancel_valid_reply(monkeypatch):
    calls=_stub(monkeypatch,json.dumps({
        "reply":"Ordinary reply.",
        "intent":{
            "kind":"AUTHORIZE_TOOL",
            "evidence_quote":"fake",
            "confidence":1.0,
            "reference":"PRIOR_OBJECTIVE",
        },
    }))
    response=OllamaChatGateway(model="tiny").generate_interpreted([
        ModelMessage("user","Maybe we should revise that.")
    ])
    assert len(calls)==1
    assert response.text=="Ordinary reply."
    assert response.intent_candidate is None


def test_plain_text_fallback_does_not_require_second_request(monkeypatch):
    calls=_stub(monkeypatch,"We could discuss an easier routine.")
    response=OllamaChatGateway(model="tiny").generate_interpreted([
        ModelMessage("user","I wish exercise were easier.")
    ])
    assert len(calls)==1
    assert response.text=="We could discuss an easier routine."
    assert response.intent_candidate is None


@pytest.mark.parametrize("raw,reply", [
    ('"A short natural answer."',"A short natural answer."),
    ('{"response":"Another natural answer.","intent":null}',"Another natural answer."),
])
def test_single_pass_small_model_reply_aliases(monkeypatch,raw,reply):
    calls=_stub(monkeypatch,raw)
    result=OllamaChatGateway(model="tiny").generate_interpreted([
        ModelMessage("user","That sounds manageable."),
    ])
    assert len(calls)==1
    assert result.text==reply
    assert result.intent_candidate is None


@pytest.mark.parametrize("raw",[
    "{}",
    "[]",
    '{"reply":"","intent":null}',
    "{ malformed json",
])
def test_invalid_response_fails_without_forging_a_reply(monkeypatch,raw):
    calls=_stub(monkeypatch,raw)
    with pytest.raises(ModelUnavailable):
        OllamaChatGateway(model="tiny").generate_interpreted([
            ModelMessage("user","Maybe I should jog.")
        ])
    assert len(calls)==1
