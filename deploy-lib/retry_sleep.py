"""How long the deploy waits before its next attempt.

A base delay comes off the caller's schedule; this adds the jitter. The base-delay
calculation and jitter draw are separate functions because only the draw is random: `sleep_seconds` is a
decision about a number and can be driven with every jitter value, while
`draw_jitter` is the one place a source of randomness is consulted.

WHY JITTER AT ALL, given a deploy is strictly serial and has no fleet to spread
out: the pipeline's own retries can line up with the control-plane recovery they
are waiting on, and a fixed schedule retries into the same instant every time. A
second of spread is enough to break that, which is why the range is small and
why nothing depends on its distribution.

A base of 1 or less takes no jitter. At 0 the caller asked for no wait at all -
the test harnesses set the schedule to zeros - and a jittered zero would put a
random second back into a suite that asked for none.
"""

from __future__ import annotations

import random

# The widest the wait may be stretched, in seconds. One second: enough to
# decorrelate competing retries landing on the same instant, small enough that the
# schedule the caller wrote is still the schedule it gets.
MAX_JITTER_SECONDS = 1


def draw_jitter(rng: random.Random) -> int:
    """A jitter value in [0, MAX_JITTER_SECONDS]. The only random step."""
    return rng.randint(0, MAX_JITTER_SECONDS)


def sleep_seconds(base_seconds: int, jitter_seconds: int) -> int:
    """The wait for one attempt: the base, plus jitter once the base earns it."""
    if base_seconds <= 0:
        return 0
    if base_seconds <= 1:
        return base_seconds
    return base_seconds + jitter_seconds
