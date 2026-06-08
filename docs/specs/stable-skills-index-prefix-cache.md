# Spec: Decouple skills index from SOUL.md to stabilize prefix-cache KV

## Problem

The system prompt is assembled in three cache-stability tiers
(`stable → context → volatile`), but the **skills index** — the largest
single block (~13 k tokens) — is currently embedded inside **SOUL.md**
(the identity file in `$HERMES_HOME/SOUL.md`).

`SOUL.md` is placed at the very top of the `stable` tier (first thing
the model sees). The skills index inside it changes every time the user
adds, edits, or renames a skill via `skill_manage`. When that happens,
every single byte of the prefix after the mutation point is
cache-invalidated on llama.cpp / Anthropic / OpenAI prompt caches —
including the 13 k token tool-schema block that follows.

### Observed impact

On a local llama-server backend (anemll-flash-llama.cpp / Gemma4-26B,
ubatch=1, ~179 ms/token) a single skill edit forces a **full cold
prefill** of the entire ~21 k token system prompt: roughly **60 minutes**
of compute. The KV slot is saved once and restored in ~1 s on future
restarts — but any skills mutation after the save busts the slot and
restarts the 60-minute clock.

On cloud providers (Anthropic, OpenAI) the impact is a cache miss on
every turn that follows a skill mutation, paying full prefill pricing
instead of the cached rate.

### Root cause

`SOUL.md` is a user-editable file that Hermes reads verbatim. It
currently contains an `<available_skills>` block that the agent itself
updates on every `skill_manage` call. This means the *most stable*
tier of the system prompt contains the *most frequently mutated* block.

`build_skills_system_prompt()` in `agent/prompt_builder.py` already
maintains a **two-layer cache** (in-process LRU + disk snapshot keyed
by skill file mtimes). The snapshot is only invalidated when a skill
file actually changes. This is exactly the right mechanism for keeping
the skills index stable — but it runs *after* SOUL.md in the assembly
order, so a stale SOUL.md invalidates the cache before the snapshot
even runs.

---

## Proposed solution

### 1. Strip `<available_skills>` out of SOUL.md

The agent should stop writing or maintaining the skills list inside
`SOUL.md`. SOUL.md should contain only the persona, behavioral rules,
and invariants — content that changes rarely and intentionally.

Migration: on startup, if a `<available_skills>` block is found in
SOUL.md, strip it automatically (or warn and skip it) so old installs
self-heal without a user action.

### 2. Inject the skills index via `build_skills_system_prompt()` exclusively

`build_skills_system_prompt()` already returns the correct formatted
index with the two-layer cache. It is already called in the `stable`
tier of `build_system_prompt_parts()` (in `agent/system_prompt.py`).
The only change needed is to ensure nothing in SOUL.md duplicates it.

### 3. Push the skills index as late in the stable tier as possible

Current stable tier order:
```
SOUL.md / DEFAULT_AGENT_IDENTITY   ← breaks cache here on skill edit
HERMES_AGENT_HELP_GUIDANCE
TASK_COMPLETION_GUIDANCE
tool_guidance (MEMORY_, SKILLS_, etc.)
nous_subscription_prompt
TOOL_USE_ENFORCEMENT_GUIDANCE + model guidance
build_skills_system_prompt()       ← skills index lives here (~13 k tokens)
env hints, Python probe, profile hint
Platform hints
```

The skills index should remain in `build_skills_system_prompt()` and
that call should stay as late in the stable tier as feasible. The key
fix is that **SOUL.md must not contain a copy of it**, so the prefix up
to `build_skills_system_prompt()` is stable across skill mutations.

### 4. (Optional / follow-up) SOUL.md snapshot hash guard

As a belt-and-suspenders check: compute a hash of the SOUL.md content
(excluding any injected blocks) at session start. If the hash changes
mid-session (e.g. curator patched SOUL.md), invalidate the cached
system prompt so the next turn sees a consistent prompt. Currently
`invalidate_system_prompt()` is only called on context compression
events.

---

## Files to change

| File | Change |
|---|---|
| `agent/system_prompt.py` | No structural changes needed if SOUL.md is clean |
| `agent/prompt_builder.py` | Add SOUL.md sanitizer: strip `<available_skills>…</available_skills>` on load |
| `SOUL.md` (user file, not in repo) | Documented migration: remove `<available_skills>` block |
| `hermes_cli/commands.py` or skill_manage hook | Stop writing `<available_skills>` back into SOUL.md after skill mutations |
| Tests | Add regression test: SOUL.md with `<available_skills>` → assert block is stripped before prompt assembly |

---

## Expected outcome

- Prefix up to and including the persona is stable across skill edits.
- Only the `build_skills_system_prompt()` block is invalidated on a
  skill mutation — and only on the *next session start*, not mid-session,
  because the prompt is cached per `AIAgent` instance.
- On local llama-server setups with slot persistence, skill edits no
  longer bust the saved KV slot. Cold prefill cost is paid at most once
  per session start after a skill change, not on every restart.
- On cloud providers, cache hit rate improves for all users who
  customise skills.

---

## Context / discovery

Found while debugging a 60-minute cold prefill regression on a local
llama.cpp backend after memory growth caused SOUL.md to be regenerated
with an updated `<available_skills>` block. Confirmed by checking the
llama-server log: prefix mismatch started at token ~509, deep inside
the SOUL.md content, not in the volatile tier where memory lives.

The three-tier prompt cache design in `agent/system_prompt.py` is
correct and well-documented. This is a placement bug: mutable content
in an immutable-by-design slot.

---

## Related

- `agent/system_prompt.py` — three-tier assembly (stable/context/volatile)
- `agent/prompt_builder.py` — `build_skills_system_prompt()` with
  two-layer disk+LRU cache
- PR #20451 — date-only timestamp (same class of cache-stability fix)
- `references/system-prompt-invariant.md` (internal dev doc)
---

## Addendum — Verified root cause (2026-06-08)

Investigation of a live incident (local llama-server / Gemma4-26B slot bust) revealed
the **actual** primary root cause: **model-family guidance blocks injected into the
stable tier based on runtime model name**.

`build_system_prompt_parts()` injects `GOOGLE_MODEL_OPERATIONAL_GUIDANCE` when
`"gemma"` or `"gemini"` appears in `agent.model`, and `OPENAI_MODEL_EXECUTION_GUIDANCE`
for `"gpt"` / `"codex"` / `"grok"`. These are part of the **stable tier**, meaning they
are fixed for the session lifetime — but they differ between the primary provider
(e.g. `claude-sonnet-4.6` → no Google guidance) and the fallback provider
(`gemma-4-26b-a4b` → Google guidance injected). When a KV slot is saved under
the primary and then restored for a fallback session, the stable prompt differs
from byte ~1 of the guidance block, busting the entire prefix cache.

The `<available_skills>` issue (original spec) is also real and compounds the
problem: any skill edit mutates the skills index block later in the stable tier,
which also causes a prefix mismatch. Both issues are addressed by this PR.

### What this PR implements

1. **`agent/prompt_builder.py` — `load_soul_md()`**: strip any
   `<available_skills>…</available_skills>` block from SOUL.md at load time.
   Migration: existing installs self-heal without user action.

2. **`agent/system_prompt.py` — model-family guidance**: add a config key
   `agent.model_family` (`"google"` | `"openai"` | `""` / unset = auto) that
   overrides the model-name heuristic for guidance injection. When set, the stable
   prompt is byte-stable across provider switches (e.g. primary=claude, fallback=gemma
   both with `model_family: google` → same stable bytes → slot restore succeeds).

### Config usage (new)

```yaml
agent:
  model_family: google  # force Google operational guidance regardless of model name
                        # set once for a local gemma backend; clear when switching back
```

Unset (default `""`) preserves existing auto-detection behaviour — no regression.
