VERSION := $(shell go run github.com/kevinburke/bump_version/current_version@latest fastagent.go)

DEPLOY_DIR := $(HOME)/.ansible/fastagent
COLLECTION_TARBALL := tmp/kevinburke-fastagent-$(VERSION).tar.gz

.PHONY: all build deploy collection release test clean

all: test build

build: tmp/fastagent-linux-amd64 tmp/fastagent-linux-arm64

tmp/fastagent-linux-amd64:
	mkdir -p tmp
	GOOS=linux GOARCH=amd64 go build -trimpath -o $@ ./cmd/fastagent

tmp/fastagent-linux-arm64:
	mkdir -p tmp
	GOOS=linux GOARCH=arm64 go build -trimpath -o $@ ./cmd/fastagent

# Copy built binaries into the canonical cache under ~/.ansible/fastagent/.
# Useful for local development — the connection plugin looks here first.
deploy: build
	mkdir -p $(DEPLOY_DIR)
	cp tmp/fastagent-linux-amd64 $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-amd64
	cp tmp/fastagent-linux-arm64 $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-arm64
	chmod +x $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-amd64
	chmod +x $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-arm64
	@echo "Deployed to $(DEPLOY_DIR):"
	@ls -l $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-*

# Build the Ansible collection tarball. Third-party users install this via
# `ansible-galaxy collection install ./kevinburke-fastagent-X.Y.Z.tar.gz`.
collection: $(COLLECTION_TARBALL)

$(COLLECTION_TARBALL): galaxy.yml plugins
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
	python3 -m unittest -v \
		plugins.connection.fastagent_test \
		plugins.module_utils.fastagent_client_test \
		tests.test_collection_layout \
		tests.test_command_action \
		tests.test_file_action

clean:
	rm -rf tmp/
