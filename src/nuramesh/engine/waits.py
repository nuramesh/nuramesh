"""The clock of a durable run's human wait (ND55, T72): pure arithmetic, no I/O, no Temporal.

A task waiting in `verifying` for a person has a working expiry: 90 days from the capture of its
output, not renewed. The orchestrator asks this clock what is due next (a warning at a lead time
before the expiry, or the expiry itself) and tells it when that has fired. Kept apart from the
workflow so the arithmetic can be tested without a server, and so the in-process and durable
paths cannot drift.

The clock counts from the time the wait was **observed**: `waited` is how long the task had
already waited when the orchestrator first looked (the time the gates took after the submit move),
so the deadline is `now + working - waited` on the orchestrator's own clock.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class WaitClock:
    """Deadline and the warnings still to send."""

    deadline: datetime
    leads: list[int] = field(default_factory=list)
    """Lead times (days before the deadline) not yet sent, descending."""

    @staticmethod
    def start(
        now: datetime, waited: timedelta, working: timedelta, leads: list[int]
    ) -> "WaitClock":
        return WaitClock(deadline=now + working - waited, leads=list(leads))

    def due(self) -> tuple[datetime, int | None]:
        """What fires next and when: `(time, lead)` for a warning, `(deadline, None)` for the
        expiry. A time at or before `now` means it is already due."""
        if self.leads:
            return self.deadline - timedelta(days=self.leads[0]), self.leads[0]
        return self.deadline, None

    def fire(self, now: datetime) -> int | None:
        """Take the next timer: the lead time of the warning to send, or `None` for the expiry.
        If a later warning is already due as well, the older one is dropped, not sent late. Once
        the deadline itself is reached no warning is sent at all (it would say "N days left" about
        a wait that is over): the expiry comes next."""
        if now >= self.deadline:
            self.leads.clear()
        if not self.leads:
            return None
        while len(self.leads) > 1 and self.deadline - timedelta(days=self.leads[1]) <= now:
            self.leads.pop(0)
        return self.leads.pop(0)
