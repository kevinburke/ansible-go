package core

import (
	"context"
	"strings"
	"time"

	"github.com/kevinburke/ansible-go/ssh"
)

type userModule interface {
	Add(ctx context.Context, host ssh.Host, name string, opts UserOpts) error
}

type defaultUserImpl struct{}

func (i *defaultUserImpl) Add(ctx context.Context, host ssh.Host, name string, opts UserOpts) error {
	args := []string{}
	if opts.Uid != "" {
		args = append(args, "--uid", opts.Uid)
	}
	if opts.Group != "" {
		args = append(args, "--gid", opts.Group)
	}
	if len(opts.Groups) > 0 {
		args = append(args, "--groups", strings.Join(opts.Groups, ","))
	}
	if opts.Comment != "" {
		args = append(args, "--comment", opts.Comment)
	}
	if opts.Shell != "" {
		args = append(args, "--shell", opts.Shell)
	}
	if !opts.Expires.IsZero() {
		args = append(args, "--expiredate", opts.Expires.Format("2006-01-02"))
	}
	if opts.Password != "" {
		args = append(args, "--password", opts.Password)
	}
	if opts.NoHomeDirectory {
		args = append(args, "-M")
	} else {
		args = append(args, "--create-home")
	}
	if opts.System {
		args = append(args, "--system")
	}
	args = append(args, name)
	return ssh.RunCommand(ctx, host, "useradd", args...)
}

type UserOpts struct {
	// The encrypted password, as returned by crypt(3).
	// The default ("") disables the password.
	Password string
	// The primary group for this user.
	Group string
	// Puts the user in this list of groups.
	Groups []string
	// If true, append Groups to the list of existing groups for the user.
	AppendGroups bool
	// Create a system account. System users will be created with no aging
	// information in /etc/shadow, and their numeric identifiers are chosen in
	// the SYS_UID_MIN-SYS_UID_MAX range, defined in /etc/login.defs, instead
	// of UID_MIN-UID_MAX.
	System bool
	Uid    string
	// Any text string. It is generally a short description of the login, and
	// is currently used as the field for the user's full name
	Comment string
	// The name of the user's login shell. The default is to leave this field
	// blank, which causes the system to select the default login shell
	// specified by the SHELL variable in /etc/default/useradd, or an empty
	// string by default.
	Shell string
	// The date on which the user account will be disabled. If zero or
	// unsupported, no expiry.
	Expires time.Time

	// If true, do not create a home directory for the user.
	NoHomeDirectory bool
}

var defaultUser = &defaultUserImpl{}

// AddUser ensures that user with the given name exists with the given
// UserOpts.
func AddUser(ctx context.Context, host ssh.Host, name string, opts UserOpts) error {
	return defaultUser.Add(ctx, host, name, opts)
}
