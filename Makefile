# Plotter. Everything here runs as your own user: no sudo anywhere.
#
#   make            list the targets
#   make up         app + WAF, the production shape
#   make dev        app only, on 127.0.0.1, no WAF
#   make serve      no container at all, uv + uvicorn with reload
#
# Override anything on the command line, e.g.
#   make up PORT=9000
#   make warm STATE_DIR=/srv/plotter
.DEFAULT_GOAL := help
SHELL := /bin/bash

# Container engine. podman compose works too: make ENGINE=podman
ENGINE      ?= docker
COMPOSE     ?= $(ENGINE) compose
PORT        ?= 8088
STATE_DIR   ?=
SERVERNAME  ?=
# Passed through to deploy.sh, which writes them into .env.
DEPLOY_ENV  := PLOTTER_PUBLIC_PORT=$(PORT) \
               $(if $(STATE_DIR),PLOTTER_STATE_DIR=$(STATE_DIR),) \
               $(if $(SERVERNAME),PLOTTER_SERVERNAME=$(SERVERNAME),)
# One-off container for the maintenance CLI. --no-deps so it does not drag
# the stack up just to run a command.
CLI         := $(COMPOSE) run --rm --no-deps app

.PHONY: help
help:  ## show this help
	@echo "Plotter make targets:"
	@grep -hE '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) \
	  | sort | awk -F':.*## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Variables: PORT=$(PORT) ENGINE=$(ENGINE) STATE_DIR=<host dir> SERVERNAME=<vhost>"

# --- running -----------------------------------------------------------------

.PHONY: up
up:  ## build and start app + Apache/ModSecurity WAF, on PORT
	$(DEPLOY_ENV) ./deploy/deploy.sh

.PHONY: dev
dev:  ## build and start the app alone on 127.0.0.1, no WAF
	$(DEPLOY_ENV) ./deploy/deploy.sh --dev

.PHONY: restart
restart:  ## restart the stack without rebuilding the image
	$(DEPLOY_ENV) ./deploy/deploy.sh --no-build

.PHONY: build
build:  ## build the app image from uv.lock
	$(COMPOSE) build

.PHONY: down
down:  ## stop and remove the containers (state and images are kept)
	$(COMPOSE) down --remove-orphans

.PHONY: logs
logs:  ## follow the container logs
	$(COMPOSE) logs -f --tail 100

.PHONY: ps
ps:  ## what is running
	$(COMPOSE) ps

.PHONY: shell
shell:  ## a shell inside the app container
	$(CLI) sh

.PHONY: serve
serve:  ## run the app on the host with uv and reload, no container
	uv run uvicorn plotter.main:app --reload --port $(PORT)

# --- data --------------------------------------------------------------------

.PHONY: warm
warm:  ## pre-download elevation tiles (Estonia + Finland is ~4 GB)
	$(CLI) plotter-refresh warm

.PHONY: refresh
refresh:  ## refresh masts and the frequency register now
	$(CLI) plotter-refresh refresh all --tolerant

.PHONY: discover
discover:  ## inspect the live JVIS portal before harvesting it
	$(CLI) plotter-refresh discover

.PHONY: timer
timer:  ## install the nightly refresh as a systemd user timer
	./deploy/user-timer.sh install

.PHONY: timer-status
timer-status:  ## when the nightly refresh last ran and runs next
	./deploy/user-timer.sh status

# --- development -------------------------------------------------------------

.PHONY: sync
sync:  ## create or update .venv from uv.lock, including dev tools
	uv sync --group dev

.PHONY: lock
lock:  ## re-resolve uv.lock after editing pyproject.toml
	uv lock

.PHONY: test
test: sync  ## run the test suite
	uv run pytest tests -q

.PHONY: lint
lint: sync  ## pyflakes over the package and tests
	uv run pyflakes plotter tests

.PHONY: check
check: test lint  ## tests plus lint, what CI would care about

.PHONY: health
health:  ## curl the health endpoint (sends the WAF's ServerName if .env has one)
	@name=$$(sed -n 's/^PLOTTER_SERVERNAME=//p' .env 2>/dev/null | tail -1); \
	curl -fsS $${name:+-H "Host: $$name"} http://127.0.0.1:$(PORT)/api/health; echo

.PHONY: clean
clean:  ## remove the venv, caches and build leftovers (state is untouched)
	rm -rf .venv .pytest_cache *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
