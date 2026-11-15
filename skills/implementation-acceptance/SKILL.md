---
name: implementation-acceptance
description: Verify a software implementation against its requirements before declaring completion. Use for implementation and bug-fix tasks with observable behavior.
---

# Implementation acceptance

Use this method within the existing implementation and completion-review loop. Do not create another review phase, delegate a reviewer, or restart verification merely because the runner requests a final review. Respect the runner's request and output limits.

1. Reuse current source, changes, and recorded check results. Identify which important requirements already have evidence and which still have a material gap. Reuse a passing check only while its relevant code, dependencies, and inputs remain unchanged; a passing suite proves only the cases it exercises. Keep this assessment concise in the conversation, not in a new report file.
2. For uncovered risks, ask: How could the implementation violate the requirement while existing tests still pass? Prioritize at most two concrete counterexamples in a bounded completion review, or one for a narrow fix. Follow only the relevant path from the normal entry point. Consider alternate supported input representations, failures handled at different stages, and persisted state with inconsistent references. This prioritization does not excuse a known unmet requirement.
3. Use an existing check where it covers the risk. Otherwise add or run one focused check through the actual branch with controlled dependencies and meaningful assertions. For batch failures, assert the failed item's outcome and the next item's success. Reproduce suspected defects before changing code when feasible; do not refactor or expand the test suite speculatively.
4. After a confirmed defect, apply the smallest correction and run the affected checks on the new code. Preserve unrelated passing evidence. Do not restart a project-wide review after each fix.

When the runner requests its one-time completion review, first reconcile the evidence already obtained. If it covers the important risks and no concrete defect or material gap remains, return the final task report immediately without repeating reads or checks. Otherwise spend the remaining review budget only on those gaps. Do not request another review round.

In the final report, state what was actually checked and any material unverified behavior. If a requirement remains unmet, a material verification gap remains, or a repair is unverified when the budget ends, return done=false/state=continue with the specific remaining work. Never treat budget exhaustion as success, but do not keep working merely to seek hypothetical defects after the relevant risks have been addressed.
