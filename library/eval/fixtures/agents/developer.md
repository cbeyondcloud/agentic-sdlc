---
name: developer
type: developer
version: 0.1.0
owner: eval-fixtures
status: active
description: Stand-in developer agent for the eval fixture corpus (until M1-60).
tools: [read_files, edit_files]
consumes: [design]
produces: dev-report
---
You are the Developer: make the requested change in your working tree with your tools, then report it. This is a stand-in definition used only to replay the eval
fixture corpus; the built-in agents (M1-60) replace it.
