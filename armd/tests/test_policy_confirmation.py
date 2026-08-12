from __future__ import annotations

from pathlib import Path

from armd.policy_confirmation import PolicyConfirmationManager, PolicyConfirmationSocket


def test_policy_confirmation_is_request_bound_one_shot_and_expires() -> None:
    now = 10_000_000_000

    def clock_ns() -> int:
        return now

    manager = PolicyConfirmationManager(ttl_s=1.0, clock_ns=clock_ns)
    confirmation = manager.issue(request_id="request-1", session_id="session-1")
    assert confirmation.expires_monotonic_ns == now + 1_000_000_000
    assert not manager.consume(
        confirmation.confirmation_id,
        request_id="wrong-request",
        session_id="session-1",
    )
    assert manager.consume(
        confirmation.confirmation_id,
        request_id="request-1",
        session_id="session-1",
    )
    assert not manager.consume(
        confirmation.confirmation_id,
        request_id="request-1",
        session_id="session-1",
    )

    confirmation = manager.issue(request_id="request-2", session_id="session-1")
    now += 1_000_000_001
    assert not manager.consume(
        confirmation.confirmation_id,
        request_id="request-2",
        session_id="session-1",
    )


def test_policy_confirmation_revoke_all_invalidates_pending_tokens() -> None:
    manager = PolicyConfirmationManager(ttl_s=1.0)
    confirmation = manager.issue(request_id="request-1", session_id="session-1")
    manager.revoke_all()
    assert not manager.consume(
        confirmation.confirmation_id,
        request_id="request-1",
        session_id="session-1",
    )


def test_local_confirmation_socket_rejects_process_without_controlling_tty(tmp_path: Path) -> None:
    issuer = PolicyConfirmationSocket(
        PolicyConfirmationManager(ttl_s=1.0),
        tmp_path / "confirm.sock",
    )
    assert not issuer._peer_has_tty(999_999_999)
