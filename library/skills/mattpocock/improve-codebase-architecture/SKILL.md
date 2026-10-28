---
name: improve-codebase-architecture
description: Scan a codebase for deepening opportunities, present them as candidates in your handoff, then grill through whichever one is picked at the gate.
---

# Improve Codebase Architecture

Surface architectural friction and propose **deepening opportunities**: refactors that turn shallow modules into deep ones. The aim is testability and AI-navigability.

This skill is _informed_ by the project's domain model and built on a shared design vocabulary:

- The architecture vocabulary: **module**, **interface**, **depth**, **seam**, **adapter**, **leverage**, **locality**, and its principles (the deletion test, "the interface is the test surface", "one adapter = hypothetical seam, two = real"). Use these terms exactly in every suggestion, and don't drift into "component," "service," "API," or "boundary."
- The domain language in `GLOSSARY.md` gives names to good seams; ADRs in `docs/adr/` record decisions this skill should not re-litigate.

## Process

### 1. Explore

**Scope before you scan: YAGNI.** Deepening a module pays off by making future changes to it easier, so put extra weight on the parts of the codebase that have recently changed. Decide *where* to look before you look:

- If the user named a direction (a module, a subsystem, a pain point), take it, and skip the inference below.
- Otherwise, walk back a good stretch of the commit history (`git log --oneline`) to find the codebase's hot spots, the files and areas that keep coming up, and let those paths pull your attention first. If the changes are scattered with no clear hot spot, widen the net.

Read the project's domain glossary (`GLOSSARY.md`) and any ADRs in the area you're touching first.

Then walk the codebase yourself. Don't follow rigid heuristics; explore organically and note where you experience friction:

- Where does understanding one concept require bouncing between many small modules?
- Where are modules **shallow**, with an interface nearly as complex as the implementation?
- Where have pure functions been extracted just for testability, but the real bugs hide in how they're called (no **locality**)?
- Where do tightly-coupled modules leak across their seams?
- Which parts of the codebase are untested, or hard to test through their current interface?

Apply the **deletion test** to anything you suspect is shallow: would deleting it concentrate complexity, or just move it? A "yes, concentrates" is the signal you want.

### 2. Present candidates in your handoff

Write the candidates into your handoff document, one section per candidate, with:

- **Files**: which files/modules are involved
- **Problem**: why the current architecture is causing friction
- **Solution**: plain English description of what would change
- **Benefits**: explained in terms of locality and leverage, and how tests would improve
- **Before / After**: a short sketch of the shallowness and the deepening (a list or a small diagram in text)
- **Recommendation strength**: one of `Strong`, `Worth exploring`, `Speculative`

End with a **Top recommendation**: which candidate you'd tackle first and why.

**Use GLOSSARY.md vocabulary for the domain, and the vocabulary above for the architecture.** If `GLOSSARY.md` defines "Order," talk about "the Order intake module," not "the FooBarHandler," and not "the Order service."

**ADR conflicts**: if a candidate contradicts an existing ADR, only surface it when the friction is real enough to warrant revisiting the ADR. Mark it clearly in the candidate (e.g. _"contradicts ADR-0007, but worth reopening because…"_). Don't list every theoretical refactor an ADR forbids.

Do NOT propose interfaces yet. Ask under Open Questions: "Which of these would you like to explore?"

### 3. Grilling loop

When the gate's feedback picks a candidate, walk the decision tree with the user, one round of Open Questions at a time: constraints, dependencies, the shape of the deepened module, what sits behind the seam, what tests survive.

Record decisions as they crystallize, with `mattpocock/domain-modeling` to keep the domain model current:

- **Naming a deepened module after a concept not in `GLOSSARY.md`?** Propose the term for `GLOSSARY.md` in your handoff.
- **Sharpening a fuzzy term during the conversation?** Propose the updated definition in your handoff.
- **User rejects the candidate with a load-bearing reason?** Offer an ADR, framed as: _"Want this recorded as an ADR so future architecture reviews don't re-suggest it?"_ Only offer when the reason would actually be needed by a future explorer to avoid re-suggesting the same thing; skip ephemeral reasons ("not worth it right now") and self-evident ones.
- **Want to explore alternative interfaces for the deepened module?** Design it twice: sketch two different interfaces yourself and compare them.
