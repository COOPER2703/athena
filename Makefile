PYTHON ?= python

.PHONY: generate test

generate:
	$(PYTHON) protocol/proto/generate.py

test: generate
	$(PYTHON) -m pytest protocol/tests
