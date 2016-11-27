package core

import (
	"context"

	"github.com/kevinburke/ansible-go/ssh"
)

type DirOpts struct {
	// Recursively set the file attributes
	Recurse bool
}

func CreateDirectory(ctx context.Context, host ssh.Host, path string, opts DirOpts) {

}
