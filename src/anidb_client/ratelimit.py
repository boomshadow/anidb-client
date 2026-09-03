#!/usr/bin/env python
#
# This file is part of anidb-client.
#
# anidb-client is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# anidb-client is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with anidb-client.  If not, see <http://www.gnu.org/licenses/>.

"""Outbound pacing for the UDP transport.

This is the policy that keeps a client from being banned, so it is worth having
somewhere it can be read and tested on its own rather than inline in the send
path. AniDB's documented flood protection allows a short burst and then requires
roughly one command every four seconds; exceeding it earns a temporary IP ban
rather than an error reply.

The clock and the sleep function are injectable so the policy can be tested
without a suite that takes half an hour, and without a banned-state test blocking
for the full half-hour back-off.

**This class is touched from two threads.** The sender thread calls `wait()` and
`record_send()`; the listener thread calls `register_ban()` and `clear_ban()` as
replies arrive. Every counter here is read-modify-write, so each is guarded by a
lock -- but the lock is never held across a sleep, so a slow sender cannot stop
the listener reporting the next ban.

**Nothing sleeps a ban.** The back-off is an instant on the clock, and the sender
asks whether it has passed. It used to be a sleep taken inside the send path --
which the listener thread reaches too, through a ban notice or its own timeout
sweep -- so the thread whose only job is to read the socket could spend hours not
reading it. Waiting is not the same as being quiet, and it is being quiet that
AniDB actually asks for.
"""

import logging
import random as _random
import threading
import time as _time
from collections.abc import Callable

from anidb_client.errors import BanCause


class RateLimiter:
    """Paces outgoing commands, and backs off exponentially once banned.

    Part of the public surface, because `init()` accepts one (SPEC-006). An
    application that wants this state to outlive its process constructs a limiter
    with what it stored and hands it over; see `__init__` for what may be resumed
    and why every seed is a duration rather than an instant.
    """

    # A short opening burst is permitted before the slower steady rate applies.
    FREE_BURST = 5
    BURST_DELAY = 2
    STEADY_DELAY = 4

    # After this long with no traffic the burst allowance is considered fresh
    # again -- the server's flood counter has decayed by then too.
    IDLE_RESET = 600

    # First back-off after a ban, doubling per consecutive ban. AniDB's temporary
    # bans last on the order of half an hour, so there is no point retrying sooner.
    BAN_BASE_DELAY = 1800

    # Ceiling on that doubling, giving a longest back-off of BAN_BASE_DELAY * this.
    # A successful authentication calls clear_ban(), so in the ordinary
    # banned-then-readmitted cycle the multiplier rarely leaves 1. It compounds when
    # authentication itself keeps failing, and without a ceiling that sequence walks
    # off into delays measured in days -- a client that has, for practical purposes,
    # stopped, while reporting only that it is waiting.
    MAX_BAN_MULTIPLIER = 8

    # Fraction of the computed back-off that is fixed; the rest is chosen at
    # random each time. Full jitter -- picking uniformly from zero to the whole
    # window -- is the usual advice, and it is not quite right here: a client that
    # rolls a low number comes back almost immediately, which against a service
    # that bans on request frequency is the one thing worth not doing. Keeping a
    # floor and randomising the remainder spreads clients out without ever
    # shortening the back-off to nothing.
    #
    # It matters because the herd is real and close to home: the reported incident
    # had ten short-lived processes on one host, each authenticating for itself.
    # Banned together, they would otherwise return together, forever in step.
    BAN_JITTER_FLOOR = 0.5

    def __init__(
        self,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], object] | None = None,
        log: logging.Logger | None = None,
        random: Callable[[], float] | None = None,
        banned_for: float = 0.0,
        ban_multiplier: int = 0,
        ban_cause: BanCause | None = None,
        seconds_since_last_send: float | None = None,
    ) -> None:
        """Build a limiter, optionally resuming state from a previous process.

        Everything this class knows is per-process and lost when the process ends:
        the burst allowance, the last send, the back-off deadline and the
        multiplier. For a long-lived service that state is what stands between one
        address and an IP ban, and a restart currently begins with a clean slate --
        one burst per deploy, which is inside AniDB's allowance on its own and is
        not on the tenth restart of a crash loop.

        `init()` accepts a limiter (SPEC-006), so an application may keep this state
        wherever it likes and hand back a limiter that already knows it. This
        library deliberately does not choose that storage.

        **The seeds are durations, not instants, and that is the whole trick.**
        `time.monotonic()` has an undefined zero point -- it is not comparable
        across processes, and on most systems it is time since boot. A deadline
        persisted from the last process and restored into this one is a number that
        means nothing here, and it would mean nothing *quietly*: a restored ban
        would read as already elapsed, or as hours away, with no error either way.
        So what is handed back is "the ban has this many seconds left" and "the last
        send was this many seconds ago", which survive a restart, a reboot and a
        move to another host, and are converted to this process's clock here.

        Every seedable field has a public reader, so state that can be resumed can
        also be captured: `ban_remaining()`, `ban_multiplier`, `ban_cause` and
        `seconds_since_last_send()`.

        **Incoherent combinations are refused rather than repaired.** A back-off
        with no multiplier is the one that matters: `is_banned` reads the
        multiplier, so such a limiter would hold a deadline while reporting itself
        unbanned, and the transport would send straight through it. A ban that
        exists always has a multiplier of at least 1 -- `register_ban` sets that on
        the first ban -- so a zero here means the state being restored is already
        wrong, and quietly repairing it would hide that from whoever stored it.

        The burst allowance is deliberately not seedable. It is the least valuable
        of the counters -- a fresh burst of five sits inside AniDB's documented
        "short burst, then one per four seconds" -- and giving it a seed would mean
        inventing a reader for it purely for symmetry.
        """
        if banned_for < 0:
            raise ValueError(f"banned_for is seconds of back-off remaining and cannot be negative: {banned_for!r}")
        if seconds_since_last_send is not None and seconds_since_last_send < 0:
            raise ValueError(
                f"seconds_since_last_send is how long ago the last command went out "
                f"and cannot be negative: {seconds_since_last_send!r}"
            )
        if ban_multiplier < 0 or ban_multiplier > self.MAX_BAN_MULTIPLIER:
            raise ValueError(
                f"ban_multiplier must be between 0 (not banned) and {self.MAX_BAN_MULTIPLIER} "
                f"(the ceiling the doubling stops at), not {ban_multiplier!r}"
            )
        # `banned_for > 0` rather than a truthiness test on it: zero is a legitimate
        # value here -- a window that has elapsed while its multiplier stands -- and
        # writing it as `if banned_for` leaves a reader working out whether that case
        # was considered or merely fell through.
        if (banned_for > 0 or ban_cause is not None) and not ban_multiplier:
            raise ValueError(
                "A back-off was described with no ban_multiplier. A ban that exists has a "
                "multiplier of at least 1, so this state is already inconsistent: is_banned "
                "reads the multiplier, and a limiter seeded this way would hold a deadline "
                "while reporting itself unbanned. Pass ban_multiplier together with "
                "banned_for and ban_cause, or pass none of the three."
            )
        if ban_multiplier and ban_cause is None:
            raise ValueError(
                "A ban_multiplier was given with no ban_cause. Every refusal names which of "
                "the three it was, and ban_cause answers None while unbanned -- so a cause "
                "omitted here cannot be read back, and a caller asking why the client is "
                "gated would be told nothing."
            )

        self._monotonic = monotonic or _time.monotonic
        self._sleep = sleep or _time.sleep
        self._random = random or _random.random
        self._log = log
        self._lock = threading.Lock()
        # Zero rather than "now" when nothing is seeded: against a monotonic clock
        # that reads as an idle long enough to trip IDLE_RESET, which is what a
        # limiter that has never sent anything should look like.
        self._last_send = 0.0 if seconds_since_last_send is None else self._monotonic() - seconds_since_last_send
        self._sent_in_burst = 0
        self._ban_multiplier = ban_multiplier
        # When the back-off ends, on the monotonic clock. Kept as an instant rather
        # than a duration to sleep, because nothing sleeps it: a ban is a state the
        # sender checks before deciding whether to send at all. Seeded from a
        # duration, because an instant on this clock does not survive the process
        # that produced it.
        self._banned_until = self._monotonic() + banned_for if banned_for else 0.0
        # Why the window is open. Recorded here rather than at the call site so
        # that every refusal handed to a caller can say which of the three it is,
        # including the ones raised by a code path far from where the ban was
        # registered.
        self._ban_cause: BanCause | None = ban_cause if ban_multiplier else None

    @property
    def is_banned(self) -> bool:
        with self._lock:
            return self._ban_multiplier > 0

    @property
    def ban_multiplier(self) -> int:
        with self._lock:
            return self._ban_multiplier

    @property
    def ban_cause(self) -> BanCause | None:
        """Why the current back-off was opened, or None if there is none."""
        with self._lock:
            return self._ban_cause if self._ban_multiplier else None

    def _seconds_since_last_send(self) -> float:
        """Caller holds the lock. The public form below takes it."""
        return self._monotonic() - self._last_send

    def seconds_since_last_send(self) -> float:
        with self._lock:
            return self._seconds_since_last_send()

    def record_send(self) -> None:
        """Called once a command has actually been handed to the socket."""
        with self._lock:
            self._sent_in_burst += 1
            self._last_send = self._monotonic()

    def register_ban(self, cause: BanCause = BanCause.REFUSED) -> int:
        """Record a ban or server-busy reply and return the new multiplier.

        Doubles per consecutive ban, so a server that stays unhappy is backed away
        from rather than hammered at a fixed interval, up to MAX_BAN_MULTIPLIER.
        Opens the window in which nothing at all is sent.

        `cause` says which kind of refusal opened it. It is recorded rather than
        acted on: the policy is the same for all three, but a caller told to wait
        wants to know whether AniDB refused it, ignored it, or was never asked.
        """
        with self._lock:
            self._ban_cause = cause
            self._ban_multiplier = (
                1 if not self._ban_multiplier else min(self._ban_multiplier * 2, self.MAX_BAN_MULTIPLIER)
            )
            window = float(self.BAN_BASE_DELAY * self._ban_multiplier)
            jittered = window * (self.BAN_JITTER_FLOOR + (1.0 - self.BAN_JITTER_FLOOR) * self._random())
            self._banned_until = self._monotonic() + jittered
            return self._ban_multiplier

    def clear_ban(self) -> None:
        """Called on a successful authentication: whatever it was, it has passed."""
        with self._lock:
            self._ban_multiplier = 0
            self._banned_until = 0.0
            self._ban_cause = None

    def ban_remaining(self) -> float:
        """Seconds until anything may be sent again, or 0 if something may be now.

        This is the whole of the back-off as far as the rest of the transport is
        concerned. It replaces a sleep, and that is the point: the back-off used to
        be taken inside the send path, which the listener thread reaches -- through
        a ban notice, or through its own timeout sweep -- so the thread whose only
        job is to read the socket spent up to four hours not reading it. Every
        reply arriving in that window was lost, and no command could time out,
        because the sweep runs on that same thread.

        Reaching zero does not clear the ban. The multiplier stays until an
        authentication succeeds, so a client that comes back and is refused again
        backs off for longer rather than starting over.
        """
        with self._lock:
            if not self._ban_multiplier:
                return 0.0
            return max(0.0, self._banned_until - self._monotonic())

    def delay_for_next_send(self) -> float:
        """Seconds to wait before the next command may go out.

        Pacing only. A ban is not a delay applied here or anywhere else -- the
        sender checks `ban_remaining()` and declines to send at all, which is a
        decision made before this is ever consulted.
        """
        with self._lock:
            age = self._seconds_since_last_send()
            if age > self.IDLE_RESET:
                self._sent_in_burst = 0
                base = 0.0
            elif self._sent_in_burst < self.FREE_BURST:
                base = self.BURST_DELAY
            else:
                base = self.STEADY_DELAY
        # Time already spent since the last packet counts towards the delay, so an
        # application that is slow in its own right is not paced twice.
        return base - age

    def wait(self) -> None:
        """Block until it is acceptable to send the next command.

        Pacing only -- seconds, not half-hours. A ban is not waited out here; it is
        a state the caller checks with `ban_remaining()` before deciding to send at
        all. Sleeping through a ban meant holding a command, and the thread holding
        it, for the length of the ban.
        """
        delay = self.delay_for_next_send()
        if delay > 0:
            if self._log:
                self._log.debug(f"Delaying request with {delay} seconds")
            self._sleep(delay)
