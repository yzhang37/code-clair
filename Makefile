.PHONY: test

test:
	@CLARITY_LANG=zh-Hans PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s engine/runner -p 'test_*.py'
	@CLARITY_LANG=zh-Hans PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
