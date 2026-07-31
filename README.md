# AP Agent

Python 3.12 project for AP Agent.

## Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install
```

## Development

```bash
# Run tests
pytest

# Run linter
ruff check .

# Format code
ruff format .
```

## Project Structure

```
src/apagent/          # Main package
├── llm/              # LLM integration
├── agent/            # Agent logic
├── extraction/       # Data extraction
├── matching/         # Matching logic
├── rules/            # Rules engine
├── scheduling/       # Scheduling logic
└── api/              # API endpoints

scripts/              # Utility scripts
eval/                 # Evaluation code
tests/                # Test suite
```
