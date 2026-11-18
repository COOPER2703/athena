PYTHON ?= python
NPM ?= npm
WEB_DIR ?= web

.PHONY: generate test web web-deps web-check web-test e2e

generate:
	$(PYTHON) protocol/proto/generate.py

test:
	$(PYTHON) -m pytest

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

# Parcours navigateur de bout en bout (#179) : le build réel de la SPA est servi
# par le serveur du seam partagé, avec un faux modèle et aucun secret réel. Le
# serveur tourne dans un répertoire temporaire, sur des ports éphémères, et un
# superviseur le relance à la même adresse après chaque sortie propre.
# Chromium : le système est utilisé s'il existe ; sinon, lancez une fois
# `cd web && npx playwright install chromium`.
e2e: generate web
	cd $(WEB_DIR) && ATHENA_E2E_PYTHON=$(PYTHON) $(NPM) run test:e2e
