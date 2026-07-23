# Contributing to Mimir

Thanks for considering contributing — every bit of help matters.

## Ways to contribute

- Report bugs or suggest features via [GitHub Issues](https://github.com/kirrns/mimir/issues)
- Improve documentation
- Contribute code or tests
- Review open pull requests

## Getting started

```bash
git clone https://github.com/kirrns/mimir && cd mimir
pip install -e '.[dev,mcp,embed]'
pytest
```

Branch from `main`. Keep pull requests focused — one change, one PR.

## Before opening a PR

- Add or update tests for any behavior change (the offline suite must stay
  token-free; live-model tests are opt-in, see `bench/live.py`)
- Run `pytest` locally — the full suite should pass
- Keep the diff scoped to the stated change; avoid unrelated refactors

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating, you agree to abide by it.

## Questions

Open an issue, or email **kiransala.dev@gmail.com**.
