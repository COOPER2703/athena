PYTHON ?= python

.PHONY: generate test

generate:
	$(PYTHON) protocol/proto/generate.py

test:
	$(PYTHON) -m pytest protocol/tests server/tests client/tests
