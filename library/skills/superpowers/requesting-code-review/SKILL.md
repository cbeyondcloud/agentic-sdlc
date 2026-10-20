---
name: requesting-code-review
description: Use when reviewing completed work against its plan or requirements - the review checklist, how to calibrate severity, and the shape of the findings.
---

# Code Review Checklist

Review completed work against its plan or requirements and identify issues before they cascade into more work. You get precisely crafted context for evaluation: the requirements, the design and the change. Judge the work product, not anyone's thought process.

**Core principle:** Review early, review often.

## The spec is a vision document

The spec says what the software must do. It does not enumerate every input, environment, or condition the software will meet. For behavior the spec is silent on, judge by what a reasonable person using this software would expect: a reasonable person's expectation is a requirement, and a spec's silence is not permission. Grade such findings by their effect on that person, not by whether the spec mentions the trigger.

## Declined to judge

Before your verdict, list every behavior you considered and set aside as outside the plan or spec, one line each, with the reason. A person rules on each line at the gate; nothing you set aside is dropped silently. An empty list means you set nothing aside.

## Read-only review

Your review is read-only. Do not mutate the working tree, the index, HEAD, or branch state in any way. Use tools like `git show`, `git diff`, and `git log` to inspect history.

## Do it yourself

Do all of this review yourself, in one seat. If the diff feels too large for one pass, review it in passes and say so in your report.

## What to Check

**Plan alignment:**
- Does the implementation match the plan / requirements?
- Are deviations justified improvements, or problematic departures?
- Is all planned functionality present?

**Code quality:**
- Clean separation of concerns?
- Proper error handling?
- Type safety where applicable?
- DRY without premature abstraction?
- Edge cases handled?

**Architecture:**
- Sound design decisions?
- Reasonable scalability and performance?
- Security concerns?
- Integrates cleanly with surrounding code?

**Testing:**
- Tests verify real behavior, not mocks?
- Edge cases covered?
- Integration tests where they matter?
- All tests passing?

**Production readiness:**
- Migration strategy if schema changed?
- Backward compatibility considered?
- Documentation complete?
- No obvious bugs?

## Calibration

Categorize issues by actual severity. Not everything is Critical. Acknowledge what was done well before listing issues — accurate praise helps the implementer trust the rest of the feedback.

If you find significant deviations from the plan, flag them specifically so the implementer can confirm whether the deviation was intentional. If you find issues with the plan itself rather than the implementation, say so.

Severities, as your handoff records them:

- **critical** (must fix): bugs, security issues, data loss risks, broken functionality
- **high** (should fix): architecture problems, missing features, poor error handling, test gaps
- **medium** and **low** (nice to have): code style, optimization opportunities, documentation polish

For each issue:
- File:line reference
- What's wrong
- Why it matters
- How to fix (if not obvious)

## Output

- **Strengths**: what's well done, specifically.
- **Issues**, by severity, each as above.
- **Recommendations**: improvements for code quality, architecture, or process.
- **Assessment**: ready to merge? Yes, no, or with fixes, and one or two sentences of technical reasoning.

## Critical Rules

**DO:**
- Categorize by actual severity
- Be specific (file:line, not vague)
- Explain WHY each issue matters
- Acknowledge strengths
- Give a clear verdict

**DON'T:**
- Say "looks good" without checking
- Mark nitpicks as Critical
- Give feedback on code you didn't actually read
- Be vague ("improve error handling")
- Avoid giving a clear verdict
