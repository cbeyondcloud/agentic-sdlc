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

from agentic.handoffs.finalise import BLOCK_ON_DEFAULT, EnvelopeStamp, finalise_handoff
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
        return document, []
