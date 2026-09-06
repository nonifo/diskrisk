# Contributing

Thanks for looking at Diskrisk. Issues and pull requests are welcome.

This repository is **AI-assisted** (Cursor, Codex, Claude, and other tools).
See [AI.md](AI.md) for how that was used and what we still expect from humans.

## Ways to help

- **Bug reports** — what you ran, host OS / ZFS version, and anonymized output
- **Feature ideas** — especially other pool layouts, HBAs, or SMART quirks
- **Docs** — clearer wording, extra examples, translations
- **Code** — keep the stack small: stdlib Python + bash, no required `pip`

## Development notes

```bash
git clone https://github.com/nonifo/diskrisk.git
cd diskrisk
cp config.example.env config.env   # never commit secrets
python3 smart_risk.py text         # one-shot against Beszel
./diskinfo/diskinfo.sh --version
bash -n diskinfo/diskinfo.sh install.sh update.sh
```

- Prefer clear, operator-facing wording over clever abstractions.
- Do not commit `config.env`, credentials, or real serials/IPs.
- Match existing style in `smart_risk.py` and `diskinfo/diskinfo.sh`.

## Pull requests

1. Open an issue first for larger changes (optional for tiny fixes).
2. Keep diffs focused.
3. Update `docs/` and `CHANGELOG.md` when behaviour changes.
4. Bump `VERSION` / `__version__` / `diskinfo` `VERSION=` together when releasing.
5. If an LLM wrote large parts of the change, say so in the PR (one line is enough).

## Release assets (optional checksums)

Install is normally via `git clone` + `./update.sh`. For people who download a
release archive, publish a **SHA-256** checksum (not MD5):

```bash
./scripts/release-assets.sh 1.3.5
# → dist/diskrisk-1.3.5.tar.gz
# → dist/SHA256SUMS

gh release create "v1.3.5" --title "…" --notes-file - \
  dist/diskrisk-1.3.5.tar.gz dist/SHA256SUMS
# or: gh release upload "v1.3.5" dist/diskrisk-1.3.5.tar.gz dist/SHA256SUMS
```

Verify a download:

```bash
sha256sum -c SHA256SUMS
```
