"""Agent evaluation (M1-63, spike S10): corpus, blinding, judging, statistics, A/B decisions.

pydantic-evals (the optional extra `eval`) runs the cases; everything that decides a
verdict - blinding, judging, the statistics and the A/B rule - is ours, so the engine
can be swapped without touching how a result is reached.

Pydantic AI prints an observability banner on its first run unless this is set; an
eval prints a report, not an advert, so it is set before pydantic-ai is used here.
"""

import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
