"""No-network typed Ollama adapter contract tests."""
import json
from io import BytesIO
from urllib.error import URLError

import pytest

from persona_agent.conversation_contracts import ModelMessage,ModelUnavailable
from persona_agent.model_gateway import OllamaChatGateway


class FakeHTTPResponse:
    def __init__(self,data): self.data=BytesIO(data)
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def read(self,n=-1): return self.data.read(n)


def test_ollama_gateway_sends_chat_json_without_streaming(monkeypatch):
    seen={}
    def stub(req,timeout):
        seen["url"]=req.full_url
        seen["body"]=json.loads(req.data)
        seen["timeout"]=timeout
        return FakeHTTPResponse(b'{"message":{"role":"assistant","content":"Answer"}}')
    monkeypatch.setattr("persona_agent.model_gateway.urlopen",stub)
    result=OllamaChatGateway(model="lexi",timeout_seconds=3.5).generate([
        ModelMessage(role="system",content="System"),
        ModelMessage(role="user",content="Hi"),
    ])
    assert (result.model_id,result.text)==("lexi","Answer")
    assert seen=={
        "url":"http://127.0.0.1:11434/api/chat","timeout":3.5,
        "body":{"model":"lexi","stream":False,"messages":[
            {"role":"system","content":"System"},
            {"role":"user","content":"Hi"}]},
    }


def test_gateway_unavailable_does_not_forge_output(monkeypatch):
    def stub(req,timeout): raise URLError("unavailable")
    monkeypatch.setattr("persona_agent.model_gateway.urlopen",stub)
    with pytest.raises(ModelUnavailable):
        OllamaChatGateway(model="lexi").generate([
            ModelMessage(role="user",content="Hello")])


@pytest.mark.parametrize("payload",[
    b'{"message":{"content":""}}',
    b'{"message":{"content":12}}',
    b'{"other":"missing"}',
    b"not json",
])
def test_gateway_rejects_malformed_model_reply(monkeypatch,payload):
    monkeypatch.setattr(
        "persona_agent.model_gateway.urlopen",
        lambda req,timeout:FakeHTTPResponse(payload))
    with pytest.raises(ModelUnavailable):
        OllamaChatGateway(model="lexi").generate([
            ModelMessage(role="user",content="Hello")])



@pytest.mark.parametrize("thinking,expected", [
    (None,None),
    (True,True),
    (False,False),
])
def test_ollama_thinking_flag_applies_to_plain_and_structured_requests(
    monkeypatch,thinking,expected,
):
    """Only an explicit bool adds think; structured calls use the same setting."""
    captured=[]
    def stub(req,timeout):
        payload=json.loads(req.data)
        captured.append(payload)
        answer=(
            '{"reply":"Hello","intent":null}'
            if payload.get("format")=="json" else "Hello"
        )
        return FakeHTTPResponse(json.dumps({
            "message":{"role":"assistant","content":answer,"thinking":"private"}
        }).encode())
    monkeypatch.setattr("persona_agent.model_gateway.urlopen",stub)
    gateway=OllamaChatGateway(model="qwen3.5:9b",thinking=thinking)
    messages=[ModelMessage(role="user",content="Hello")]
    assert gateway.generate(messages).text=="Hello"
    assert gateway.generate_interpreted(messages).text=="Hello"
    assert len(captured)==2
    assert "format" not in captured[0] and captured[1]["format"]=="json"
    for request in captured:
        if expected is None:
            assert "think" not in request
        else:
            assert request["think"] is expected


@pytest.mark.parametrize("value",["false",0,1,{},[]])
def test_ollama_thinking_rejects_non_boolean_values(value):
    with pytest.raises(TypeError,match="thinking must be bool or None"):
        OllamaChatGateway(model="lexi",thinking=value)
