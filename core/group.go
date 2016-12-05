package core

import (
	"context"
	"os/user"
)

type groupModule interface {
	Add(context.Context, string, GroupOpts) error
	Exists(context.Context, string) (bool, error)
	Mod(context.Context, string, GroupOpts) error
}

// implementation taken from
// https://github.com/ansible/ansible-modules-core/blob/devel/system/group.py

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

func (i *defaultGroupImpl) Exists(ctx context.Context, name string) (bool, error) {
	_, err := lookupGroup(name)
	if err == nil {
		return true, nil
	}
	switch err.(type) {
	case user.UnknownGroupError:
		return false, nil
	default:
		return false, err
	}
}

// Mod modifies the group
func (i *defaultGroupImpl) Mod(ctx context.Context, name string, opts GroupOpts) error {
	grp, err := user.LookupGroup(name)
	if err != nil {
		return err
	}
	args := []string{}
	if opts.Gid != "" && grp.Gid != opts.Gid {
		args = append(args, "--gid", opts.Gid)
	}
	if len(args) == 0 {
		// Nothing to modify
		return nil
	}
	return RunCommand(ctx, "groupmod", args...)
}

var defaultGroup = &defaultGroupImpl{}

type GroupOpts struct {
	System bool
	Gid    string
}

// AddGroup ensures a group exists with the given name and options. AddGroup is
// not thread safe; it is the caller's responsibility to ensure only one
// instance of AddGroup is running on a host at a time.
func AddGroup(ctx context.Context, name string, opts GroupOpts) error {
	exists, err := defaultGroup.Exists(ctx, name)
	if err != nil {
		return err
	}
	if exists {
		return defaultGroup.Mod(ctx, name, opts)
	} else {
		return defaultGroup.Add(ctx, name, opts)
	}
}
