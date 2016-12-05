STATICCHECK := $(shell command -v staticcheck)

test: vet
	go test ./...

vet:
ifndef STATICCHECK
	go get -u honnef.co/go/staticcheck/cmd/staticcheck
endif
	go vet ./...
	staticcheck ./...
