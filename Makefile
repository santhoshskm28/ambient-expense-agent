.PHONY: install playground run-server generate-traces grade eval

install:
	agents-cli install

playground:
	agents-cli playground

run-server:
	uv run uvicorn expense_agent.fast_api_app:app --host 0.0.0.0 --port 8080

generate-traces:
	uv run python tests/eval/generate_traces.py

grade:
	agents-cli eval grade

eval: generate-traces grade
