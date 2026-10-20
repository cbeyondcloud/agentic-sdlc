"""Detectors of our own, beside detect-secrets' regex catalogue (review F1).

detect-secrets 1.5.0 (2024-05) has no rule for the key formats an agent meets
most often today, nor for a password written as an assignment. These rules are
data: a name policy can list in `secret_scan.detectors`, what it finds in words,
and its patterns. A pattern with a group named `secret` masks only that group
(the value, not the name it is assigned to).

The assignment rules stay narrow on purpose: tool output is scanned under the
same policy, so a rule that fired on code naming a password (`password =
os.environ[...]`) would withhold ordinary source files. A value must be a quoted
literal (or an unquoted env-file value), at least six characters, with a letter
and a digit, and not a template (`${...}`, `{{ ... }}`, `<...>`, `%...%`) or an
obvious stand-in (`your-...`, `example...`, `abc123...`).
"""

#: A value that is a template or an obvious stand-in, not a secret.
_STAND_INS = "your|example|sample|dummy|fake|placeholder|changeme|xxx"
_NOT_A_PLACEHOLDER = rf"(?![$<{{%])(?!(?i:{_STAND_INS}))"

#: name -> (what it finds, patterns). Names follow detect-secrets' plugin names.
KEY_FORMATS: dict[str, tuple[str, tuple[str, ...]]] = {
    "AnthropicKeyDetector": (
        "Anthropic API Key",
        (r"sk-ant-(?:api|admin|oat|sid)\d{2}-[A-Za-z0-9_\-]{32,}",),
    ),
    "OpenAIProjectKeyDetector": (
        "OpenAI Project Key",
        (r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{32,}",),
    ),
    "GoogleApiKeyDetector": ("Google API Key", (r"AIza[0-9A-Za-z_\-]{35}",)),
    "GitHubFineGrainedTokenDetector": (
        "GitHub Fine-grained Token",
        (r"github_pat_[A-Za-z0-9_]{22,}",),
    ),
    "HuggingFaceTokenDetector": (
        "Hugging Face Token",
        (r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{30,}",),
    ),
    "PasswordAssignmentDetector": (
        "Password Assignment",
        (
            # name = "value" / "name": "value" / name: 'value' (code, JSON, YAML); the
            # name's start is not matched and every run is bounded: no quadratic backtracking
            r"(?i)(?:password|passwd|passphrase|secret[_\-]?key|client[_\-]?secret|app[_\-]?secret"
            r"|api[_\-]?key|access[_\-]?key|private[_\-]?key"
            r"|auth[_\-]?token|access[_\-]?token|api[_\-]?token|refresh[_\-]?token)"
            r"[A-Za-z0-9_.\-]{0,64}[\"']?[ \t]*(?::=|=>|[:=])[ \t]*(?P<quote>[\"'])"
            rf"(?P<secret>{_NOT_A_PLACEHOLDER}(?=[^\"'\s]{{0,512}}\d)"
            r"(?=[^\"'\s]{0,512}[A-Za-z])(?![^\"'\s]{0,512}\.\.\.)[^\"'\s]{6,512})(?P=quote)",
            # NAME=value in an env file or shell: unquoted, no spaces around '=' (code
            # constants such as `X_PASSWORD = re.compile(...)` have them)
            r"(?m)^[ \t]*(?:export[ \t]+)?[A-Z0-9_]{0,64}"
            r"(?:PASSWORD|PASSWD|PASSPHRASE|SECRET_KEY|CLIENT_SECRET|APP_SECRET"
            r"|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|AUTH_TOKEN|ACCESS_TOKEN|API_TOKEN)"
            rf"[A-Z0-9_]{{0,64}}=(?P<secret>{_NOT_A_PLACEHOLDER}(?!0x)(?=\S{{0,512}}\d)"
            r"(?=\S{0,512}[A-Za-z])[^\s\"'#;(){}\[\],]{6,512})",
        ),
    ),
}

#: Values published as examples in vendor documentation: never a live secret.
DOCUMENTED_EXAMPLES = frozenset(
    {
        "AKIA" + "IOSFODNN7EXAMPLE",  # AWS's documented example access key id
        "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY",  # ...and its secret key
    }
)
