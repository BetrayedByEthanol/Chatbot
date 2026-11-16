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
    "I'll run these commands for you.",
    "I’ll execute the shell command.",
    "We'll configure your server for you.",
    "We’ll install software on your server.",
])
def test_unprompted_or_conditional_execution_offers_are_blocked(response):
    result=enforce_capability_claims("How do I configure Nginx?", response)
    assert result.reason=="unsupported_offer:remote"

@pytest.mark.parametrize("response", [
    "I'll delete your data.",
    "I’ll erase your data.",
    "We'll remove your files.",
    "We’ll delete your data.",
])
def test_contracted_data_deletion_promises_are_blocked(response):
    result=enforce_capability_claims("What happens to my account?", response)
    assert result.reason=="unsupported_offer:data_deletion"
    assert "haven't deleted anything" in result.text


@pytest.mark.parametrize("answer", [
    'Run these commands yourself. Do not assume that "I can run these commands" is a capability this host has.',
    "Run these commands yourself. Don't assume that 'I can run these commands' is supported.",
    "Use your own terminal; `I'll execute this shell command` is an example of an unsupported promise.",
    "Do not infer that “We’ll delete your data” is an available operation.",
    "Don't assume I can run these commands for you.",
    "This does not mean that I can configure your server.",
])
def test_quoted_or_negated_examples_are_not_execution_offers(answer):
    result=enforce_capability_claims("How do I configure Nginx?",answer)
    assert result.reason is None
    assert result.text==answer


def test_real_offer_after_quoted_or_negated_example_is_still_blocked():
    answer=(
        'Do not assume "I can run these commands" is supported. '
        "Nevertheless, I'll execute the shell command for you."
    )
    result=enforce_capability_claims("How do I configure Nginx?",answer)
    assert result.reason=="unsupported_offer:remote"


@pytest.mark.parametrize(("input_text", "answer"), [
    ('Can you remove my data from this sentence: "Here is my data"?', "Here is."),
    ("Could you delete your data from this phrase: your data are protected?",
     "are protected?"),
    ("Can you remove my account from this string?", "Done editing the text."),
])
def test_text_edits_are_not_host_data_deletion(input_text,answer):
    result=enforce_capability_claims(input_text,answer)
    assert result.reason is None
    assert result.text==answer


def test_textual_removal_offer_preserved_but_storage_deletion_still_blocked():
    editing=enforce_capability_claims(
        "Can you edit this sentence?",
        "I can remove your data from this sentence.",
    )
    assert editing.reason is None
    assert editing.text=="I can remove your data from this sentence."
    deletion=enforce_capability_claims(
        "Can you delete my data from your servers?",
        "I can do that.",
    )
    assert deletion.reason=="unsupported_request:data_deletion"


def test_vague_execution_uses_immediately_previous_user_topic_only():
    # A stale Nginx subject must not shadow the supported word-count task.
    history=("How do I configure Nginx?", "Count words in: alpha beta")
    answer="There are two words."
    result=enforce_capability_claims(
        "Can you run this for me?", answer, recent_user_texts=history,
    )
    assert result.reason is None
    assert result.text==answer

    # Reverse order: the current server topic should still be enforced.
    switched=("Count words in: alpha beta", "How do I configure Nginx?")
    result=enforce_capability_claims(
        "Can you run this for me?", answer, recent_user_texts=switched,
    )
    assert result.reason=="unsupported_request:remote"


@pytest.mark.parametrize(("input_text", "answer"), [
    ("Can you run a word count on this code?", "There are two words."),
    ("Could you run text statistics on this script?", "I counted three words."),
    ("Can you run a character count on my Nginx configuration?", "28 characters."),
])
def test_read_only_analysis_of_executable_content_is_not_remote_execution(
    input_text, answer,
):
    result=enforce_capability_claims(input_text,answer)
    assert result.reason is None
    assert result.text==answer


@pytest.mark.parametrize("input_text", [
    "Can you run this code?",
    "Can you execute a script on the server?",
    "Can you run these commands for me?",
])
def test_explicit_executable_targets_remain_blocked(input_text):
    result=enforce_capability_claims(input_text,"Sure, I can do that.")
    assert result.reason=="unsupported_request:remote"


def test_word_count_offer_on_code_is_not_an_execution_promise():
    answer="I'll run a word count on this code and tell you the result."
    result=enforce_capability_claims("How many words are in this code?",answer)
    assert result.reason is None
    assert result.text==answer


def test_unavailable_data_deletion_is_not_promised():
    direct=enforce_capability_claims("Could you delete my data?", "Sure, done.")
    assert direct.reason=="unsupported_request:data_deletion"
    assert "haven't deleted anything" in direct.text
    hypothetical=enforce_capability_claims(
        "What if I asked you to delete my data?",
        "If you ask me, it will be done securely.",
    )
    assert hypothetical.reason=="unsupported_hypothetical:data_deletion"
    assert "haven't deleted anything" in hypothetical.text
    offered=enforce_capability_claims(
        "What happens to the account?", "I can delete your data now.",
    )
    assert offered.reason=="unsupported_offer:data_deletion"

@pytest.mark.parametrize(("user","response","history"), [
    ("How do I configure Nginx?",
     "I can't execute shell commands here. Run `openssl req -x509` on your machine.", ()),
    ("How do I configure Nginx?",
     "I can help you run these commands, but I can't run them myself.", ()),
    ("Could you explain the data deletion process?",
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


def test_topic_switch_to_read_only_work_does_not_inherit_remote_refusal(dsn):
    class ContextAwareModel(NonCompliantModel):
        def generate(self,messages):
            if messages[-1].content=="Can you run this for me?":
                self.calls.append(messages[-1].content)
                return ModelReply(model_id="safe-model", text="There are two words.")
            return super().generate(messages)

    model=ContextAwareModel()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),model)
    host.begin_session("topic-shift")
    host.send("How do I configure Nginx?",idempotency_key="nginx")
    host.send("Count words in: alpha beta",idempotency_key="words")
    answer=host.send("Can you run this for me?",idempotency_key="run")
    assert answer.text=="There are two words."
    host.restart_host()
    replay=host.send("Can you run this for me?",idempotency_key="run")
    assert replay.replayed
    assert replay.response_id==answer.response_id
    identity=host.active_identity
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE user_id=%s AND instance_id=%s
                 AND event_type='conversation.response.capability_claim_blocked.v1'""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==1  # unsolicited "I can configure your server" earlier
        persisted=conn.execute(
            "SELECT response_text FROM conversation_responses WHERE response_id=%s",
            (answer.response_id,),
        ).fetchone()[0]
        assert persisted==answer.text


def test_host_preserves_word_count_of_code_and_does_not_audit_false_block(dsn):
    class ReadonlyModel(NonCompliantModel):
        def generate(self, messages):
            current=messages[-1].content
            if current=="Can you run a word count on this code?":
                self.calls.append(current)
                return ModelReply(model_id="readonly-model",text="There are two words.")
            return super().generate(messages)

    host=BehavioralAcceptanceHarness(
        dsn,seed_local_demo(dsn),ReadonlyModel(),
    )
    host.begin_session("word-count-on-code")
    answer=host.send(
        "Can you run a word count on this code?",idempotency_key="word-count",
    )
    assert answer.text=="There are two words."
    identity=host.active_identity
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            "SELECT response_text FROM conversation_responses WHERE response_id=%s",
            (answer.response_id,),
        ).fetchone()
        assert row==("There are two words.",)
        blocked=conn.execute(
            """SELECT count(*) FROM events
               WHERE user_id=%s AND instance_id=%s
                 AND event_type='conversation.response.capability_claim_blocked.v1'""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]
        assert blocked==0


def test_host_keeps_quoted_guidance_and_text_edit_without_false_block(dsn):
    quoted=(
        'Run these commands yourself. Do not assume that '
        '"I can run these commands" is a capability this host has.'
    )
    class SafeModel(NonCompliantModel):
        def generate(self,messages):
            current=messages[-1].content
            self.calls.append(current)
            if current=="Explain how to configure Nginx safely.":
                return ModelReply(model_id="safe-model",text=quoted)
            if current.startswith("Can you remove my data from this sentence:"):
                return ModelReply(model_id="safe-model",text="Here is.")
            return ModelReply(model_id="safe-model",text="Instructions only.")

    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),SafeModel())
    host.begin_session("safe-guidance-and-editing")
    guidance=host.send("Explain how to configure Nginx safely.",idempotency_key="guide")
    assert guidance.text==quoted
    edit=host.send(
        'Can you remove my data from this sentence: "Here is my data"?',
        idempotency_key="edit",
    )
    assert edit.text=="Here is."
    host.restart_host()
    replay=host.send(
        'Can you remove my data from this sentence: "Here is my data"?',
        idempotency_key="edit",
    )
    assert replay.replayed and replay.response_id==edit.response_id
    identity=host.active_identity
    with psycopg.connect(dsn,autocommit=True) as conn:
        stored=conn.execute(
            "SELECT response_text FROM conversation_responses WHERE response_id=%s",
            (guidance.response_id,),
        ).fetchone()[0]
        assert stored==quoted
        stored=conn.execute(
            "SELECT response_text FROM conversation_responses WHERE response_id=%s",
            (edit.response_id,),
        ).fetchone()[0]
        assert stored=="Here is."
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE user_id=%s AND instance_id=%s
                 AND event_type='conversation.response.capability_claim_blocked.v1'""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0
