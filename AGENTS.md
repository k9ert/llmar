# AGENTS.md

Notes for agents working on this repo.

## Layout

- `llmar` — the entire CLI. Single Python file, stdlib only.
- `tests/test_llmar.py` — unittest suite. Subprocess-driven; each test points
  the script at temp dirs via env vars.
- `.github/workflows/test.yml` — CI matrix on py3.10/3.11/3.12. Reusable via
  `workflow_call`.
- `.github/workflows/release.yml` — triggered by `v*` tags. Reuses test.yml,
  injects the tag into `__version__`, checksums, attaches `llmar` +
  `llmar.sha256` to a GitHub Release.

## Run tests

```sh
python3 -m unittest tests.test_llmar -v
```

No deps. Tests spin up an in-process HTTP server for `update` cases.

## Release flow

1. Land changes on `main`. CI must be green.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. Release workflow: seds `__version__ = "dev"` → `__version__ = "vX.Y.Z"`,
   then `sha256sum`. Order matters — checksum must be of the version-injected
   bytes.

## Env vars used by tests

- `LLMAR_LOCAL_DIR`, `LLMAR_ARCHIVE_DIR`, `LLMAR_REGISTRY` — point at temp dirs.
- `LLMAR_RELEASE_API`, `LLMAR_RELEASE_DOWNLOAD` — redirect `update` to a local
  fake HTTP server. Hardcoded to `k9ert/llmar` URLs in production.

## Conventions

- Keep `llmar` a single file, stdlib-only. No new deps.
- Be terse in commit messages and code comments. Sacrifice grammar for
  concision.
- Don't add docstrings/comments that just restate the code.
- New commands: add `cmd_*` function + argparse subparser + entry in the
  `handlers` dict in `main()`. Mirror the existing `_validate_exists` /
  `ensure_description` patterns where applicable.
- New tests go in `tests/test_llmar.py` as a `TestX(Base)` class. Use the
  `Base.run_cli` helper.

## Self-update gotchas

- `cmd_update` writes to `Path(__file__).resolve()` via a same-dir tmpfile +
  `os.replace`. If you change the file's location semantics, re-verify
  atomicity.
- The sanity check requires the downloaded payload to start with
  `#!/usr/bin/env python3` and contain `def main()`. If you ever rename
  `main()`, update the check.
