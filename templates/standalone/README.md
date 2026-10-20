# Standalone policy template

The policy for using the framework without an organisation: one person, any repo, no
control plane (spec §24, "Trying it without the org setup").

```
standalone/
├── policy/               the policy folder (the same files as an org policy repo's policy/)
│   ├── tiers.yaml        capability tiers -> models   EDIT: every model is a placeholder
│   ├── agent-types.yaml  what each kind of agent may use, and its caps
│   ├── tools.yaml        tool catalog and what every agent is denied
│   ├── classifications.yaml, limits.yaml, licences.yaml, retention.yaml, prices.yaml
│   └── org.yaml          sandbox, secret scan, local pipelines allowed
└── skills/preload.yaml   which models get their skills pre-loaded (the local tier)
```

`agentic init --standalone` copies it to `~/.agentic/` (or `AGENTIC_HOME`). To use it by
hand, copy `policy/` to `~/.agentic/policy/` and `skills/preload.yaml` to
`~/.agentic/skills/preload.yaml`, then:

1. In `policy/tiers.yaml`, replace every `change-me` model with a model your gateway serves.
2. Optionally adjust caps in `policy/agent-types.yaml`. Agents may only narrow their type.
3. Run a library pipeline, for example `feature-delivery` or `bugfix`.

Because the layout matches an org policy repo, moving to org mode later is a copy into
that repo's `policy/` folder.
