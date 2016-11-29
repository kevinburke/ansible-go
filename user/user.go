package user

import (
	"context"
	"strings"
	"time"

	"github.com/kevinburke/ansible-go/core"
)

type userModule interface {
	Add(ctx context.Context, name string) error
}

type AddUser struct {
	// The encrypted password, as returned by crypt(3).
	// The default ("") disables the password.
	password string

	// The primary group for this user.
	group string

	// Puts the user in this list of groups.
	groups []string

	// If true, append Groups to the list of existing groups for the user.
	appendGroups bool

	// Create a system account. System users will be created with no aging
	// information in /etc/shadow, and their numeric identifiers are chosen in
	// the SYS_UID_MIN-SYS_UID_MAX range, defined in /etc/login.defs, instead
	// of UID_MIN-UID_MAX.
	system bool
	uid    string

	// Any text string. It is generally a short description of the login, and
	// is currently used as the field for the user's full name
	comment string

	// The name of the user's login shell. The default is to leave this field
	// blank, which causes the system to select the default login shell
	// specified by the SHELL variable in /etc/default/useradd, or an empty
	// string by default.
	shell string

	// The date on which the user account will be disabled. If zero or
	// unsupported, no expiry.
	expires time.Time

	// If true, create a home directory for the user.
	home bool
}

func (i *AddUser) Add(ctx context.Context, name string) error {
	args := []string{}
	if i.uid != "" {
		args = append(args, "--uid", i.uid)
	}
	if i.group != "" {
		args = append(args, "--gid", i.group)
	}
	if len(i.groups) > 0 {
		args = append(args, "--groups", strings.Join(i.groups, ","))
	}
	if i.comment != "" {
		args = append(args, "--comment", i.comment)
	}
	if i.shell != "" {
		args = append(args, "--shell", i.shell)
	}
	if !i.expires.IsZero() {
		args = append(args, "--expiredate", i.expires.Format("2006-01-02"))
	}
	if i.password != "" {
		args = append(args, "--password", i.password)
	}
	if i.home {
		args = append(args, "--create-home")
	} else {
		args = append(args, "-M")
	}
	if i.system {
		args = append(args, "--system")
	}
	args = append(args, name)
	return core.RunCommand(ctx, "useradd", args...)
}

func System(au *AddUser) error {
	au.system = true
	return nil
}

func Shell(shell string) func(au *AddUser) error {
	return func(au *AddUser) error {
		au.shell = shell
		return nil
	}
}

func HomeDirectory(au *AddUser) error {
	au.home = true
	return nil
}

func PrimaryGroup(grp string) func(au *AddUser) error {
	return func(au *AddUser) error {
		au.group = grp
		return nil
	}
}

func Group(grp string) func(au *AddUser) error {
	return func(au *AddUser) error {
		au.groups = append(au.groups, grp)
		return nil
	}
}

func Password(pass string) func(au *AddUser) error {
	return func(au *AddUser) error {
		au.password = pass
		return nil
	}
}

func Comment(comment string) func(au *AddUser) error {
	return func(au *AddUser) error {
		au.comment = comment
		return nil
	}
}

func UID(uid string) func(au *AddUser) error {
	return func(au *AddUser) error {
		au.uid = uid
		return nil
	}
}

func Expires(expires time.Time) func(au *AddUser) error {
	return func(au *AddUser) error {
		au.expires = expires
		return nil
	}
}

// AppendGroups will append secondary Groups to the list of existing groups.
func AppendGroups() func(au *AddUser) error {
	return func(au *AddUser) error {
		au.appendGroups = true
		return nil
	}
}

// AddUserCommand ensures that user with the given name exists with the given
// UserOpts.
func Add(ctx context.Context, name string, opts ...func(*AddUser) error) error {
	adduser := &AddUser{}
	for _, o := range opts {
		if err := o(adduser); err != nil {
			return err
		}
	}
	return adduser.Add(ctx, name)
}
