"""Tests for the outbound pacing policy.

This is the code that keeps a client from being banned, so it is worth testing
directly rather than inferring it from transport behaviour. The clock and sleep
are injected, so a half-hour ban back-off is asserted in microseconds.
"""

from anidb_client.ratelimit import RateLimiter


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def make(clock=None):
    clock = clock or FakeClock()
    return RateLimiter(monotonic=clock.monotonic, sleep=clock.sleep), clock


class TestBurstThenSteadyRate:
    def test_the_first_command_is_not_delayed(self):
        """A freshly started client should not pause before its first request."""
        limiter, clock = make()
        limiter.wait()
        assert clock.slept == []

    def test_the_opening_burst_uses_the_shorter_delay(self):
        limiter, clock = make()
        for _ in range(RateLimiter.FREE_BURST):
            limiter.wait()
            limiter.record_send()
        # The first send is free; the rest of the burst pays the short delay.
        assert clock.slept == [RateLimiter.BURST_DELAY] * (RateLimiter.FREE_BURST - 1)

    def test_after_the_burst_the_delay_lengthens(self):
        """Past the burst allowance AniDB expects roughly one command per 4s."""
        limiter, clock = make()
        for _ in range(RateLimiter.FREE_BURST + 1):
            limiter.wait()
            limiter.record_send()
        assert clock.slept[-1] == RateLimiter.STEADY_DELAY

    def test_time_already_elapsed_counts_towards_the_delay(self):
        """An application slow in its own right must not be paced twice.

        The wait is until N seconds *since the last packet*, not N seconds from
        now, so a caller that spent 1.5s hashing a file waits only the remainder.
        """
        limiter, clock = make()
        limiter.wait()
        limiter.record_send()
        clock.advance(1.5)

        limiter.wait()
        assert clock.slept == [RateLimiter.BURST_DELAY - 1.5]

    def test_no_delay_when_more_than_enough_time_has_passed(self):
        limiter, clock = make()
        limiter.wait()
        limiter.record_send()
        clock.advance(RateLimiter.STEADY_DELAY + 1)

        limiter.wait()
        assert clock.slept == []

    def test_a_long_idle_period_restores_the_burst_allowance(self):
        """The server's flood counter decays too, so ours resets to match."""
        limiter, clock = make()
        for _ in range(RateLimiter.FREE_BURST + 2):
            limiter.wait()
            limiter.record_send()

        clock.advance(RateLimiter.IDLE_RESET + 1)
        clock.slept.clear()

        limiter.wait()
        limiter.record_send()
        limiter.wait()
        assert clock.slept == [RateLimiter.BURST_DELAY]


class TestBanBackoff:
    def test_a_new_limiter_is_not_banned(self):
        limiter, _ = make()
        assert not limiter.is_banned

    def test_the_first_ban_waits_the_base_delay(self):
        limiter, clock = make()
        limiter.register_ban()
        limiter.wait()
        assert clock.slept[0] == RateLimiter.BAN_BASE_DELAY

    def test_consecutive_bans_double_the_wait(self):
        """Exponential, so a server that stays unhappy is backed away from."""
        limiter, _ = make()
        assert [limiter.register_ban() for _ in range(4)] == [1, 2, 4, 8]

    def test_the_back_off_reflects_the_current_multiplier(self):
        limiter, clock = make()
        limiter.register_ban()
        limiter.register_ban()
        limiter.wait()
        assert clock.slept[0] == RateLimiter.BAN_BASE_DELAY * 2

    def test_a_successful_auth_clears_the_ban(self):
        """clear_ban is called from the auth handler: the back-off has served."""
        limiter, clock = make()
        limiter.register_ban()
        limiter.clear_ban()

        assert not limiter.is_banned
        limiter.wait()
        assert clock.slept == []

    def test_clearing_then_banning_again_starts_from_the_base_delay(self):
        limiter, _ = make()
        limiter.register_ban()
        limiter.register_ban()
        limiter.clear_ban()
        assert limiter.register_ban() == 1


class TestSendAccounting:
    def test_seconds_since_last_send_tracks_the_clock(self):
        limiter, clock = make()
        limiter.record_send()
        clock.advance(42)
        assert limiter.seconds_since_last_send() == 42

    def test_a_limiter_that_has_never_sent_reports_a_long_idle(self):
        """The transport uses this to decide on keepalives; it must not read as 0."""
        limiter, _ = make()
        assert limiter.seconds_since_last_send() > RateLimiter.IDLE_RESET
