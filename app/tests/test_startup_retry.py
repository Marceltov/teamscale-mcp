import pytest

from server import build_server_with_retry


class FakeClock:
    """Monotonic clock advanced by the fake sleep, so the deadline is exercised
    without real waiting."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_returns_first_success_after_transient_failures():
    clock = FakeClock()
    calls = []

    def build():
        calls.append(clock.now)
        if len(calls) < 3:
            raise RuntimeError("connection refused")
        return "server"

    result = build_server_with_retry(
        build, timeout=60, interval=5, sleep=clock.sleep, clock=clock
    )

    assert result == "server"
    assert calls == [0.0, 5.0, 10.0]


def test_reraises_last_error_once_deadline_passed():
    clock = FakeClock()
    calls = []

    def build():
        calls.append(clock.now)
        raise RuntimeError(f"still down at {clock.now:g}")

    with pytest.raises(RuntimeError, match="still down at 10"):
        build_server_with_retry(
            build, timeout=10, interval=5, sleep=clock.sleep, clock=clock
        )

    # Attempts at 0, 5 and 10 seconds; the one at the deadline is the last.
    assert calls == [0.0, 5.0, 10.0]


def test_env_configures_timeout_and_interval(monkeypatch):
    monkeypatch.setenv("TEAMSCALE_STARTUP_TIMEOUT", "3")
    monkeypatch.setenv("TEAMSCALE_STARTUP_RETRY_INTERVAL", "1.5")
    clock = FakeClock()
    calls = []

    def build():
        calls.append(clock.now)
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        build_server_with_retry(build, sleep=clock.sleep, clock=clock)

    assert calls == [0.0, 1.5, 3.0]
