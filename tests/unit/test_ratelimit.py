"""Tests for the outbound pacing policy.

This is the code that keeps a client from being banned, so it is worth testing
directly rather than inferring it from transport behaviour. The clock, the sleep
and the jitter roll are all injected, so a half-hour back-off window is asserted
in microseconds and exactly.
"""

import threading

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


def make(clock=None, random=None):
    """A limiter on a controlled clock, and by default an unjittered back-off.

    `random=lambda: 1.0` means "the top of the jitter range", which makes the
    back-off exactly the computed window and lets these tests assert on it. The
    jitter itself has its own tests below.
    """
    clock = clock or FakeClock()
    return RateLimiter(monotonic=clock.monotonic, sleep=clock.sleep, random=random or (lambda: 1.0)), clock


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

    def test_the_first_ban_closes_the_window_for_about_the_base_delay(self):
        """A window on the clock, not a sleep.

        Nothing waits this out. The sender asks whether the window is still open
        and declines to send if it is, which is what keeps the listener reading
        the socket while the back-off runs.
        """
        limiter, clock = make()
        limiter.register_ban()

        assert clock.slept == [], "a ban must not be slept"
        assert 0 < limiter.ban_remaining() <= RateLimiter.BAN_BASE_DELAY

    def test_consecutive_bans_double_the_wait(self):
        """Exponential, so a server that stays unhappy is backed away from."""
        limiter, _ = make()
        assert [limiter.register_ban() for _ in range(4)] == [1, 2, 4, 8]

    def test_the_window_reflects_the_current_multiplier(self):
        limiter, _ = make(random=lambda: 1.0)
        limiter.register_ban()
        limiter.register_ban()

        assert limiter.ban_remaining() == RateLimiter.BAN_BASE_DELAY * 2

    def test_the_window_closes_once_it_has_elapsed(self):
        limiter, clock = make(random=lambda: 1.0)
        limiter.register_ban()

        clock.advance(RateLimiter.BAN_BASE_DELAY)

        assert limiter.ban_remaining() == 0

    def test_an_elapsed_window_does_not_clear_the_ban(self):
        """Only a successful authentication does.

        Otherwise a client that comes back, is refused again and re-bans starts
        from the base delay every time, and never actually backs further off.
        """
        limiter, clock = make(random=lambda: 1.0)
        limiter.register_ban()
        clock.advance(RateLimiter.BAN_BASE_DELAY)

        assert limiter.is_banned
        assert limiter.register_ban() == 2

    def test_the_window_is_jittered(self):
        """Ten cron processes banned together must not come back together.

        The incident had exactly that: separate short-lived processes on one host,
        each authenticating for itself. Without jitter they return in step, which
        against a service that counts requests per client is a burst.
        """
        rolls = iter([0.0, 0.25, 1.0])
        windows = []
        for roll in rolls:
            limiter, _ = make(random=lambda roll=roll: roll)
            limiter.register_ban()
            windows.append(limiter.ban_remaining())

        assert len(set(windows)) == 3, f"the back-off did not vary: {windows}"

    def test_jitter_never_shortens_the_back_off_to_nothing(self):
        """The lowest roll must still be a real back-off.

        Full jitter -- uniform from zero -- is the usual advice and is wrong here:
        a client that rolls low comes back almost immediately, which is the one
        thing not to do to a service that bans on request frequency.
        """
        limiter, _ = make(random=lambda: 0.0)
        limiter.register_ban()

        assert limiter.ban_remaining() >= RateLimiter.BAN_BASE_DELAY * RateLimiter.BAN_JITTER_FLOOR

    def test_a_successful_auth_clears_the_ban(self):
        """clear_ban is called from the auth handler: the back-off has served."""
        limiter, clock = make()
        limiter.register_ban()
        limiter.clear_ban()

        assert not limiter.is_banned
        assert limiter.ban_remaining() == 0
        limiter.wait()
        assert clock.slept == []

    def test_clearing_then_banning_again_starts_from_the_base_delay(self):
        limiter, _ = make()
        limiter.register_ban()
        limiter.register_ban()
        limiter.clear_ban()
        assert limiter.register_ban() == 1

    def test_the_doubling_stops_at_a_ceiling(self):
        """Unbounded, this walks off into delays measured in days.

        clear_ban() runs on every successful authentication, so the ordinary
        banned-then-readmitted cycle rarely leaves 1. It compounds when
        authentication itself keeps failing -- which is the case where a client
        that has effectively stopped still reports only that it is waiting.
        """
        limiter, _ = make()
        assert [limiter.register_ban() for _ in range(6)] == [1, 2, 4, 8, 8, 8]

    def test_the_longest_back_off_is_bounded(self):
        limiter, _ = make(random=lambda: 1.0)
        for _ in range(20):
            limiter.register_ban()

        assert limiter.ban_remaining() == RateLimiter.BAN_BASE_DELAY * RateLimiter.MAX_BAN_MULTIPLIER


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


class TestThreadSafety:
    """The limiter is touched by both transport threads: the sender calls wait()
    and record_send(), the listener calls register_ban() and clear_ban() as replies
    arrive. Every counter is read-modify-write, so each is locked.
    """

    def test_the_lock_is_not_held_across_the_sleep(self):
        """The pacing delay is computed under the lock and slept for outside it.

        Seconds rather than half-hours now that a ban is a window rather than a
        sleep, but the property still has to hold: a sender paused between
        commands must not be able to stop the listener reporting a ban.

        The injected sleep stands in for that pause, and a second thread stands in
        for the listener arriving during it.
        """
        reported = threading.Event()

        def sleeping(_seconds):
            worker = threading.Thread(target=lambda: (limiter.register_ban(), reported.set()))
            worker.start()
            worker.join(timeout=2)

        limiter = RateLimiter(monotonic=lambda: 0.0, sleep=sleeping)
        limiter.register_ban()

        limiter.wait()

        assert reported.is_set(), "the listener could not touch the limiter while the sender was backing off"
