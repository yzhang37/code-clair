.PHONY: test

test:
	@PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s engine/runner -p 'test_*.py'
	@PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
