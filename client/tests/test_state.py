from __future__ import annotations

from client.state import ClientState


def test_client_state_has_only_listening_and_active() -> None:
    assert {state.value for state in ClientState} == {"listening", "active"}
