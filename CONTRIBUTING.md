# Contributing

Use Python 3.11+ and synthetic data in temporary directories. The core has no third-party runtime dependencies. Do not use your live knowledge base for tests.

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
python3 scripts/build_release.py --to dist/Tongle-source.zip
```

For binary builds, create an isolated virtual environment, install the pinned build-only dependency from `requirements-build.txt`, then run:

```sh
python scripts/build_binary.py --to dist/Tongle-local
python tests/windows_acceptance.py --binary dist/Tongle-local/tongle --report dist/acceptance.json
```

On Windows use `tongle.exe`. The historical acceptance script name is retained; its synthetic loopback test runs on both supported platforms. It does not measure model intelligence.

Describe changes and relevant validation. Keep manual edits, provenance, bounded work and recovery semantics intact. Report issues with redacted minimal examples; never upload credentials, conversations, private documents or runtime databases. Cross-platform builds are defined in `.github/workflows/preview.yml`.
