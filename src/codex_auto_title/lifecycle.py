"""Compact candidate-owned budgets, retained across connections."""

from dataclasses import dataclass

from .history import Deferred


class Exhausted(Deferred):
    pass


@dataclass
class AttemptState:
    probes: int = 0
    generations: int = 0
    context_retries: int = 0
    budget_ceiling: int | None = None

    def check(self, settings):
        if (
            self.probes >= settings.generation_attempts
            or self.generations >= settings.generation_attempts
        ):
            raise Exhausted("candidate generation/probe budget exhausted")
