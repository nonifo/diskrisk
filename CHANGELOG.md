# Changelog

## [1.2.1] — 2026-09-06

### diskinfo
- Show **raidz / raidz1–3** and **draid / draid1–3** vdev headers (not only mirrors)
- Docs: treat “same vdev” as the failure domain for mirrors and raidz

## [1.2.0] — 2026-09-06

First public release.

**Note:** development used Cursor, OpenAI Codex, Anthropic Claude, and other
LLM assistants — see [AI.md](AI.md).

### diskinfo
- Standalone ZFS vdev CLI by default (no Diskrisk required)
- Optional SMART/RISK enrichment via `SMART_RISK_URL` or `--risk`
- `--live-smart` fallback to local `smartctl` when Diskrisk is unreachable
- Install with `./install.sh --diskinfo-only`

### Diskrisk
- HTTP service: HTML UI, `/json`, `/text`
- Beszel hub SMART ingest; optional Scrutiny
- Trend history (baseline / Now / GROWING)
- Git-based install (`install.sh` / `update.sh`) with config under `/etc/diskrisk/`

[1.2.1]: https://github.com/nonifo/diskrisk/releases/tag/v1.2.1
[1.2.0]: https://github.com/nonifo/diskrisk/releases/tag/v1.2.0
