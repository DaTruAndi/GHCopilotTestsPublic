# Agent Orchestration Methods

This document describes every mechanism available for structuring work across
multiple agents, sub-agents, or AI models in the GitHub Copilot Coding Agent
environment.

---

## 1. `.github/agents/*.md` — Declarative Agent Definitions

**What it is:** Markdown files with YAML front-matter that declare a named
agent persona, the model that drives it, and its system-prompt instructions.

**How the model is chosen:** The `model:` field in the front-matter selects
the exact LLM.  GitHub Copilot resolves the model at invocation time.

**When to use:** When you want a reusable, named agent that humans (or other
agents) can reference by name in a chat or workflow context.

**Example** (`.github/agents/analyze-software.md`):

```markdown
---
name: Analyze Software
description: Analyzes the software environment.
model: claude-sonnet-4.6
---
You are the software analysis agent. Report OS, kernel, Python runtime …
```

**Files in this repo:**

| File | Model | Aspect |
|---|---|---|
| `analyze-software.md`  | `claude-sonnet-4.6`    | OS, kernel, Python, hypervisor |
| `analyze-processor.md` | `gemini-3-pro-preview` | CPU topology, cache, speedup |
| `analyze-storage.md`   | `gpt-4.1`              | Filesystem mounts, capacity |
| `analyze-memory.md`    | `claude-opus-4.6`      | RAM, swap, allocation limits |

---

## 2. `task` Tool — Imperative Sub-Agent Spawning

**What it is:** A tool available to the Copilot Coding Agent during PR work.
Launches a specialised sub-agent in a *separate context window* with its own
model and instructions.

**Agent types:**

| `agent_type`       | Default model    | Best for |
|---|---|---|
| `explore`          | claude-haiku-4.5 | Fast codebase search / Q&A |
| `task`             | claude-haiku-4.5 | Running CLI commands, builds, tests |
| `general-purpose`  | claude-sonnet-4.6| Complex multi-step reasoning |

**Model override:** Pass `model="<name>"` to use any supported model instead
of the default.

**Parallel dispatch:** Call multiple `task` tools *in the same response* — the
orchestrator launches them concurrently.

**Sequential chaining:** Call them one after another, passing results forward.

**Example (parallel, 4 different models):**

```python
# All four dispatched simultaneously in one orchestrator turn:
task(agent_type="general-purpose", model="claude-sonnet-4.6",    prompt="Analyze software …")
task(agent_type="general-purpose", model="gemini-3-pro-preview",  prompt="Analyze processor …")
task(agent_type="general-purpose", model="gpt-4.1",               prompt="Analyze storage …")
task(agent_type="general-purpose", model="claude-opus-4.6",       prompt="Analyze memory …")
```

---

## 3. Model Availability — Live Parallel Probe Results

All 17 candidate models were probed **simultaneously** (parallel `task` calls
in a single orchestrator response) with a trivial echo task.

| Model | Status | Notes |
|---|---|---|
| `claude-sonnet-4.6`    | ✅ Available | Used for **software** analysis |
| `claude-sonnet-4.5`    | ✅ Available | |
| `claude-haiku-4.5`     | ❌ No response | Task returned empty |
| `claude-opus-4.6`      | ✅ Available | Used for **memory** analysis |
| `claude-opus-4.6-fast` | ❌ Unsupported | `CAPIError 400: model not supported` |
| `claude-opus-4.5`      | ✅ Available | |
| `claude-sonnet-4`      | ✅ Available | |
| `gemini-3-pro-preview` | ✅ Available | Used for **processor** analysis |
| `gpt-5.3-codex`        | ✅ Available | |
| `gpt-5.2-codex`        | ✅ Available | |
| `gpt-5.2`              | ❌ Unsupported | `CAPIError 400: model not supported` |
| `gpt-5.1-codex-max`    | ✅ Available | |
| `gpt-5.1-codex`        | ✅ Available | |
| `gpt-5.1`              | ❌ Unsupported | `CAPIError 400: model not supported` |
| `gpt-5.1-codex-mini`   | ✅ Available | |
| `gpt-5-mini`           | ✅ Available | |
| `gpt-4.1`              | ✅ Available | Used for **storage** analysis |

**Summary:** 13 of 17 available; 4 unavailable at time of probe.

---

## 4. Python `concurrent.futures` — Within-Process Parallelism

**What it is:** Standard Python threading/multiprocessing inside a single
agent's context.  All code runs under the *same* model.

**When to use:** Parallelise I/O-bound work (reading files, making system
calls) or CPU-bound work (benchmarks) *within* one agent's task.  This is
**not** multi-model orchestration — it does not change which LLM is driving
the work.

**Example** (`tests/test_parallel_analysis.py`):

```python
with ThreadPoolExecutor(max_workers=4) as executor:
    futures = {executor.submit(fn): name for name, fn in TASKS.items()}
    for future in as_completed(futures):
        results[futures[future]] = future.result()
```

---

## 5. GitHub Actions Matrix Jobs

**What it is:** A `.github/workflows/*.yml` file that defines a job matrix.
Each matrix entry runs as a separate, independent runner.

**Multi-model use:** Each job can call a different model via the GitHub Models
API (or any LLM API), providing true external-process parallelism.

**Example:**

```yaml
jobs:
  analyze:
    strategy:
      matrix:
        model: [claude-sonnet-4.6, gpt-4.1, gemini-3-pro-preview]
    steps:
      - run: python analyze.py --model ${{ matrix.model }}
```

---

## Summary: Method Comparison

| Method | True multi-model? | Parallel? | Scope |
|---|---|---|---|
| `.github/agents/*.md`      | ✅ Yes | When invoked together | Declarative, reusable |
| `task` tool                | ✅ Yes | ✅ Yes (same response) | Imperative, ad-hoc |
| Python `concurrent.futures`| ❌ No  | ✅ Yes (threads/procs) | Within one model |
| GitHub Actions matrix      | ✅ Yes | ✅ Yes (runners)       | CI/CD pipeline |
