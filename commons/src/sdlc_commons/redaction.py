"""Secret masking for durable text (step logs; M1-47), composed from Zyroflo (D34).

  * values of secret-NAMED variables (KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL) in the
    environment, 8 characters or longer - Zyroflo's `secret_values`: a shorter value
    (`1`, `true`) would mangle ordinary words if replaced in prose;
  * `extra` values the caller knows are secret (the TokenLean key), whatever their name;
  * a credential passed inline (`--api-key x`, `--password=x`) - `provider_smoke.safe`;
  * credential shapes (`ghp_...`, `sk-...`, JWTs, `NAME_TOKEN=...`) - `guard.scrub`.

The result is a plain `str -> str` function, which is what `agentic.telemetry.logs`
asks for (core never imports this package).
"""

import os
from collections.abc import Callable, Iterable, Mapping

from zyroflo import provider_smoke
from zyroflo.sdlc.guard import scrub as scrub_shapes


def redactor(
    env: Mapping[str, str] | None = None, extra: Iterable[str] = ()
) -> Callable[[str], str]:
    """A function masking every secret above in a text (`<redacted>`)."""
    values = dict(os.environ if env is None else env)
    secrets = provider_smoke.secret_values(values) | {value for value in extra if value}

    def redact(text: str) -> str:
        return scrub_shapes(provider_smoke.safe(text, secrets))

    return redact
