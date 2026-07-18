.PHONY: test demo clean

test:
	python3 -m unittest discover -s tests -v

demo:
	python3 -m reliable_automation --state-dir runtime/demo demo all --reset

clean:
	rm -rf runtime build dist *.egg-info .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
