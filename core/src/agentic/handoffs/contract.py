"""What a step's agent must hand over, as the runner's output contract (M1-20).

`HandoffContract` gives the runner two things: the instructions that show the
agent its kind's template (merged with the run's extensions, D37), and the check
that turns its answer into the final handoff or lists what is wrong. The runner
feeds those errors back once (Pydantic AI's output retry), then reports the run
`invalid` for a human gate.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agentic.handoffs.finalise import (
    BLOCK_ON_DEFAULT,
    EnvelopeStamp,
    check_block_on,
    finalise_handoff,
)
from agentic.handoffs.model import HandoffError
from agentic.handoffs.schema import HandoffSchema, render_template

INSTRUCTIONS = """\
<output-format kind="{kind}">
Finish by answering with one handoff document and nothing else: a line '---', YAML fields, \
a line '---', then the Markdown body. The framework fills in kind, schema_version, run_id, \
step, agent and produced_at. You write status (complete, partial or blocked) and the fields \
shown below. Keep every section heading; replace every example value and every \
<!-- guidance --> comment with your own content.

{template}
</output-format>"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class HandoffContract:
    schema: HandoffSchema
    run_id: str
    step: str
    agent: str  # name@version
    block_on: frozenset[str] = BLOCK_ON_DEFAULT
    clock: Callable[[], datetime] = field(default=_utc_now)

    def __post_init__(self) -> None:
        # Caller mistakes surface here, not on the agent's first answer.
        check_block_on(self.block_on)
        EnvelopeStamp(self.run_id, self.step, self.agent, self.clock())

    @property
    def digest(self) -> str:
        """What a paused run was checked against: the schema and the blocking severities."""
        return f"{self.schema.digest}|block_on={','.join(sorted(self.block_on))}"

    @property
    def instructions(self) -> str:
        return INSTRUCTIONS.format(kind=self.schema.kind, template=render_template(self.schema))

    def check(self, text: str) -> tuple[str | None, list[str]]:
        """(the final document, []) or (None, every problem with the answer)."""
        stamp = EnvelopeStamp(self.run_id, self.step, self.agent, self.clock())
        try:
            document, _ = finalise_handoff(
                text, schema=self.schema, stamp=stamp, block_on=self.block_on
            )
        except HandoffError as error:
            return None, list(error.errors)
        except Exception as error:  # an answer odd enough to break a parser: still feedback
            return None, [
                f"the answer could not be read ({type(error).__name__}); send plain YAML "
                "front matter between '---' lines, then the Markdown body"
            ]
        return document, []
