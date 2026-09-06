# AI / assistant tooling disclosure

Diskrisk and diskinfo were designed around a real DIY/TrueNAS storage
workflow (locate the right serial, check mirror partners, watch whether SMART
counters are still climbing). The **problem, behaviour, and operator UX** are
human-directed.

**Large language model (LLM) assistants were used heavily** while implementing
and packaging the public tree. Treat this as an AI-assisted project, not a
hand-typed-from-scratch codebase.

## Tools used (non-exhaustive)

| Tool | Typical role |
|------|----------------|
| **[Cursor](https://cursor.com/)** | Primary IDE / agent sessions: editing, refactors, install/docs, repo hygiene |
| **OpenAI Codex** (via Cursor and related flows) | Code generation, refactors, scripting |
| **Anthropic Claude** (via Cursor and related flows) | Code, docs, review-style passes |
| **Other LLMs / assistants** | Occasional help for wording, debugging ideas, and packaging |

Exact model names and session splits change over time; the important part is
that assistants drafted and edited substantial amounts of code and
documentation under human direction.

## What humans still own

- Product intent and what “good” looks like on a live ZFS host
- What to trust (serials, mirror partners, GROWING vs scar)
- Review before deploy, live testing, and keeping secrets out of git
- License choice (MIT) and dependency policy (stdlib + external Beszel/smartctl)

## For contributors

You do **not** need to avoid AI tools. Please:

1. Say briefly in the PR if an assistant wrote large chunks.
2. Run the result on real hardware when you can (`diskinfo`, Diskrisk against Beszel).
3. Do not commit secrets, real serials, or private IPs.

See also [CONTRIBUTING.md](CONTRIBUTING.md) and [THIRD_PARTY.md](THIRD_PARTY.md)
(runtime dependencies — separate from AI tooling).
