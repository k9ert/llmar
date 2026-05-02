# llmar

LM Studio model archiver. Move local LLM models between your fast local disk
and an external archive drive. Works with LM Studio + oMLX (both use the
two-level `<publisher>/<model-name>/...` layout).

Single-file Python script. No dependencies beyond stdlib.

## Install

Download the latest release into your PATH:

```sh
mkdir -p ~/.local/bin
curl -L -o ~/.local/bin/llmar https://github.com/k9ert/llmar/releases/latest/download/llmar
chmod +x ~/.local/bin/llmar
```

Make sure `~/.local/bin` is on your `PATH`.

### Verify checksum (optional)

```sh
curl -LO https://github.com/k9ert/llmar/releases/latest/download/llmar
curl -LO https://github.com/k9ert/llmar/releases/latest/download/llmar.sha256
shasum -a 256 -c llmar.sha256   # macOS
sha256sum -c llmar.sha256       # linux
```

### Requirements

- Python 3.10+
- `rsync` recommended (used for copies if available; falls back to `shutil.copytree`)

## Update

```sh
llmar update              # latest stable release
llmar update v0.2.0       # specific tag
llmar update --pre        # include pre-releases
llmar update --force      # reinstall current version
```

## Usage

```sh
llmar list                        # show local + archive models
llmar pin    <publisher/model>    # keep on local disk
llmar unpin  <publisher/model>
llmar archive [<model>]           # move to archive (one model, or all unpinned)
llmar backup  [<model>]           # copy to archive, keep local
llmar restore <model>             # copy from archive back to local
llmar desc    <model> [text]      # show or set description
llmar log     <model> [text]      # append journal entry
```

Every command that touches a model needs a one-line description. You'll be
prompted for one if it's missing.

## Configuration

| env var                    | default                              |
| -------------------------- | ------------------------------------ |
| `LLMAR_LOCAL_DIR`          | `~/.lmstudio/models`                 |
| `LLMAR_ARCHIVE_DIR`        | `/Volumes/INTENSO/llm-archive`       |
| `LLMAR_REGISTRY`           | `~/.llmar/registry.json`             |
| `LLMAR_RELEASE_API`        | `https://api.github.com/repos/k9ert/llmar` |
| `LLMAR_RELEASE_DOWNLOAD`   | `https://github.com/k9ert/llmar/releases/download` |

## Development

```sh
python3 -m unittest tests.test_llmar -v
```

Tagged pushes (`v*`) trigger the release workflow which runs the test matrix,
injects the tag into `__version__`, checksums, and uploads `llmar` +
`llmar.sha256` to a GitHub Release.
