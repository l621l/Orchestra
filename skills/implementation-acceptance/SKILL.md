---
name: implementation-acceptance
description: Verify a software implementation against its requirements before declaring completion. Use for implementation and bug-fix tasks with observable behavior.
---

# Implementation acceptance

Turn each material requirement into an observable result. Before editing, identify the entry point, the boundary where the behavior can fail, and the evidence that would distinguish success from a plausible broken implementation. For a small fix, keep this to the affected behavior and its nearest regression.

Check each materially different failure boundary covered by the requirement. If processing must continue after one item fails, inject failures at distinct stages where handling differs, then assert both the failed item's outcome and the next item's success. If inputs have different representations, cover one valid case that takes a different path from the common case. For persisted data, test a valid round trip and a structurally valid record with inconsistent identity, references, or state when those invariants matter.

Prefer a test through the normal entry point with controlled dependencies. For a reported defect, reproduce it before the fix when feasible. For a new feature, make at least one important assertion that a superficially working implementation would fail. Reuse existing fixtures and checks.

Before reporting completion, map each material requirement to a specific passing check or concrete evidence. Run the relevant checks after the last behavior change. Report any unverified behavior explicitly and keep material gaps open. A passing suite only establishes the cases it exercises.
