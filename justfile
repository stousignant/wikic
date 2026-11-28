# List available recipes.
default:
    @just --list

# Everything that must pass before merge (CI lint, format, tests, privacy sweep). Never modifies files.
check: lint test
    python3 scripts/leak_sweep.py --history

# Lint and verify formatting without changing files.
lint:
    uv run --locked ruff check .
    uv run --locked ruff format --check .

# Run the test suite.
test:
    uv run --locked pytest -q

# Apply ruff formatting (modifies files).
fmt:
    uv run --locked ruff format .
