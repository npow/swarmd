# Round 2 Verification: Classifier Invocation Layer

**Artifact:** `2026-04-18-swarm-durability-design.md` (revised)
**Round:** 2 — targeted verification of C-6 fix + related defects M-8, M-9, M-10
**Reviewer mode:** THOROUGH (no escalation to ADVERSARIAL — critical defect count stayed at 0 for the targeted scope)

---

## Pre-commitment Predictions

Before detailed investigation, I predicted:
1. Hook contract constraint would be stated but not specific enough for implementers — **PARTIALLY CONFIRMED** (constraint is well-stated; injection text is underspecified)
2. MCP tool contracts still underspecified — **CONFIRMED** (no schemas added)
3. False-positive UX path unaddressed — **CONFIRMED** (no dismiss/decline mechanism specified)
4. STRONG injection text too vague for two implementers to converge — **CONFIRMED**
5. "Do NOT begin substantive work" lacks fallback — **CONFIRMED**

---

## C-6 (Auto-launch downgrade)

**Fixed? YES — the architectural infeasibility is resolved.**

**Evidence:**

The revised §9.3 (line 435) now opens with an explicit hook-contract constraint paragraph:

> `UserPromptSubmit` hooks can only inject `hookSpecificOutput.additionalContext` — they cannot block, suppress, or short-circuit the chat agent's response to the user's original prompt. There is no `decision: "block"` for `UserPromptSubmit` (that exists only for Stop/SubagentStop). This forces the invocation layer to use ADVISORY injection + AGENT-INITIATED launch, not silent auto-launch.

I verified this claim against the actual OMC keyword-detector hook implementation at `/Users/npow/.claude/plugins/marketplaces/omc/scripts/keyword-detector.mjs` lines 429-439:

```javascript
function createHookOutput(additionalContext) {
  return {
    continue: true,
    hookSpecificOutput: {
      hookEventName: 'UserPromptSubmit',
      additionalContext
    }
  };
}
```

The hook output format matches exactly — `continue: true` + `additionalContext` string injection. No `decision` field, no blocking capability. The spec's understanding of the constraint is accurate.

The confidence gate table (lines 437-444) replaces the old "auto-launch at ≥0.9" with "STRONG system-reminder instructing the chat agent to call `swarm.propose_criteria`". The ≥0.9 and 0.6-0.9 paths both route through `AskUserQuestion` confirmation. No silent launch exists anywhere in the revised spec.

The §12 locked-decisions row (line 563) is updated and consistent:

> `≥0.9 STRONG injection → agent-initiated swarm.propose_criteria + 1-tap confirm; 0.6-0.9 MEDIUM injection → same but softer; <0.6 treat as CHAT. No silent auto-launch (hook contract limitation, §9.3).`

**Verdict on C-6: FIXED. Downgraded from Critical to resolved.**

---

## Original motivation preserved?

**Question:** Does the revision preserve the "human forgets to type /swarm" motivation (§2.2)?

**Answer: YES, with a meaningful trade-off acknowledged.**

The original motivation (§2.2, line 29): "The `/swarm` slash command requires the human to remember to invoke it." The classifier still runs on every `UserPromptSubmit` — the CLASSIFIER does the remembering, not the human. The human's cognitive load is reduced from "remember to invoke /swarm" to "tap confirm when prompted."

The trade-off: the old spec promised zero-friction auto-launch at high confidence. The new spec requires exactly one user interaction (AskUserQuestion tap). This is documented in the "Why not true auto-launch" paragraph (line 446). The motivation is preserved because:

1. The classifier still fires on every prompt — no human memory required
2. The criteria derivation happens automatically via `swarm.propose_criteria`
3. The user sees a pre-filled confirmation, not a blank form
4. The "forget-proof" property is intact: if the human forgets /swarm, the classifier catches it

The 1-tap confirm is the minimum viable interaction given the hook contract constraint. This is an honest engineering trade-off, not a regression.

---

## MCP tool contracts (M-10 status)

**Fixed? NO — M-10 remains open. MCP tool contracts are still undefined.**

**Evidence:**

The spec names three MCP tools across multiple sections:
- `swarm.propose_criteria` — referenced at lines 439, 440, 538, 563
- `swarm.launch` — referenced at lines 439, 448, 450
- `swarm.query` — referenced at lines 443, 538

Their only structural home is §11 file layout (line 538): `mcp/server.py — MCP tools: swarm.propose_criteria, swarm.query`

No section in the spec defines:
- Request parameters (types, required/optional)
- Response schema
- Error handling / failure modes
- Who calls each tool (the chat agent? the hook? the CLI?)
- What `swarm.propose_criteria` actually returns (a full mission.yaml? just criteria? a natural-language summary?)

`swarm.launch` is not even listed in the `mcp/server.py` comment — the file layout says `swarm.propose_criteria, swarm.query` but §9.3 also requires `swarm.launch`.

**Severity: MAJOR (unchanged from Round 1)**
- Confidence: HIGH
- Why this matters: The entire §9.3 flow depends on these tools. An implementer hitting `swarm.propose_criteria` has to invent: input format, output format, whether it writes mission.yaml to disk or returns it in-memory, how the AskUserQuestion displays it, and what `swarm.launch` expects as input. Two implementers will build incompatible tools.
- Fix: Add a §6.5 or §10.x section with tool contracts:
  - `swarm.propose_criteria(prompt: str, context: list[Turn]) -> {criteria: list[Criterion], workspace: str, mission_prose: str, confidence: float}` — or whatever the actual contract is
  - `swarm.launch(mission_yaml: MissionYaml) -> {workflow_id: str, status: str}` — with lock-check semantics
  - `swarm.query(workflow_id: str | None, question: str) -> {answer: str, sources: list[str]}` — or whatever shape META queries take

---

## Validation gates (M-8, M-9 status)

### M-8 (Concurrent missions corrupt settings.json)

**Fixed? YES.**

**Evidence:** Line 450 adds workspace lock check:

> `swarm.launch` checks `$WORKSPACE/.claude/.swarm-lock` before installing settings. If another mission holds the lock, reject or queue per the user's choice in `AskUserQuestion`.

Line 572 in §12 locked decisions confirms:

> Workspace lock: `$WORKSPACE/.claude/.swarm-lock` prevents concurrent missions from corrupting settings.json. `swarm launch` checks before installing settings.

**Verdict: M-8 FIXED.** Lock file path specified, check timing specified (before install), conflict resolution specified (reject or queue via AskUserQuestion).

### M-9 (No pre-work validation gate for misclassified META queries)

**Fixed? YES.**

**Evidence:** Line 448 adds pre-launch validation:

> before calling `swarm.launch`, the chat agent validates the derived `mission.yaml` passes schema + sanity checks (≥1 criterion with non-trivial check command, workspace directory exists, criteria not trivially satisfiable). On validation failure, downgrade to explicit user-authored criteria instead of silent failure.

This addresses M-9's concern about misclassified META queries launching nonsense missions. The validation gate catches trivially-satisfiable criteria and missing workspaces before launch.

**Verdict: M-9 FIXED.** Validation checks are specified, fallback path (downgrade to user-authored) is specified.

---

## New Defects

### N-1: §13 open question contradicts §9.3 fix — stale text claims auto-launch still exists
**Severity: MAJOR**
**Confidence: HIGH**

**Evidence:** Line 581 in §13 reads:

> Auto-derived mission.yaml for classifier auto-launch. Stage 4 confidence ≥ 0.9 launches **without human confirmation.**

This directly contradicts §9.3 (line 446):

> The design trades zero-friction auto-launch for deterministic **chat-agent-initiated launch** — user sees one `AskUserQuestion` tap at high confidence.

And contradicts §12 (line 563):

> No silent auto-launch (hook contract limitation, §9.3).

The §13 text is stale — it was not updated when §9.3 was revised. An implementer reading §13 could reasonably conclude that auto-launch without confirmation is a future goal and implement it, reintroducing the C-6 race condition.

**Fix:** Update line 581 to: "Auto-derived mission.yaml for classifier-initiated launch. The chat agent calls `swarm.propose_criteria` to derive criteria from the prompt; the user confirms via AskUserQuestion. The derivation heuristics (workspace selection, criteria generation, hold_window defaults) need a template + sub-spec."

---

### N-2: STRONG injection text is described in prose but not specified as a template
**Severity: MAJOR**
**Confidence: HIGH**

**Evidence:** Line 439 describes the STRONG injection:

> Hook injects a STRONG system-reminder instructing the chat agent: "This prompt is mission-shaped (confidence X.XX). Before doing any work, call the `swarm.propose_criteria` MCP tool with the prompt + recent context; show the user the derived criteria via AskUserQuestion (single-tap confirm); then call `swarm.launch` MCP tool. Do NOT begin substantive work on the prompt before the user confirms."

This reads like a summary of intent, not a usable template. Compare with the classifier prompt in §9.2 (lines 404-431), which gives the exact prompt text. The injection text needs the same treatment because:

1. The exact wording determines whether the LLM follows the instruction or ignores it
2. "STRONG" vs "MEDIUM" is described qualitatively ("softer") but the MEDIUM text is not specified at all
3. The phrase "Do NOT begin substantive work" is the critical behavioral constraint — its placement, emphasis, and framing in the actual system-reminder determine compliance rates

**Fix:** Add a §9.3.1 with the literal injection templates for STRONG, MEDIUM, and META, similar to §9.2's classifier prompt. Include the system-reminder XML wrapper that Claude Code actually uses (e.g., `<system-reminder>...</system-reminder>`). For MEDIUM, specify how it differs from STRONG (e.g., "MAY" vs "MUST", or "consider calling" vs "call").

---

### N-3: No specified fallback when chat agent ignores the injection instruction
**Severity: MINOR**
**Confidence: MEDIUM**

**Evidence:** The entire §9.3 flow depends on the chat agent obeying the injected system-reminder. LLMs sometimes ignore instructions, especially when the user's prompt is compelling (e.g., "build me a CLI for S3 buckets" — the agent may start building before calling `swarm.propose_criteria`).

The spec acknowledges the hook is "advisory" (line 435) but provides no fallback for non-compliance. If the agent ignores the injection and starts working:
- No mission enforcement runs
- The Safety Net (§9.5 Stop-hook) catches regressions but not scope drift, premature completion, or other mission-level concerns
- The `user_override` log field (line 458) captures user overrides but not agent non-compliance

This is MINOR because: (a) in practice, system-reminders have high compliance rates with Claude models, especially with strong directive language, (b) the Safety Net partially mitigates, (c) the user can always manually invoke `/swarm`.

**Fix:** Add a sentence acknowledging this: "If the chat agent ignores the injection and begins substantive work, the Safety Net (§9.5) provides partial coverage. Full enforcement requires the user to invoke `/swarm` manually. Future: a PostToolUse hook could detect that `swarm.propose_criteria` was never called after a MISSION classification and re-inject the reminder."

---

### N-4: False-positive UX path unspecified — no dismiss mechanism for wrong MISSION classifications
**Severity: MAJOR**
**Confidence: HIGH**

**Evidence:** When the classifier incorrectly labels a CHAT prompt as MISSION (conf ≥ 0.6), the chat agent calls `swarm.propose_criteria` and shows the user an AskUserQuestion. The spec does not specify:

1. What happens when the user declines the AskUserQuestion (taps "No" or dismisses)
2. Whether the agent then proceeds with normal CHAT handling of the original prompt
3. Whether the decline is logged as `user_override` in `classifier.jsonl`
4. Whether repeated false positives degrade the user experience (every "fix this typo" getting a mission-proposal popup)

The `user_override` field exists in the classifier log (line 458) but its semantics are never defined — what triggers it, who writes it, and how it feeds back.

The §13 open question on classifier accuracy (line 577) sets thresholds ("false-positive rate > 10%") but doesn't specify the dismiss UX that generates the measurement data.

**Fix:** Add to §9.3: "If the user declines the AskUserQuestion, the chat agent proceeds with normal CHAT handling of the original prompt. The decline is logged as `user_override: 'declined'` in `classifier.jsonl`. Implementer note: AskUserQuestion must have a clear 'Not a mission — just chat' option, not just confirm/cancel."

---

### N-5: `swarm.launch` missing from §11 file layout MCP tool list
**Severity: MINOR**
**Confidence: HIGH**

**Evidence:** Line 538 in §11:

> `mcp/server.py  # MCP tools: swarm.propose_criteria, swarm.query`

But §9.3 (line 439) also requires `swarm.launch`:

> "...then call `swarm.launch` MCP tool."

`swarm.launch` is missing from the file layout comment. Minor because it's just a comment, but it signals incomplete thinking about which tools the MCP server exposes.

**Fix:** Update line 538 to: `mcp/server.py  # MCP tools: swarm.propose_criteria, swarm.launch, swarm.query`

---

## Multi-Perspective Notes

**Executor perspective:** The §9.3 flow description is narratively clear but operationally incomplete. An implementer knows the WHAT (classifier → injection → agent calls MCP tools → AskUserQuestion → launch) but not the HOW for the MCP tools. The injection templates are paraphrased, not specified. This will cause the implementer to make 5-10 design decisions that should be spec-level decisions.

**Stakeholder perspective:** The original motivation (§2.2 "humans forget") is preserved. The 1-tap confirm is a reasonable UX trade-off. The bigger stakeholder risk is false positives: if 1 in 5 CHAT prompts triggers a mission-proposal popup, the user will disable the classifier entirely. The spec sets a 10% FP threshold (§13) but doesn't specify the dismiss UX that prevents annoyance.

**Skeptic perspective:** The strongest argument against this design: it relies on an LLM (the chat agent) reliably following a system-reminder to call specific MCP tools in a specific order before doing anything else. This is a "polite request to a stochastic system" dressed up as a deterministic flow. The spec's own §5 principle 2 says "the jailer is not the prisoner" — but the invocation layer makes the chat agent (the potential prisoner) the executor of the launch sequence. The classifier (the jailer) can only advise. This tension is inherent to the hook contract constraint and cannot be fully resolved without Claude Code adding a blocking hook type for UserPromptSubmit.

---

## Self-Audit

| # | Finding | Confidence | Author could refute? | Flaw or preference? | Action |
|---|---------|-----------|---------------------|---------------------|--------|
| N-1 | §13 stale text | HIGH | NO — text is verbatim contradictory | FLAW | Keep as MAJOR |
| N-2 | Injection template missing | HIGH | PARTIALLY — could argue it's implementation detail | FLAW | Keep as MAJOR |
| N-3 | No fallback for agent non-compliance | MEDIUM | YES — could argue compliance rate is high enough | FLAW but mitigated | Keep as MINOR |
| N-4 | No dismiss UX for false positives | HIGH | NO — the gap is clear | FLAW | Keep as MAJOR |
| N-5 | swarm.launch missing from file layout | HIGH | NO — omission is verifiable | FLAW (trivial) | Keep as MINOR |

## Realist Check

| # | Realistic worst case | Mitigating factors | Detection time | Recalibration |
|---|---------------------|-------------------|---------------|---------------|
| N-1 | Implementer reads §13 and builds auto-launch, reintroducing C-6 race | §9.3 and §12 both say "no auto-launch" — implementer would likely notice the contradiction | During implementation or code review | Stays MAJOR — the text is factually wrong and creates confusion |
| N-2 | Two implementers write different injection text; one version has low compliance | Implementation plan phase will force this decision anyway | During implementation | Stays MAJOR — but mitigated by the fact that writing-plans will likely add this detail |
| N-4 | User gets annoyed by false-positive popups, disables classifier, loses the "forget-proof" property entirely | 10% FP threshold in §13 provides a measurement plan | After production use | Stays MAJOR — this is a product-design gap, not just an engineering gap |

---

## Overall Verdict

**VERDICT: ACCEPT-WITH-RESERVATIONS**

The C-6 fix is sound. The architectural infeasibility (hook cannot short-circuit) is explicitly documented, the auto-launch path is removed, and the replacement flow (classifier → injection → agent-initiated MCP call → AskUserQuestion → launch) is coherent and feasible. The original motivation ("human forgets to type /swarm") is preserved — the classifier does the remembering, the user just confirms.

**Reservations (3 MAJOR, 2 MINOR):**

1. **N-1 (MAJOR):** §13 still claims auto-launch exists. Fix is a one-line text edit.
2. **N-2 (MAJOR):** Injection templates need to be literal, not paraphrased. Add §9.3.1.
3. **N-4 (MAJOR):** False-positive dismiss UX is unspecified. Add decline semantics.
4. **M-10 (MAJOR, carried from R1):** MCP tool contracts still undefined. Add §6.5.
5. **N-3 (MINOR):** No fallback for agent ignoring injection. Acknowledge and defer.
6. **N-5 (MINOR):** `swarm.launch` missing from file layout comment.

**What would upgrade to ACCEPT:** Fix N-1 (30 seconds), add injection templates (N-2), and add MCP tool contracts (M-10). N-4 can be deferred to implementation plan if the writing-plans phase will address UX flows.

**Recommendation:** These reservations are all addressable in the writing-plans phase without another spec revision cycle. Proceed to `superpowers:writing-plans` with the understanding that the plan must include: (a) literal injection templates, (b) MCP tool request/response schemas, (c) AskUserQuestion decline handling. The §13 stale text (N-1) should be fixed immediately as it takes 30 seconds and removes a contradiction.
