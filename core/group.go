package core

import (
	"context"

	"github.com/kevinburke/ansible-go/ssh"
)

type groupModule interface {
	Add(context.Context, ssh.Host, string, GroupOpts) error
}

type defaultGroupImpl struct{}

func (i *defaultGroupImpl) Add(ctx context.Context, host ssh.Host, name string, opts GroupOpts) error {
	// Ansible calls getgrnam here to check whether the group exists or not
	// first. Can we get away with not calling that?
	args := []string{}
	if opts.Gid != "" {
		args = append(args, "-g", opts.Gid)
	} else if opts.System {
		args = append(args, "-r")
	}
	args = append(args, name)
	return ssh.RunCommand(ctx, host, "groupadd", args...)
}

var defaultGroup = &defaultGroupImpl{}

type GroupOpts struct {
	System bool
	Gid    string
}

// AddGroup adds a new group with the given name.
func AddGroup(ctx context.Context, host ssh.Host, name string, opts GroupOpts) error {
	return defaultGroup.Add(ctx, host, name, opts)
}