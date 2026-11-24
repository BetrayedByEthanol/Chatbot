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

@pytest.mark.parametrize("failure,detail", [
    (TimeoutError("private endpoint"), "timeout"),
    (URLError(TimeoutError("private endpoint")), "timeout"),
    (URLError(ConnectionRefusedError("secret")), "connection refused"),
    (ConnectionResetError("secret"), "connection reset"),
    (URLError("private endpoint"), "transport failure"),
])
def test_transport_diagnostic_is_bounded_and_keeps_original_cause(monkeypatch,failure,detail):
    calls=[]
    def stub(req,timeout):
        calls.append(req)
        raise failure
    monkeypatch.setattr("persona_agent.model_gateway.urlopen",stub)
    with pytest.raises(ModelUnavailable) as exc:
        OllamaChatGateway(model="lexi").generate([ModelMessage("user","Private text")])
    assert str(exc.value)==f"model gateway request failed ({detail})"
    assert exc.value.__cause__ is failure
    assert len(calls)==1


def test_http_failure_reports_only_status_not_provider_body(monkeypatch):
    from urllib.error import HTTPError
    error=HTTPError("http://private.invalid",503,"secret message",None,BytesIO(b"secret body"))
    def stub(req,timeout):
        raise error
    monkeypatch.setattr("persona_agent.model_gateway.urlopen",stub)
    with pytest.raises(ModelUnavailable) as exc:
        OllamaChatGateway(model="lexi").generate([ModelMessage("user","Hello")])
    assert str(exc.value)=="model gateway request failed (HTTP 503)"
    assert exc.value.__cause__ is error


def test_incomplete_http_response_becomes_model_unavailable(monkeypatch):
    from http.client import IncompleteRead
    def stub(req,timeout):
        raise IncompleteRead(b"private response",20)
    monkeypatch.setattr("persona_agent.model_gateway.urlopen",stub)
    with pytest.raises(ModelUnavailable,match="HTTP protocol failure"):
        OllamaChatGateway(model="lexi").generate([ModelMessage("user","Hello")])
