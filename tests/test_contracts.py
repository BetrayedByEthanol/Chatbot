from uuid import uuid4

import pytest
from pydantic import ValidationError

from persona_agent.contracts import RenameInstanceCommand, TrustedActor


def test_command_is_typed_frozen_and_normalizes_label():
    actor = TrustedActor(user_id=uuid4(), source_id="terminal")
    command = RenameInstanceCommand(
        instance_id=uuid4(), expected_generation=1,
        expected_version=0, new_label="  Friendly  ",
        idempotency_key="command-1",
    )
    assert actor.user_id
    assert command.new_label == "Friendly"
    with pytest.raises(ValidationError):
        command.new_label = "different"


@pytest.mark.parametrize("label", ["", "   ", "x" * 81])
def test_invalid_label(label):
    with pytest.raises(ValidationError):
        RenameInstanceCommand(
            instance_id=uuid4(), expected_generation=1,
            expected_version=0, new_label=label,
            idempotency_key="command-1",
        )


def test_extra_trust_fields_are_rejected():
    with pytest.raises(ValidationError):
        TrustedActor(user_id=uuid4(), source_id="terminal", trust_level="SYSTEM")
