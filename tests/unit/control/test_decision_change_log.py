import logging

from issue_orchestrator.control.decision_change_log import DecisionChangeLog


def _log() -> logging.Logger:
    return logging.getLogger("test.decision_change_log")


def test_logs_on_first_occurrence_and_returns_true(caplog) -> None:
    log = DecisionChangeLog(_log())
    with caplog.at_level(logging.INFO, logger="test.decision_change_log"):
        assert log.note(6887, "skip:paused", "issue=%d %s", 6887, "paused") is True
    assert "issue=6887 paused" in caplog.text


def test_same_fingerprint_is_silent_no_spam(caplog) -> None:
    log = DecisionChangeLog(_log())
    with caplog.at_level(logging.INFO, logger="test.decision_change_log"):
        log.note(6887, "skip:paused", "line-A")
        second = log.note(6887, "skip:paused", "line-B")
    assert second is False
    # Only the first line was emitted; the repeat is suppressed.
    assert "line-A" in caplog.text
    assert "line-B" not in caplog.text


def test_changed_fingerprint_re_logs(caplog) -> None:
    log = DecisionChangeLog(_log())
    with caplog.at_level(logging.INFO, logger="test.decision_change_log"):
        log.note(6887, "skip:paused", "was-paused")
        relog = log.note(6887, "launch:reserved_slot", "now-launching")
    assert relog is True
    assert "was-paused" in caplog.text and "now-launching" in caplog.text


def test_retain_forgets_absent_keys_so_reappearance_logs_fresh(caplog) -> None:
    log = DecisionChangeLog(_log())
    with caplog.at_level(logging.INFO, logger="test.decision_change_log"):
        log.note(6887, "skip:paused", "first")
        log.retain([])  # 6887 no longer queued -> forgotten
        again = log.note(6887, "skip:paused", "second")
    assert again is True  # same fingerprint, but state was pruned
    assert "second" in caplog.text


def test_retain_keeps_present_keys_still_deduped(caplog) -> None:
    log = DecisionChangeLog(_log())
    with caplog.at_level(logging.INFO, logger="test.decision_change_log"):
        log.note(6887, "skip:paused", "first")
        log.retain([6887])  # still queued -> state kept
        again = log.note(6887, "skip:paused", "second")
    assert again is False
    assert "second" not in caplog.text
