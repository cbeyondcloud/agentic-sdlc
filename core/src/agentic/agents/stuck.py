"""Stuck-loop detection (M1-18, spec §8.5).

Port of OpenHands software-agent-sdk `openhands/sdk/conversation/stuck_detector.py`
at commit c8a65f0 (MIT License, Copyright (c) OpenHands contributors; see
THIRD_PARTY_NOTICES.md). The patterns and default thresholds are OpenHands';
the events are this framework's own, built from Pydantic AI messages, and
compared by digest so ids never make two identical steps look different.

Patterns, over the events since the last user message (at most 20):
  * repeat      - the same action with the same result, `action_observation` times
  * error_loop  - the same action failing, more than `action_error` times in a row
  * monologue   - `monologue` agent messages in a row with no action between
  * ping_pong   - actions and results alternating A, B, A, B over `alternating` steps

`oscillation` is the pipeline-level check: a loop whose review findings or
developer output repeat across iterations ends early and escalates (M1-24).
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

MAX_EVENTS_TO_SCAN = 20


@dataclass(frozen=True)
class Action:
    tool: str
    args_digest: str


@dataclass(frozen=True)
class Observation:
    tool: str
    content_digest: str
    error: bool = False


@dataclass(frozen=True)
class Message:
    text_digest: str


@dataclass(frozen=True)
class UserMessage:
    text_digest: str


Event = Action | Observation | Message | UserMessage


@dataclass(frozen=True)
class StuckThresholds:
    action_observation: int = 4
    action_error: int = 3
    monologue: int = 3
    alternating: int = 6


@dataclass(frozen=True)
class StuckVerdict:
    pattern: Literal["repeat", "error_loop", "monologue", "ping_pong"]
    detail: str


class StuckDetector:
    def __init__(self, thresholds: StuckThresholds | None = None) -> None:
        self.t = thresholds or StuckThresholds()

    def check(self, events: list[Event]) -> StuckVerdict | None:
        events = _since_last_user(events)
        if len(events) < min(self.t.action_observation, self.t.action_error, self.t.monologue):
            return None
        needed = max(self.t.action_observation, self.t.action_error + 1)
        actions, observations = _recent(events, needed)
        if self._repeating(actions, observations):
            return StuckVerdict(
                "repeat", f"the same call and result {self.t.action_observation} times in a row"
            )
        if _error_streak(actions, observations) > self.t.action_error:
            return StuckVerdict("error_loop", "the same call kept failing")
        if self._monologue(events):
            return StuckVerdict("monologue", f"{self.t.monologue} messages in a row and no action")
        if len(events) >= self.t.alternating and self._alternating(events):
            return StuckVerdict("ping_pong", "alternating between the same two steps")
        return None

    def _repeating(self, actions: list[Action], observations: list[Observation]) -> bool:
        n = self.t.action_observation
        if len(actions) < n or len(observations) < n:
            return False
        return all(a == actions[0] for a in actions[:n]) and all(
            o == observations[0] for o in observations[:n]
        )

    def _monologue(self, events: list[Event]) -> bool:
        count = 0
        for event in reversed(events):
            if not isinstance(event, Message):
                break
            count += 1
        return count >= self.t.monologue

    def _alternating(self, events: list[Event]) -> bool:
        n = self.t.alternating
        actions, observations = _recent(events, n)
        if len(actions) < n or len(observations) < n:
            return False
        return all(actions[i] == actions[i + 2] for i in range(n - 2)) and all(
            observations[i] == observations[i + 2] for i in range(n - 2)
        )


def events_from_messages(messages: list[ModelMessage]) -> list[Event]:
    """The run's history as stuck-detection events, in order."""
    events: list[Event] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    events.append(UserMessage(_digest(part.content)))
                elif isinstance(part, ToolReturnPart):
                    events.append(Observation(part.tool_name, _digest(part.content)))
                elif isinstance(part, RetryPromptPart):
                    events.append(Observation(part.tool_name or "", _digest(part.content), True))
        elif isinstance(message, ModelResponse):
            calls = [p for p in message.parts if isinstance(p, ToolCallPart)]
            for call in calls:
                events.append(Action(call.tool_name, _digest(call.args_as_dict())))
            if not calls:
                text = "".join(p.content for p in message.parts if isinstance(p, TextPart))
                if text:
                    events.append(Message(_digest(text)))
    return events


def stuck_check(detector: StuckDetector) -> Callable[[list[ModelMessage]], str | None]:
    """The runner's stuck check: the pattern's name, or None."""

    def check(messages: list[ModelMessage]) -> str | None:
        verdict = detector.check(events_from_messages(messages))
        return verdict.pattern if verdict else None

    return check


def oscillation(previous: list[str], current: str, window: int = 2) -> bool:
    """Whether `current` repeats one of the last `window` outputs of a loop
    (compared after normalising case and whitespace)."""
    seen = {_normalised(text) for text in previous[-window:]}
    return _normalised(current) in seen


def _since_last_user(events: list[Event]) -> list[Event]:
    for index in range(len(events) - 1, -1, -1):
        if isinstance(events[index], UserMessage):
            events = events[index + 1 :]
            break
    return events[-MAX_EVENTS_TO_SCAN:]


def _recent(events: list[Event], needed: int) -> tuple[list[Action], list[Observation]]:
    """The last `needed` actions and observations, most recent first."""
    actions: list[Action] = []
    observations: list[Observation] = []
    for event in reversed(events):
        if isinstance(event, Action) and len(actions) < needed:
            actions.append(event)
        elif isinstance(event, Observation) and len(observations) < needed:
            observations.append(event)
        if len(actions) >= needed and len(observations) >= needed:
            break
    return actions, observations


def _error_streak(actions: list[Action], observations: list[Observation]) -> int:
    if not actions or not observations:
        return 0
    streak = 0
    for action, observation in zip(actions, observations, strict=False):
        if action != actions[0] or not observation.error:
            break
        streak += 1
    return streak


def _digest(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _normalised(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()
