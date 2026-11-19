from datetime import datetime, timezone
from types import SimpleNamespace

from classmate.worker.main import (
    MAX_ATTEMPTS,
    has_active_lease,
    stale_lease_recovery_status,
)


def test_reminder_job_accepts_its_current_lease():
    token = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    job = SimpleNamespace(status="processing", locked_at=token)

    assert has_active_lease(job, token)


def test_reminder_job_rejects_a_stale_worker_lease():
    stale_token = datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc)
    current_token = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    job = SimpleNamespace(status="processing", locked_at=current_token)

    assert not has_active_lease(job, stale_token)


def test_reminder_job_rejects_non_processing_state_even_when_token_matches():
    token = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    job = SimpleNamespace(status="sent", locked_at=token)

    assert not has_active_lease(job, token)



def test_expired_lease_retries_below_max_attempts():
    assert stale_lease_recovery_status(MAX_ATTEMPTS - 1) == "retry"


def test_expired_lease_fails_at_max_attempts():
    assert stale_lease_recovery_status(MAX_ATTEMPTS) == "failed"
