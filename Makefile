PYTHON ?= python
NPM ?= npm
WEB_DIR ?= web

.PHONY: generate test web web-deps web-check web-test

generate:
	$(PYTHON) protocol/proto/generate.py

test:
	$(PYTHON) -m pytest protocol/tests server/tests client/tests

# Dépendances du front : `npm ci` seulement quand `node_modules` manque, pour
# que `make web` reste rapide après une première installation.
web-deps:
	@cd $(WEB_DIR) && if [ ! -d node_modules ]; then $(NPM) ci; fi

# Build de la SPA servi par la Console (`web/dist`). Jamais committé.
web: web-deps
	cd $(WEB_DIR) && $(NPM) run build

web-check: web-deps
	cd $(WEB_DIR) && $(NPM) run typecheck

web-test: web-deps
	cd $(WEB_DIR) && $(NPM) test
