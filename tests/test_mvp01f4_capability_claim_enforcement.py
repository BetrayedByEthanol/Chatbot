"""MVP-01F.4: enforce capability truth at the final PostgreSQL response boundary."""
import pytest

from persona_agent.capability_claim_enforcement import enforce_capability_claims

def test_clear_remote_execution_request_cannot_be_accepted_by_model():
    result=enforce_capability_claims(
        "Can you run this for me?", "Sure, I will take care of it.",
        recent_user_texts=("How do I create a self-signed TLS certificate for Nginx?",),
    )
    assert result.reason=="unsupported_request:remote"
    assert "can't run commands" in result.text
    assert "haven't executed anything" in result.text

@pytest.mark.parametrize("response", [
    "Would you like me to run these commands for you, or would you prefer to do it yourself?",
    "I can configure your server for you.",
    "I will execute the shell command.",
    "Let me run those commands for you.",
])
def test_unprompted_or_conditional_execution_offers_are_blocked(response):
    result=enforce_capability_claims("How do I configure Nginx?", response)
    assert result.reason=="unsupported_offer:remote"

def test_unavailable_data_deletion_is_not_promised():
    direct=enforce_capability_claims("Could you delete my data?", "Sure, done.")
    assert direct.reason=="unsupported_request:data_deletion"
    assert "haven't deleted anything" in direct.text
    offered=enforce_capability_claims(
        "What if I asked you to delete my data?", "I can delete your data now.",
    )
    assert offered.reason=="unsupported_offer:data_deletion"

@pytest.mark.parametrize(("user","response","history"), [
    ("How do I configure Nginx?",
     "I can't execute shell commands here. Run `openssl req -x509` on your machine.", ()),
    ("How do I configure Nginx?",
     "I can help you run these commands, but I can't run them myself.", ()),
    ("What if I asked you to delete my data?",
     "I would explain the deletion options, not delete anything.", ()),
    ("Count words in: alpha beta", "There are two words.", ()),
    ("Can you run this for me?", "I can count the words.",
     ("Count words in: alpha beta",)),
    ('"Can you run these commands for me?"', "That is a quoted question.", ()),
])
def test_safe_guidance_and_existing_read_only_work_are_preserved(user,response,history):
    result=enforce_capability_claims(user,response,recent_user_texts=history)
    assert result.reason is None
    assert result.text==response

# The integration case uses actual Conversation ingress, model output,
# selected-persona fencing, PostgreSQL response events and replay.
psycopg=pytest.importorskip("psycopg")
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.acceptance_host import seed_local_demo
from persona_agent.conversation_contracts import ModelReply

class NonCompliantModel:
    def __init__(self):
        self.calls=[]
    def generate(self,messages):
        current=messages[-1].content
        self.calls.append(current)
        if current=="Can you run this for me?":
            return ModelReply(model_id="unreliable.llm",text=(
                "Use openssl for your certificate. "
                "Would you like me to run these commands for you?"
            ))
        if current=="Can you delete my data?":
            return ModelReply(model_id="unreliable.llm",text="Sure, I deleted it.")
        if current=="How do I configure Nginx?":
            return ModelReply(model_id="unreliable.llm",text=(
                "Here's a sample configuration. I can configure your server for you."
            ))
        return ModelReply(model_id="unreliable.llm",text=(
            "I can show you the commands to run yourself."
        ))

def test_real_host_blocks_unavailable_claims_and_replays_committed_response(dsn):
    model=NonCompliantModel()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),model)
    host.begin_session("capability-claims")
    host.send(
        "How do I create a self-signed TLS certificate for Nginx?",
        idempotency_key="setup",
    )
    direct=host.send("Can you run this for me?",idempotency_key="run")
    assert "can't run commands" in direct.text
    assert "Would you like me to run" not in direct.text
    assert direct.model_id=="unreliable.llm"
    host.restart_host()
    replay=host.send("Can you run this for me?",idempotency_key="run")
    assert replay.replayed
    assert replay.response_id==direct.response_id
    assert replay.text==direct.text
    unsolicited=host.send(
        "How do I configure Nginx?",idempotency_key="unsolicited",
    )
    assert "can't run commands" in unsolicited.text
    deletion=host.send("Can you delete my data?",idempotency_key="delete")
    assert "haven't deleted anything" in deletion.text
    safe=host.send("Please only explain the steps.",idempotency_key="safe")
    assert safe.text=="I can show you the commands to run yourself."

    identity=host.active_identity
    with psycopg.connect(dsn,autocommit=True) as conn:
        for table in ("operations","goal_records"):
            assert conn.execute(
                f"SELECT count(*) FROM {table} WHERE user_id=%s AND instance_id=%s",
                (identity.user_id,identity.instance_id),
            ).fetchone()[0]==0
        events=conn.execute(
            """SELECT payload FROM events
               WHERE user_id=%s AND instance_id=%s
                 AND event_type='conversation.response.capability_claim_blocked.v1'
               ORDER BY stream_sequence""",
            (identity.user_id,identity.instance_id),
        ).fetchall()
        assert len(events)==3
        assert [row[0]["reason"] for row in events]==[
            "unsupported_request:remote",
            "unsupported_offer:remote",
            "unsupported_request:data_deletion",
        ]
    assert model.calls.count("Can you run this for me?")==1
