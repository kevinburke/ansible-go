VERSION := $(shell go run -trimpath ./cmd/fastagent --version 2>/dev/null | awk '{print $$2}')
ifeq ($(VERSION),)
VERSION := dev
endif

LDFLAGS :=

.PHONY: all build test clean

all: test build

build: tmp/fastagent-linux-amd64 tmp/fastagent-linux-arm64

tmp/fastagent-linux-amd64:
	mkdir -p tmp
	GOOS=linux GOARCH=amd64 go build -trimpath -o $@ ./cmd/fastagent

tmp/fastagent-linux-arm64:
	mkdir -p tmp
	GOOS=linux GOARCH=arm64 go build -trimpath -o $@ ./cmd/fastagent

test:
	go test -trimpath -count=1 ./...

clean:
	rm -rf tmp/
