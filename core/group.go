package core

import "context"

type groupModule interface {
	Add(context.Context, string, GroupOpts) error
}

type defaultGroupImpl struct{}

func (i *defaultGroupImpl) Add(ctx context.Context, name string, opts GroupOpts) error {
	// Ansible calls getgrnam here to check whether the group exists or not
	// first. Can we get away with not calling that?
	args := []string{}
	if opts.Gid != "" {
		args = append(args, "-g", opts.Gid)
	} else if opts.System {
		args = append(args, "-r")
	}
	args = append(args, name)
	return RunCommand(ctx, "groupadd", args...)
}

var defaultGroup = &defaultGroupImpl{}

type GroupOpts struct {
	System bool
	Gid    string
}

// AddGroup adds a new group with the given name.
func AddGroup(ctx context.Context, name string, opts GroupOpts) error {
	return defaultGroup.Add(ctx, name, opts)
}
