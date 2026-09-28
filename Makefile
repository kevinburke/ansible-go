VERSION := $(shell go run github.com/kevinburke/bump_version/current_version@latest fastagent.go)

DEPLOY_DIR := $(HOME)/.ansible/fastagent
COLLECTION_TARBALL := tmp/kevinburke-fastagent-$(VERSION).tar.gz

.PHONY: all build deploy dev-install check-release-checkout collection release test clean FORCE

all: test build

build: tmp/fastagent-linux-amd64 tmp/fastagent-linux-arm64

# FORCE: without it, an existing binary is never rebuilt after a source
# change. go build's cache makes a no-op rebuild cheap.
tmp/fastagent-linux-amd64: FORCE
	mkdir -p tmp
	GOOS=linux GOARCH=amd64 go build -trimpath -o $@ ./cmd/fastagent

tmp/fastagent-linux-arm64: FORCE
	mkdir -p tmp
	GOOS=linux GOARCH=arm64 go build -trimpath -o $@ ./cmd/fastagent

# Copy built binaries into the canonical cache under ~/.ansible/fastagent/,
# for hosts that cannot download release binaries. They are installed
# under the release name, which every deploy on this machine uploads, so
# this only runs from a clean checkout of the release tag. To try local
# changes, use `make dev-install`.
deploy: check-release-checkout build
	mkdir -p $(DEPLOY_DIR)
	cp tmp/fastagent-linux-amd64 $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-amd64
	cp tmp/fastagent-linux-arm64 $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-arm64
	chmod +x $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-amd64
	chmod +x $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-arm64
	@echo "Deployed to $(DEPLOY_DIR):"
	@ls -l $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-*

check-release-checkout:
	@if [ -n "$$(git status --porcelain)" ] || \
		[ "$$(git describe --tags --exact-match 2>/dev/null)" != "v$(VERSION)" ]; then \
		echo "make deploy installs binaries as fastagent-$(VERSION)-linux-*, the" >&2; \
		echo "names every deploy from this machine uploads, so it only runs from a" >&2; \
		echo "clean checkout of tag v$(VERSION). To try local changes, run" >&2; \
		echo "make dev-install instead." >&2; \
		exit 1; \
	fi

# Build this checkout, uncommitted changes included, under a unique
# development version, and install it where only commands run through
# ~/.ansible/fastagent-dev/run use it. See scripts/dev-install.sh.
dev-install:
	scripts/dev-install.sh

FORCE:

# Build the Ansible collection tarball. Third-party users install this via
# `ansible-galaxy collection install ./kevinburke-fastagent-X.Y.Z.tar.gz`.
collection: $(COLLECTION_TARBALL)

# List every file shipped inside the collection tarball so editing any
# plugin source triggers a rebuild. A bare `plugins/` dependency matches
# only the directory's mtime (which changes on add/remove, not on content
# edits), so `make collection` would falsely report "nothing to be done"
# after an in-place edit and you'd install stale bytes.
COLLECTION_SOURCES := galaxy.yml meta/runtime.yml README.md \
	$(shell find plugins -type f \
		! -name '*_test.py' ! -name '*.pyc' ! -path '*/__pycache__/*')

$(COLLECTION_TARBALL): $(COLLECTION_SOURCES)
	mkdir -p tmp
	ansible-galaxy collection build --force --output-path tmp .
	@echo "Built collection: $(COLLECTION_TARBALL)"

# Full release: build linux binaries and the collection tarball together.
# Binaries are attached to a GitHub release (so third parties don't need Go),
# and the collection tarball is published to Galaxy.
release: build collection
	@echo "Release artifacts in tmp/:"
	@ls -l tmp/fastagent-linux-amd64 tmp/fastagent-linux-arm64 $(COLLECTION_TARBALL)

test:
	go test -trimpath -count=1 ./...
	cd plugins/module_utils && python3 -m unittest -v fastagent_client_test
	cd plugins/connection && python3 -m unittest -v fastagent_test
	python3 -m unittest discover -v -s tests -t . -p 'test_*.py'

clean:
	rm -rf tmp/
