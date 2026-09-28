package fastagent

import (
	"bytes"
	"errors"
	"maps"
	"os"
	"os/exec"
	"os/user"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

// This file holds the pieces of ansible.builtin.stat that must run on the
// remote host: path expansion, symlink resolution, and running `file` and
// `lsattr`. Each function is a port of the Python it replaces, named in its
// doc comment; keep them in step with ansible-core and CPython at tip.
// Parsing of `file` and `lsattr` output stays in the action plugin, which
// runs the same Python expressions stock does.

// sIMODE is the mask Python's stat.S_IMODE applies: the permission bits
// plus setuid, setgid and sticky, without the file type.
const sIMODE = unix.S_ISUID | unix.S_ISGID | unix.S_ISVTX |
	unix.S_IRWXU | unix.S_IRWXG | unix.S_IRWXO

// statEnv is the environment ansible.builtin.stat would see: the agent's
// own environment plus the task's `environment:` keyword, which Ansible
// prepends to the module command.
type statEnv map[string]string

func newStatEnv(overlay map[string]string) statEnv {
	env := statEnv{}
	for _, kv := range os.Environ() {
		if k, v, ok := strings.Cut(kv, "="); ok {
			env[k] = v
		}
	}
	maps.Copy(env, overlay)
	return env
}

func (e statEnv) list() []string {
	out := make([]string, 0, len(e))
	for k, v := range e {
		out = append(out, k+"="+v)
	}
	return out
}

// pyVarPattern is posixpath._varpattern, compiled with re.ASCII so \w is
// [A-Za-z0-9_].
var pyVarPattern = regexp.MustCompile(`\$([A-Za-z0-9_]+|\{[^}]*\}?)`)

// pyExpandVars ports posixpath.expandvars: replace $name and ${name} with
// their values, leaving unknown names and an unterminated ${ unchanged.
// Replacement text is not rescanned.
func pyExpandVars(path string, env statEnv) string {
	if !strings.Contains(path, "$") {
		return path
	}
	return pyVarPattern.ReplaceAllStringFunc(path, func(m string) string {
		name := m[1:]
		if strings.HasPrefix(name, "{") {
			if !strings.HasSuffix(name, "}") {
				return m
			}
			name = name[1 : len(name)-1]
		}
		if v, ok := env[name]; ok {
			return v
		}
		return m
	})
}

// pyExpandUser ports posixpath.expanduser: ~ uses $HOME, falling back to
// the passwd entry for the current uid; ~name uses that user's passwd
// entry. Unknown users leave the path unchanged.
func pyExpandUser(path string, env statEnv) string {
	if !strings.HasPrefix(path, "~") {
		return path
	}
	i := strings.Index(path[1:], "/")
	if i < 0 {
		i = len(path)
	} else {
		i++
	}
	var home string
	if i == 1 {
		if h, ok := env["HOME"]; ok {
			home = h
		} else {
			// pwd.getpwuid(os.getuid()); user.Current would fall back
			// to environment variables instead of failing.
			u, err := user.LookupId(strconv.Itoa(os.Getuid()))
			if err != nil {
				return path
			}
			home = u.HomeDir
		}
	} else {
		u, err := user.Lookup(path[1:i])
		if err != nil {
			return path
		}
		home = u.HomeDir
	}
	home = strings.TrimRight(home, "/")
	if out := home + path[i:]; out != "" {
		return out
	}
	return "/"
}

// expandModulePath applies Ansible's type='path' conversion
// (check_type_path): expanduser(expandvars(value)).
func expandModulePath(path string, env statEnv) string {
	return pyExpandUser(pyExpandVars(path, env), env)
}

// pyRealpath ports posixpath.realpath(path, strict=False) from Python
// 3.14. Unlike filepath.EvalSymlinks it never fails: a missing component
// or a dangling link resolves as far as it can and keeps the rest as
// written. Symlink loops follow 3.14; Python 3.12 and older return early
// with the unresolved remainder, which only differs for looping links.
func pyRealpath(filename string) string {
	const sep = "/"
	parts := strings.Split(filename, sep)
	// rest is a stack; a nil entry marks that the entry below it is a
	// symlink whose target has now been fully resolved.
	rest := make([]*string, 0, len(parts))
	for i := len(parts) - 1; i >= 0; i-- {
		rest = append(rest, &parts[i])
	}
	partCount := len(parts)
	path := sep
	if !strings.HasPrefix(filename, sep) {
		wd, err := os.Getwd()
		if err != nil {
			wd = sep
		}
		path = wd
	}
	seen := map[string]*string{}
	pop := func() *string {
		v := rest[len(rest)-1]
		rest = rest[:len(rest)-1]
		return v
	}
	for partCount > 0 {
		namep := pop()
		if namep == nil {
			resolved := path
			seen[*pop()] = &resolved
			continue
		}
		name := *namep
		partCount--
		if name == "" || name == "." {
			continue
		}
		if name == ".." {
			path = path[:strings.LastIndex(path, sep)]
			if path == "" {
				path = sep
			}
			continue
		}
		newpath := path + sep + name
		if path == sep {
			newpath = path + name
		}
		var st unix.Stat_t
		if err := unix.Lstat(newpath, &st); err != nil {
			path = newpath
			continue
		}
		if st.Mode&unix.S_IFMT != unix.S_IFLNK {
			path = newpath
			continue
		}
		if cached, ok := seen[newpath]; ok {
			if cached != nil {
				path = *cached
				continue
			}
			// Unresolved and seen again: a loop.
			path = newpath
			continue
		}
		target, err := os.Readlink(newpath)
		if err != nil {
			path = newpath
			continue
		}
		if strings.HasPrefix(target, sep) {
			path = sep
		}
		seen[newpath] = nil
		marker := newpath
		rest = append(rest, &marker, nil)
		tparts := strings.Split(target, sep)
		for i := len(tparts) - 1; i >= 0; i-- {
			rest = append(rest, &tparts[i])
		}
		partCount += len(tparts)
	}
	return path
}

// getBinPath ports ansible.module_utils.common.process.get_bin_path: search
// PATH, then /sbin, /usr/sbin and /usr/local/sbin, for an existing
// non-directory with any execute bit set. It returns "" when not found.
func getBinPath(arg string, env statEnv) string {
	paths := strings.Split(env["PATH"], string(os.PathListSeparator))
	for _, p := range []string{"/sbin", "/usr/sbin", "/usr/local/sbin"} {
		if !containsString(paths, p) && pathExists(p) {
			paths = append(paths, p)
		}
	}
	for _, d := range paths {
		if d == "" {
			continue
		}
		// os.path.join does not clean the path; keep Python's spelling.
		candidate := d + "/" + arg
		if strings.HasSuffix(d, "/") {
			candidate = d + arg
		}
		st, err := os.Stat(candidate)
		if err != nil || st.IsDir() {
			continue
		}
		// is_executable: (S_IXUSR | S_IXGRP | S_IXOTH) & st_mode.
		if uint32(st.Mode().Perm())&(unix.S_IXUSR|unix.S_IXGRP|unix.S_IXOTH) != 0 {
			return candidate
		}
	}
	return ""
}

func containsString(list []string, s string) bool {
	return slices.Contains(list, s)
}

func pathExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// runModuleCommand runs argv the way AnsibleModule.run_command does for a
// list: each argument gets expanduser(expandvars(...)) first
// (expand_user_and_vars defaults to True), and the command inherits the
// module's environment. A command that cannot be started is an error:
// run_command calls fail_json, which stat's `except Exception` does not
// catch, so the stock module fails.
func runModuleCommand(argv []string, env statEnv) (*StatCommandOutput, error) {
	args := make([]string, len(argv))
	for i, a := range argv {
		args[i] = pyExpandUser(pyExpandVars(a, env), env)
	}
	cmd := exec.Command(args[0], args[1:]...)
	cmd.Env = env.list()
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	err := cmd.Run()
	var exitErr *exec.ExitError
	switch {
	case err == nil:
		return &StatCommandOutput{RC: 0, Stdout: stdout.String()}, nil
	case errors.As(err, &exitErr):
		rc := exitErr.ExitCode()
		if rc < 0 {
			// Killed by a signal; subprocess reports -signum.
			if ws, ok := exitErr.Sys().(unix.WaitStatus); ok && ws.Signaled() {
				rc = -int(ws.Signal())
			}
		}
		return &StatCommandOutput{RC: rc, Stdout: stdout.String()}, nil
	default:
		return nil, err
	}
}

// libcStrerror returns strerror(3) text for errno, as Python's
// OSError.strerror does. x/sys/unix generates its messages from the C
// library's and lowercases the first letter when the second is lowercase
// (mkerrors.sh), so undo that.
func libcStrerror(errno unix.Errno) string {
	msg := errno.Error()
	if len(msg) >= 2 && 'a' <= msg[0] && msg[0] <= 'z' && 'a' <= msg[1] && msg[1] <= 'z' {
		return string(msg[0]-'a'+'A') + msg[1:]
	}
	return msg
}
