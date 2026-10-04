# Tongle

**Keep your knowledge up to date with what you learn.**

Tongle turns conversations, documents, and corrections into source-linked, editable local knowledge. It revises existing notes, preserves conditions and disagreements, and makes current knowledge available to later AI tasks.

[中文](README.md) · [Download preview](https://github.com/sishuai33-commits/tongle-next/releases/tag/v0.2.0-preview.1) · [Quick start](docs/quickstart.md)

## What makes this project worth trying?

Try Tongle when you work on long-running projects or research and care about **how a conclusion changes when its evidence changes**. Source versions, manual edits, independent content review and publication checks are part of the maintenance workflow.

For example: a release rule later acquires a payment-related exception. Tongle can revise the existing knowledge and, for an explicitly enabled synthesis topic, stop serving the stale summary until it is reviewed again. Whether the next AI task actually applies that exception correctly is a separate evaluation.

This is a product focus, not a claim of unique technology or demonstrated superiority over other memory systems.

## Run

Download the macOS Apple Silicon or Windows x64 archive, extract the entire folder, and open `开始使用.command` (Mac) or `开始使用.cmd` (Windows). The interactive guide is currently in Chinese. Python is bundled; Pi and Obsidian are optional integrations.

Or use Python 3.11+:

```sh
git clone https://github.com/sishuai33-commits/tongle-next.git
cd tongle-next
python3 -m tongle.product guide
```

Bring your own compatible model endpoint and API key. Markdown and the source/version ledger stay local, but relevant content is sent to your chosen provider when model-powered processing is enabled. Offline mode supports registration, storage and retrieval without semantic compilation.

## Preview boundaries

- Unsigned terminal-based preview. Not a desktop GUI; platform validation is documented in [release validation](docs/release-validation.md).
- Bounded topic synthesis and outcome-driven learning are opt-in experiments, disabled by default.
- Topic synthesis requires complete inputs: at most 12 cards, 16 sources and 48,000 characters; larger topics wait for intervention.
- Correct revision and delivery do not guarantee correct use by an AI. Long-term task gains and competitive advantages remain unproven.
- No model-weight training or claim of general recursive self-improvement.

[MIT License](LICENSE). Contributions and small, redacted failure cases are welcome. Never attach API keys or private knowledge databases to issues.
