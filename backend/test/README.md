# Backend tests

Upstream MoneyPrinterTurbo's suite, kept so changes to the vendored backend can
be checked against the behaviour it was written for.

Tests covering components this build does not ship were moved to
`packaging/removed-tests/` rather than left failing: the Streamlit WebUI
(replaced by the React frontend), the CLI entry point, and the agent skill
script. None of their code is in `app/`.

Several remaining tests were updated where this build deliberately changed
behaviour — each carries a comment explaining why:

- `test_video.py` — the default encoder is `h264_mf` rather than `libx264`, and
  the fallback is an ordered chain instead of a fixed codec.
- `test_video.py::test_bundled_fonts_script_coverage` — new; pins which scripts
  the bundled OFL fonts render, after the proprietary fonts were removed.
- `test_config.py` — `config.example.toml` moved into `resource/`.

Run them with the data and resource directories pointed somewhere disposable:

```bash
MPT_ROOT_DIR=/tmp/mpt-test MPT_RESOURCE_DIR="$PWD/resource" \
  uv run pytest test/ -q
```
