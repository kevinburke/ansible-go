VERSION := $(shell go run github.com/kevinburke/bump_version/current_version@latest fastagent.go)

DEPLOY_DIR := $(HOME)/.ansible/fastagent

.PHONY: all build deploy test clean

all: test build

build: tmp/fastagent-linux-amd64 tmp/fastagent-linux-arm64

tmp/fastagent-linux-amd64:
	mkdir -p tmp
	GOOS=linux GOARCH=amd64 go build -trimpath -o $@ ./cmd/fastagent

tmp/fastagent-linux-arm64:
	mkdir -p tmp
	GOOS=linux GOARCH=arm64 go build -trimpath -o $@ ./cmd/fastagent

deploy: build
	mkdir -p $(DEPLOY_DIR)
	cp tmp/fastagent-linux-amd64 $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-amd64
	cp tmp/fastagent-linux-arm64 $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-arm64
	chmod +x $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-amd64
	chmod +x $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-arm64
	@echo "Deployed to $(DEPLOY_DIR):"
	@ls -l $(DEPLOY_DIR)/fastagent-$(VERSION)-linux-*

test:
	go test -trimpath -count=1 ./...
	cd module_utils && python3 -m unittest fastagent_client_test -v

clean:
	rm -rf tmp/
