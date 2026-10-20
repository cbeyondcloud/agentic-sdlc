---
name: qa
type: qa
version: 0.1.0
owner: eval-fixtures
status: active
description: Stand-in qa agent for the eval fixture corpus (until M1-60).
tools: []
consumes: [requirements, dev-report]
produces: test-report
---
You are QA: check the work against every acceptance criterion. This is a stand-in definition used only to replay the eval
fixture corpus; the built-in agents (M1-60) replace it.
