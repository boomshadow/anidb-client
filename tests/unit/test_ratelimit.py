"""Tests for the outbound pacing policy.

This is the code that keeps a client from being banned, so it is worth testing
directly rather than inferring it from transport behaviour. The clock, the sleep
and the jitter roll are all injected, so a half-hour back-off window is asserted
in microseconds and exactly.
"""

import logging
import threading

import pytest

import anidb_client
from anidb_client.errors import BackOffKind, BanCause
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


def make(clock=None, random=None, **seeds):
    """A limiter on a controlled clock, and by default an unjittered back-off.

    `random=lambda: 1.0` means "the top of the jitter range", which makes the
    back-off exactly the computed window and lets these tests assert on it. The
    jitter itself has its own tests below.

    Any further keyword arguments are the resumable state, forwarded as given so
    that the tests for it read the way a caller's own code would.
    """
    clock = clock or FakeClock()
    limiter = RateLimiter(monotonic=clock.monotonic, sleep=clock.sleep, random=random or (lambda: 1.0), **seeds)
    return limiter, clock


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


class TestBanCause:
    """Which of the three refusals opened the window.

    The policy is the same for all three -- be quiet for a while -- but a caller
    told to wait cannot act on that without knowing whether AniDB refused it,
    ignored it, or was never asked. It is recorded here, beside the window it
    describes, so that a refusal raised far from where the ban was registered can
    still say which one it is.
    """

    def test_a_limiter_that_is_not_banned_has_no_cause(self):
        limiter, _clock = make()
        assert limiter.ban_cause is None

    def test_the_cause_is_whatever_registered_the_ban(self):
        limiter, _clock = make()
        limiter.register_ban(BanCause.SILENCE)
        assert limiter.ban_cause is BanCause.SILENCE

    def test_a_refusal_is_the_default(self):
        """The overwhelming majority of bans arrive as a response code."""
        limiter, _clock = make()
        limiter.register_ban()
        assert limiter.ban_cause is BanCause.REFUSED

    def test_a_later_ban_replaces_the_cause(self):
        """The window is one state, so it has one reason -- the current one."""
        limiter, _clock = make()
        limiter.register_ban(BanCause.LOCAL)
        limiter.register_ban(BanCause.REFUSED)
        assert limiter.ban_cause is BanCause.REFUSED

    def test_clearing_the_ban_clears_the_cause(self):
        limiter, _clock = make()
        limiter.register_ban(BanCause.SILENCE)
        limiter.clear_ban()
        assert limiter.ban_cause is None

    def test_an_elapsed_window_still_reports_its_cause(self):
        """Elapsing is not clearing: the multiplier stands until an auth succeeds.

        A client that comes back and is refused again backs off for longer, and
        the reason it was backing off in the first place is still the answer to
        why the multiplier is where it is.
        """
        limiter, clock = make()
        limiter.register_ban(BanCause.SILENCE)
        clock.advance(RateLimiter.BAN_BASE_DELAY + 1)

        assert limiter.ban_remaining() == 0
        assert limiter.ban_cause is BanCause.SILENCE


class TestBackOffKind:
    """A busy upstream and a banned client get different schedules.

    The response table already separates the two -- `602 SERVER BUSY`, `601 OUT OF
    SERVICE`, `604 TIMEOUT` and `600 INTERNAL SERVER ERROR` are dispositioned
    `BACK_OFF`, while `555 BANNED` and `504 CLIENT BANNED` are `BANNED` -- and this
    is the class that spends that distinction rather than discarding it.

    Backing off from a busy server is not the defect and is not removed here:
    pushing through a `602` is a good way to earn a real ban. What is wrong is a
    transient busy signal inheriting a schedule written for punishment. The
    reported incident is the shape to keep in mind -- seven metered calls in ninety
    minutes, a `602` that was the upstream's own load, and a back-off of nineteen
    minutes with the multiplier climbing.
    """

    def test_a_busy_upstream_backs_off_for_seconds_rather_than_half_an_hour(self):
        limiter, _clock = make()
        limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)

        assert limiter.ban_remaining() == RateLimiter.BUSY_BASE_DELAY

    def test_a_ban_still_backs_off_for_the_length_of_a_ban(self):
        """The schedule that was right stays exactly as it was."""
        limiter, _clock = make()
        limiter.register_ban(BanCause.REFUSED, BackOffKind.BANNED)

        assert limiter.ban_remaining() == RateLimiter.BAN_BASE_DELAY

    def test_a_ban_is_the_default_kind(self):
        """A caller with no verdict from AniDB to read waits as though banned.

        The safe direction: being wrong costs a client that is quiet too long,
        where the other default costs a client probing a service that banned it.
        """
        limiter, _clock = make()
        limiter.register_ban()

        assert limiter.back_off_kind is BackOffKind.BANNED
        assert limiter.ban_remaining() == RateLimiter.BAN_BASE_DELAY

    def test_a_busy_back_off_still_escalates(self):
        """Exponential back-off on a busy upstream is correct and stays.

        What changes is what it counts from, not that it counts.
        """
        limiter, _clock = make()

        windows = []
        for _ in range(3):
            limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)
            windows.append(limiter.ban_remaining())

        assert windows == [
            RateLimiter.BUSY_BASE_DELAY,
            RateLimiter.BUSY_BASE_DELAY * 2,
            RateLimiter.BUSY_BASE_DELAY * 4,
        ]

    def test_the_longest_a_busy_upstream_can_cost_is_bounded(self):
        """Minutes, against the ban schedule's hours."""
        limiter, _clock = make()
        for _ in range(20):
            limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)

        assert limiter.ban_remaining() == RateLimiter.BUSY_BASE_DELAY * RateLimiter.MAX_BAN_MULTIPLIER
        assert limiter.ban_remaining() < RateLimiter.BAN_BASE_DELAY

    def test_the_kind_is_readable_from_the_limiter(self):
        """The point of the field: the live state answers, not only the error."""
        limiter, _clock = make()
        limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)

        assert limiter.back_off_kind is BackOffKind.BUSY

    def test_a_limiter_that_is_not_banned_has_no_kind(self):
        limiter, _clock = make()
        assert limiter.back_off_kind is None

    def test_clearing_the_ban_clears_the_kind(self):
        limiter, _clock = make()
        limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)
        limiter.clear_ban()

        assert limiter.back_off_kind is None

    def test_an_elapsed_window_still_reports_its_kind(self):
        """Elapsing is not clearing, for the kind as for the cause."""
        limiter, clock = make()
        limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)
        clock.advance(RateLimiter.BUSY_BASE_DELAY + 1)

        assert limiter.ban_remaining() == 0
        assert limiter.back_off_kind is BackOffKind.BUSY

    def test_the_kind_and_the_cause_are_independent(self):
        """Two axes, deliberately not collapsed into one.

        The cause says how the back-off arose; the kind says which refusal it was.
        A `602` and a `555` are both `REFUSED`, which is exactly why the cause on
        its own could not tell the reported incident from a real ban.
        """
        limiter, _clock = make()
        limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)

        assert limiter.ban_cause is BanCause.REFUSED
        assert limiter.back_off_kind is BackOffKind.BUSY

    def test_a_busy_back_off_does_not_inherit_a_ban_s_escalation(self):
        """The defect, stated as a schedule.

        The multiplier counts consecutive refusals of one kind. A busy reply is
        not the next step of a ban's escalation, so it starts its own count --
        otherwise a client that had been banned once would serve a four-minute
        sentence for the upstream having a bad second.
        """
        limiter, _clock = make()
        for _ in range(3):
            limiter.register_ban(BanCause.REFUSED, BackOffKind.BANNED)
        assert limiter.ban_multiplier == 4

        limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)

        assert limiter.ban_multiplier == 1
        assert limiter.ban_remaining() == RateLimiter.BUSY_BASE_DELAY

    def test_a_ban_after_a_busy_run_starts_from_the_full_ban_delay(self):
        """And the same rule the other way, which is the one that must not be lax.

        A run of busy replies must not leave a real ban starting at a multiplier
        it did not earn -- but nor may it shorten one. The ban's own base delay is
        what the first ban is worth.
        """
        limiter, _clock = make()
        for _ in range(3):
            limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)

        limiter.register_ban(BanCause.REFUSED, BackOffKind.BANNED)

        assert limiter.ban_multiplier == 1
        assert limiter.ban_remaining() == RateLimiter.BAN_BASE_DELAY

    def test_a_busy_back_off_is_jittered_like_any_other(self):
        """The herd argument does not stop applying because the window is shorter."""
        limiter, _clock = make(random=lambda: 0.0)
        limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)

        assert limiter.ban_remaining() == RateLimiter.BUSY_BASE_DELAY * RateLimiter.BAN_JITTER_FLOOR

    def test_a_busy_back_off_is_not_slept(self):
        """A window on the clock, whichever schedule drew it."""
        limiter, clock = make()
        limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)

        assert clock.slept == []


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


class TestResumingStateFromAPreviousProcess:
    """A limiter may be built already knowing what the last process knew.

    Everything this class holds is per-process, and for a long-lived service that
    state is what stands between one address and an IP ban. A restart begins with a
    fresh allowance: the burst is full, the first command goes out with no delay,
    and any back-off that was standing is simply forgotten. One burst per deploy
    sits inside AniDB's documented flood allowance; a crash loop repeating it is the
    shape of the incident this library has already been bitten by.

    `init()` accepts a limiter (SPEC-006), so where that state lives is the
    application's decision rather than this library's.
    """

    def test_a_plain_limiter_resumes_nothing(self):
        """The default is exactly what it was."""
        limiter, _clock = make()

        assert not limiter.is_banned
        assert limiter.ban_remaining() == 0
        assert limiter.ban_cause is None

    def test_a_standing_ban_is_resumed(self):
        limiter, _clock = make(banned_for=900, ban_multiplier=1, ban_cause=BanCause.REFUSED)

        assert limiter.is_banned
        assert limiter.ban_remaining() == pytest.approx(900)
        assert limiter.ban_multiplier == 1
        assert limiter.ban_cause is BanCause.REFUSED

    def test_the_multiplier_carries_so_the_next_ban_is_longer(self):
        """The point of resuming it.

        A client that comes back after a restart and is refused again must back off
        for longer, rather than starting the doubling over at the base delay --
        which is what a fresh limiter would do, forever, for a client that keeps
        being refused across restarts.
        """
        limiter, _clock = make(ban_multiplier=4, ban_cause=BanCause.SILENCE)

        assert limiter.register_ban() == 8

    def test_the_window_is_measured_from_now_rather_than_from_a_stored_instant(self):
        """The trap this shape of API exists to avoid.

        `monotonic()` has an undefined zero point, so a deadline stored by one
        process means nothing in the next -- and it would mean nothing *silently*,
        reading as already elapsed or as hours away with no error either way.
        Seeding a duration is what makes the value portable: the same 900 seconds
        must come back as 900 seconds whatever this process's clock happens to read.
        """
        early_clock = FakeClock()
        early_clock.now = 5.0
        late_clock = FakeClock()
        late_clock.now = 9_000_000.0

        seeds = {"banned_for": 900, "ban_multiplier": 1, "ban_cause": BanCause.REFUSED}
        early, _ = make(clock=early_clock, **seeds)
        late, _ = make(clock=late_clock, **seeds)

        assert early.ban_remaining() == pytest.approx(late.ban_remaining())

    def test_a_resumed_window_elapses_like_any_other(self):
        limiter, clock = make(banned_for=100, ban_multiplier=2, ban_cause=BanCause.REFUSED)

        clock.advance(100)

        assert limiter.ban_remaining() == 0
        # Elapsing does not clear the ban, resumed or not.
        assert limiter.is_banned
        assert limiter.ban_multiplier == 2

    def test_a_multiplier_may_be_resumed_with_no_window_left(self):
        """A legitimate state: the window elapsed, and no authentication has
        succeeded to clear the multiplier it left behind."""
        limiter, _clock = make(ban_multiplier=3, ban_cause=BanCause.LOCAL)

        assert limiter.is_banned
        assert limiter.ban_remaining() == 0
        assert limiter.ban_multiplier == 3

    def test_the_last_send_is_resumed_as_an_age(self):
        """So the first command after a restart is paced rather than free."""
        limiter, _clock = make(seconds_since_last_send=1)

        assert limiter.seconds_since_last_send() == pytest.approx(1)
        # One second into a two-second burst interval, so one second is still owed.
        assert limiter.delay_for_next_send() == pytest.approx(1)

    def test_omitting_the_last_send_still_reads_as_never_sent(self):
        limiter, _clock = make()

        assert limiter.seconds_since_last_send() > RateLimiter.IDLE_RESET
        assert limiter.delay_for_next_send() <= 0

    def test_a_successful_auth_clears_resumed_state_like_any_other(self):
        limiter, _clock = make(banned_for=900, ban_multiplier=2, ban_cause=BanCause.REFUSED)

        limiter.clear_ban()

        assert not limiter.is_banned
        assert limiter.ban_remaining() == 0
        assert limiter.ban_cause is None

    def test_every_seedable_field_can_also_be_read(self):
        """The rule that keeps this honest.

        State that can be resumed must also be capturable, or a caller can
        rehydrate a limiter it has no way to persist again -- which is a worse trap
        than not being able to resume it at all.
        """
        limiter, _clock = make(
            banned_for=900,
            ban_multiplier=2,
            ban_cause=BanCause.SILENCE,
            back_off_kind=BackOffKind.BUSY,
            seconds_since_last_send=7,
        )

        assert limiter.ban_remaining() == pytest.approx(900)
        assert limiter.ban_multiplier == 2
        assert limiter.ban_cause is BanCause.SILENCE
        assert limiter.back_off_kind is BackOffKind.BUSY
        assert limiter.seconds_since_last_send() == pytest.approx(7)

    def test_the_kind_survives_the_round_trip(self):
        """Without this, a restart silently converts a busy back-off into a ban.

        The application stores the window and the multiplier and hands them back;
        if the kind is not part of that, the resumed limiter reports a ban to a
        status endpoint that was reporting a busy upstream a moment earlier -- the
        process disagreeing with itself across its own restart.
        """
        limiter, _clock = make(
            banned_for=20, ban_multiplier=1, ban_cause=BanCause.REFUSED, back_off_kind=BackOffKind.BUSY
        )

        assert limiter.back_off_kind is BackOffKind.BUSY

    def test_the_resumed_kind_chooses_the_next_schedule(self):
        """Which is what makes resuming it worth anything.

        A resumed busy back-off that escalated on the ban schedule would hand the
        incident back the moment the process restarted.
        """
        limiter, _clock = make(ban_multiplier=1, ban_cause=BanCause.REFUSED, back_off_kind=BackOffKind.BUSY)

        limiter.register_ban(BanCause.REFUSED, BackOffKind.BUSY)

        assert limiter.ban_multiplier == 2
        assert limiter.ban_remaining() == RateLimiter.BUSY_BASE_DELAY * 2

    def test_an_omitted_kind_resumes_as_a_ban(self):
        """A limiter seeded by an application that names no kind must still resume.

        Assuming a ban errs the safe way: the resumed window is whatever
        `banned_for` said either way, and only the next escalation differs -- so
        the mistake this default can make is waiting too long.
        """
        limiter, _clock = make(banned_for=900, ban_multiplier=1, ban_cause=BanCause.REFUSED)

        assert limiter.back_off_kind is BackOffKind.BANNED
        assert limiter.ban_remaining() == pytest.approx(900)

    def test_a_successful_auth_clears_a_resumed_kind_too(self):
        limiter, _clock = make(
            banned_for=20, ban_multiplier=1, ban_cause=BanCause.REFUSED, back_off_kind=BackOffKind.BUSY
        )

        limiter.clear_ban()

        assert limiter.back_off_kind is None


class TestIncoherentStateIsRefused:
    """Refused rather than repaired, because repairing it hides the bug upstream.

    The combination that matters is a back-off with no multiplier. `is_banned`
    reads the multiplier, so such a limiter holds a deadline while reporting itself
    unbanned -- and the transport, which asks `is_banned` before it asks anything
    else, sends straight through the ban it was told about. A ban that exists always
    has a multiplier of at least one, because `register_ban` sets that on the first
    one, so a zero here says the stored state is already wrong.
    """

    def test_a_window_without_a_multiplier_is_refused(self):
        with pytest.raises(ValueError, match="ban_multiplier"):
            RateLimiter(banned_for=900)

    def test_the_refusal_explains_what_would_have_gone_wrong(self):
        with pytest.raises(ValueError, match="reporting itself unbanned"):
            RateLimiter(banned_for=900)

    def test_a_cause_without_a_multiplier_is_refused(self):
        """`ban_cause` answers None while unbanned, so this one could not be read back."""
        with pytest.raises(ValueError, match="ban_multiplier"):
            RateLimiter(ban_cause=BanCause.REFUSED)

    def test_a_multiplier_without_a_cause_is_refused(self):
        with pytest.raises(ValueError, match="ban_cause"):
            RateLimiter(ban_multiplier=1)

    def test_a_kind_without_a_multiplier_is_refused(self):
        """`back_off_kind` answers None while unbanned, so this could not be read back."""
        with pytest.raises(ValueError, match="ban_multiplier"):
            RateLimiter(back_off_kind=BackOffKind.BUSY)

    def test_a_negative_window_is_refused(self):
        with pytest.raises(ValueError, match="banned_for"):
            RateLimiter(banned_for=-1, ban_multiplier=1, ban_cause=BanCause.REFUSED)

    def test_a_negative_last_send_is_refused(self):
        with pytest.raises(ValueError, match="seconds_since_last_send"):
            RateLimiter(seconds_since_last_send=-1)

    def test_a_multiplier_past_the_ceiling_is_refused(self):
        """The doubling stops at the ceiling, so nothing legitimate is above it."""
        with pytest.raises(ValueError, match="ceiling"):
            RateLimiter(ban_multiplier=RateLimiter.MAX_BAN_MULTIPLIER + 1, ban_cause=BanCause.REFUSED)

    def test_a_negative_multiplier_is_refused(self):
        with pytest.raises(ValueError, match="ban_multiplier"):
            RateLimiter(ban_multiplier=-1, ban_cause=BanCause.REFUSED)


class TestTheLimiterInitIsGiven:
    """`init()` accepts a limiter, so the pacing state can be the caller's to keep.

    `AniDBLink` has always taken one; `init()` is the only supported entry point
    and did not expose it, so the sole route to supplying one was to construct the
    transport directly and assign it into a module-private global -- a fork of this
    library's own construction sequence, which would break or silently drive a
    second transport the next time that sequence changed.
    """

    @pytest.fixture
    def opened(self, monkeypatch, tmp_path):
        """Run init() for real and record what reached the transport."""
        for name, value in (
            ("log", logging.getLogger("anidb_client.test")),
            ("_anidb", None),
            ("_sessionmaker", None),
            ("_engine", None),
            ("fanart_key", None),
        ):
            monkeypatch.setattr(anidb_client, name, value, raising=False)

        seen: list[dict[str, object]] = []

        class FakeLink:
            def __init__(self, *args, **kwargs):
                seen.append(kwargs)

            def stop(self, *args, **kwargs):
                pass

        monkeypatch.setattr(anidb_client.link, "AniDBLink", FakeLink)

        def go(**kwargs):
            anidb_client.init(f"sqlite:///{tmp_path}/cache.db", api_user="u", api_pass="p", **kwargs)
            return seen[-1]

        yield go

        anidb_client.close()

    def test_the_limiter_given_is_the_one_handed_over(self, opened):
        mine = RateLimiter()

        assert opened(rate_limiter=mine)["rate_limiter"] is mine

    def test_omitting_it_leaves_the_transport_to_build_its_own(self, opened):
        """None keeps today's behaviour exactly, which is what every caller that
        does not care about pacing state gets."""
        assert opened()["rate_limiter"] is None

    def test_a_resumed_limiter_arrives_still_banned(self, opened):
        """End to end: the state a previous process stored is the state the
        transport starts with, rather than something a restart forgets."""
        resumed = RateLimiter(
            banned_for=900, ban_multiplier=2, ban_cause=BanCause.SILENCE, back_off_kind=BackOffKind.BANNED
        )

        handed_over = opened(rate_limiter=resumed)["rate_limiter"]

        assert handed_over.is_banned
        assert handed_over.ban_multiplier == 2
        assert handed_over.ban_cause is BanCause.SILENCE
        assert handed_over.back_off_kind is BackOffKind.BANNED

    def test_the_limiter_is_part_of_the_declared_public_surface(self):
        """Supplying one is only supported if the type can be named.

        `BanCause` and `BackOffKind` are here for the same reason: a limiter
        resuming a back-off has to say which of the three refusals opened it and
        which kind of back-off it is, so a caller cannot construct one -- or read
        the answer back off the health surface -- without the vocabulary.
        """
        assert "RateLimiter" in anidb_client.__all__
        assert "BanCause" in anidb_client.__all__
        assert "BackOffKind" in anidb_client.__all__
