"""Policy engine: decides allow / confirm / deny for every physical action.

Order of evaluation (no override can lift a guard):

  guards      frozen -> who may do this (agent scope and tier ceiling, or a rule's
              ceiling) -> offline -> bounds -> human hold -> lease
              -> rate limit / circuit breaker
  base        tier -> effect (default T0/T1 allow, T2/T3 confirm) + required assurance
  overrides   the first matching [[policy.rules]] entry may set the effect
  escalation  unknown state on T2+ -> confirm; quiet hours -> confirm

"Safer" commands (lock, heater off, valve close, stop) stay available while
frozen or during a human hold: agents can always make things safer.

Who is asking matters (model.Principal). The guards that hold agents back do not
apply to a person acting through the fabric, and a person's own channel counts
toward the approval a tier needs. A rule runs without per-action approval up to
the tier ceiling it was approved with, and otherwise yields like an agent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .model import KIND_AGENT, KIND_HUMAN, KIND_RULE, Assurance, Decision, Effect, Thing, Tier
from .util import glob, match_thing


@dataclass
class AgentPolicy:
    name: str
    max_tier: Tier = Tier.T1
    things: tuple[str, ...] = ("*",)  # what it may control
    deny: tuple[str, ...] = ()  # hidden from it entirely: no control, no state, no events
    observe: tuple[str, ...] | None = None  # what it may read; None = everything not denied
    presence: bool = True  # may it see which room people are in

    def controls(self, thing: Thing) -> bool:
        return any(match_thing(p, thing) for p in self.things) and not any(
            match_thing(p, thing) for p in self.deny)

    def observes(self, thing: Thing) -> bool:
        if any(match_thing(p, thing) for p in self.deny):
            return False
        return self.observe is None or any(match_thing(p, thing) for p in self.observe)


@dataclass
class Bound:
    param: str
    min: float | None = None
    max: float | None = None
    thing: str = "*"
    capability: str = "*"
    note: str = ""


@dataclass
class Override:
    """One [[policy.rules]] entry. `agent` is matched against the principal id, so "*"
    covers people and rules too, and "rule:*" or "human:*" single them out."""

    effect: Effect
    agent: str = "*"
    thing: str = "*"
    capability: str = "*"
    command: str = "*"
    min_tier: Tier | None = None
    assurance: Assurance | None = None
    note: str = ""

    def matches(self, agent: str, thing: Thing, cap_id: str, command: str, tier: Tier) -> bool:
        return (
            glob(self.agent, agent)
            and match_thing(self.thing, thing)
            and glob(self.capability, cap_id)
            and glob(self.command, command)
            and (self.min_tier is None or tier >= self.min_tier)
        )


@dataclass
class QuietHours:
    start: dt.time
    end: dt.time
    escalate_from: Tier = Tier.T1

    def active(self, local: dt.datetime) -> bool:
        now = local.time()
        if self.start <= self.end:
            return self.start <= now < self.end
        return now >= self.start or now < self.end  # crosses midnight


def _default_effects() -> dict[Tier, Effect]:
    return {Tier.T0: Effect.ALLOW, Tier.T1: Effect.ALLOW, Tier.T2: Effect.CONFIRM, Tier.T3: Effect.CONFIRM}


def _default_assurance() -> dict[Tier, Assurance]:
    return {Tier.T0: Assurance.CLI, Tier.T1: Assurance.DESKTOP, Tier.T2: Assurance.DESKTOP,
            Tier.T3: Assurance.PRESENCE}


@dataclass
class PolicyConfig:
    tier_effects: dict[Tier, Effect] = field(default_factory=_default_effects)
    tier_assurance: dict[Tier, Assurance] = field(default_factory=_default_assurance)
    approval_timeout_s: float = 120.0
    human_hold_s: float = 600.0
    human_hold_origins: tuple[str, ...] = ("human", "external")
    allow_safer_when_frozen: bool = True
    allow_safer_during_hold: bool = True
    rate_per_agent_per_minute: int = 30
    rate_per_thing_per_minute: int = 6
    suspend_after_denials_per_minute: int = 10
    suspend_s: float = 300.0
    lease_max_s: float = 3600.0
    quiet_hours: QuietHours | None = None
    bounds: list[Bound] = field(default_factory=list)
    rules: list[Override] = field(default_factory=list)
    agents: dict[str, AgentPolicy] = field(default_factory=dict)
    # No rule acts above this tier, whatever it was approved with. Raising it to T3
    # lets an approved rule unlock a door on its own: a deliberate, written decision.
    rules_max_tier: Tier = Tier.T2
    # After a request lapses unanswered, the approve button only takes a repeat of
    # that same request for this long (default: the approval timeout). See approvals.py.
    swap_guard_s: float | None = None

    def agent(self, name: str) -> AgentPolicy | None:
        return self.agents.get(name) or self.agents.get("*")

    def can_control(self, name: str, thing: Thing) -> bool:
        agent = self.agent(name)
        return agent is not None and agent.controls(thing)

    def can_observe(self, name: str, thing: Thing) -> bool:
        agent = self.agent(name)
        return agent is not None and agent.observes(thing)

    def sees_presence(self, name: str) -> bool:
        agent = self.agent(name)
        return agent is not None and agent.presence

    @property
    def swap_guard(self) -> float:
        return self.approval_timeout_s if self.swap_guard_s is None else self.swap_guard_s


@dataclass
class Context:
    """Everything the engine needs, gathered by the fabric for one request."""

    agent: str
    thing: Thing
    capability: str
    command: str
    args: dict[str, Any]
    tier: Tier
    safer: bool
    online: bool
    frozen: bool
    local_time: dt.datetime
    human_hold_left_s: float = 0.0
    human_hold_attr: str = ""
    lease_holder: str | None = None
    lease_left_s: float = 0.0
    rate_ok: bool = True
    rate_code: str = "ok"
    rate_retry_s: float = 0.0
    state_known: bool = True
    approved: Assurance = Assurance.NONE  # set when re-evaluating an approved request
    kind: str = KIND_AGENT  # who is asking: see model.Principal
    principal_assurance: Assurance = Assurance.NONE  # human: what their channel proves
    principal_max_tier: Tier | None = None  # rule: the ceiling it was approved with
    author: str | None = None  # rule: the agent that proposed it


class PolicyEngine:
    def __init__(self, config: PolicyConfig):
        self.config = config

    def _deny(self, ctx: Context, code: str, reason: str) -> Decision:
        return Decision(Effect.DENY, ctx.tier, code, [reason], safer=ctx.safer)

    def evaluate(self, ctx: Context) -> Decision:
        c = self.config
        what = f"{ctx.capability}.{ctx.command} on {ctx.thing.id}"
        human = ctx.kind == KIND_HUMAN

        # ---------------------------------------------------------- guards
        # The emergency stop is for agents and rules. People are never locked out
        # of their own home by it.
        if ctx.frozen and not human and not (ctx.safer and c.allow_safer_when_frozen):
            return self._deny(ctx, "frozen", "the fabric is frozen (emergency stop); only safer actions run")

        if ctx.kind == KIND_RULE:
            ceiling = min(ctx.principal_max_tier if ctx.principal_max_tier is not None else Tier.T1,
                          c.rules_max_tier)
            if ctx.tier > ceiling:
                return self._deny(ctx, "tier_exceeds_rule",
                                  f"{what} is {ctx.tier}; {ctx.agent} is limited to {ceiling}")
            if ctx.author is not None:  # a rule can never reach further than the agent that proposed it
                author = c.agent(ctx.author)
                if author is None or not author.controls(ctx.thing) or ctx.tier > author.max_tier:
                    return self._deny(ctx, "not_permitted",
                                      f"{ctx.agent} was proposed by agent {ctx.author!r}, "
                                      f"which may not do this to {ctx.thing.id}")
        elif not human:
            agent = c.agent(ctx.agent)
            if agent is None:
                return self._deny(ctx, "unknown_agent", f"agent {ctx.agent!r} has no policy")
            if not agent.controls(ctx.thing):
                return self._deny(ctx, "not_permitted", f"agent {ctx.agent!r} may not control {ctx.thing.id}")
            if ctx.tier > agent.max_tier:
                return self._deny(
                    ctx, "tier_exceeds_agent",
                    f"{what} is {ctx.tier}; agent {ctx.agent!r} is limited to {agent.max_tier}",
                )

        if not ctx.online:
            return self._deny(ctx, "offline", f"{ctx.thing.id} is offline or its backend is disconnected")

        for bound in c.bounds:
            if bound.param in ctx.args and glob(bound.capability, ctx.capability) and match_thing(bound.thing, ctx.thing):
                value = ctx.args[bound.param]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue  # a bound only limits numbers
                if (bound.min is not None and value < bound.min) or (bound.max is not None and value > bound.max):
                    lo = "-inf" if bound.min is None else bound.min
                    hi = "inf" if bound.max is None else bound.max
                    note = f" ({bound.note})" if bound.note else ""
                    return self._deny(
                        ctx, "out_of_bounds",
                        f"{bound.param}={value} is outside the allowed range [{lo}, {hi}]{note}",
                    )

        if not human:
            if ctx.human_hold_left_s > 0 and not (ctx.safer and c.allow_safer_during_hold):
                minutes = max(1, round(ctx.human_hold_left_s / 60))
                return self._deny(
                    ctx, "human_hold",
                    f"a person changed {ctx.thing.id} ({ctx.human_hold_attr}) recently; agents yield "
                    f"for about {minutes} more minute(s)",
                )

            if ctx.lease_holder and ctx.lease_holder != ctx.agent and not ctx.safer:
                return self._deny(
                    ctx, "leased",
                    f"{ctx.thing.id} is leased by agent {ctx.lease_holder!r} for {round(ctx.lease_left_s)}s more",
                )

            if not ctx.rate_ok:
                msg = (f"{ctx.agent} is suspended after repeated denials" if ctx.rate_code == "agent_suspended"
                       else "rate limit reached")
                return self._deny(ctx, ctx.rate_code, f"{msg}; retry in {round(ctx.rate_retry_s)}s")

        # ---------------------------------------------------------- base
        effect = c.tier_effects.get(ctx.tier, Effect.CONFIRM)
        assurance = c.tier_assurance.get(ctx.tier, Assurance.PRESENCE)
        code = "tier"
        reasons = [f"{what} is {ctx.tier}"]
        if ctx.kind == KIND_RULE and effect == Effect.CONFIRM:
            effect, code = Effect.ALLOW, "rule"
            reasons.append("within the tier ceiling this rule was approved with")

        # ---------------------------------------------------------- overrides
        for override in c.rules:
            if override.matches(ctx.agent, ctx.thing, ctx.capability, ctx.command, ctx.tier):
                effect = override.effect
                if override.assurance is not None:
                    assurance = override.assurance
                code = "rule"
                reasons.append(f"rule: {override.note or override.effect.value}")
                break

        if effect == Effect.DENY:
            return Decision(Effect.DENY, ctx.tier, code, reasons, safer=ctx.safer)

        # ---------------------------------------------------------- escalation
        if ctx.kind == KIND_AGENT:
            if effect == Effect.ALLOW and ctx.tier >= Tier.T2 and not ctx.state_known and not ctx.safer:
                effect, code = Effect.CONFIRM, "state_unknown"
                reasons.append("current state is unknown, so a human should confirm")
            if (
                effect == Effect.ALLOW
                and c.quiet_hours is not None
                and ctx.tier >= c.quiet_hours.escalate_from
                and not ctx.safer
                and c.quiet_hours.active(ctx.local_time)
            ):
                effect, code = Effect.CONFIRM, "quiet_hours"
                assurance = max(assurance, Assurance.DESKTOP)
                reasons.append("quiet hours are active")

        if effect == Effect.CONFIRM:
            if ctx.approved >= assurance:
                return Decision(Effect.ALLOW, ctx.tier, "approved", reasons + [f"approved ({ctx.approved})"],
                                safer=ctx.safer)
            if human and ctx.principal_assurance >= assurance:
                return Decision(Effect.ALLOW, ctx.tier, "operator",
                                reasons + [f"a person acted on a {ctx.principal_assurance} channel"],
                                safer=ctx.safer)
        return Decision(effect, ctx.tier, code, reasons,
                        assurance if effect == Effect.CONFIRM else Assurance.NONE, safer=ctx.safer)
