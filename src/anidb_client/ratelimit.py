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
"""

import logging
import time as _time
from collections.abc import Callable


class RateLimiter:
    """Paces outgoing commands, and backs off exponentially once banned."""

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

    def __init__(
        self,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], object] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._monotonic = monotonic or _time.monotonic
        self._sleep = sleep or _time.sleep
        self._log = log
        self._last_send = 0.0
        self._sent_in_burst = 0
        self._ban_multiplier = 0

    @property
    def is_banned(self) -> bool:
        return self._ban_multiplier > 0

    @property
    def ban_multiplier(self) -> int:
        return self._ban_multiplier

    def seconds_since_last_send(self) -> float:
        return self._monotonic() - self._last_send

    def record_send(self) -> None:
        """Called once a command has actually been handed to the socket."""
        self._sent_in_burst += 1
        self._last_send = self._monotonic()

    def register_ban(self) -> int:
        """Record a ban or server-busy reply and return the new multiplier.

        Doubles per consecutive ban, so a server that stays unhappy is backed away
        from rather than hammered at a fixed interval, up to MAX_BAN_MULTIPLIER.
        """
        self._ban_multiplier = 1 if not self._ban_multiplier else min(self._ban_multiplier * 2, self.MAX_BAN_MULTIPLIER)
        return self._ban_multiplier

    def clear_ban(self) -> None:
        """Called on a successful authentication: whatever it was, it has passed."""
        self._ban_multiplier = 0

    def delay_for_next_send(self) -> float:
        """Seconds to wait before the next command may go out.

        Excludes any ban back-off, which `wait()` applies first and separately.
        """
        age = self.seconds_since_last_send()
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
        """Block until it is acceptable to send the next command."""
        if self.is_banned:
            ban_delay = self.BAN_BASE_DELAY * self._ban_multiplier
            if self._log:
                self._log.warning(f"API not available, will wait for {ban_delay / 60} minutes")
            self._sleep(ban_delay)

        delay = self.delay_for_next_send()
        if delay > 0:
            if self._log:
                self._log.debug(f"Delaying request with {delay} seconds")
            self._sleep(delay)
