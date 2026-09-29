package fastagent

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

// This file implements WriteFile's Validate option: copy and template's
// `validate`. The behavior follows `ansible-doc ansible.builtin.copy` and
// what stock copy was observed to do on a test host (see
// tests/test_copy_validate_differential.py); no ansible-core source was
// used.

func (v *WriteValidate) check() error {
	if v.Placeholder == "" {
		return errors.New("validate: empty placeholder")
	}
	if len(v.Argv) == 0 {
		return errors.New("validate: empty argv")
	}
	if !slices.ContainsFunc(v.Argv, func(a string) bool { return strings.Contains(a, v.Placeholder) }) {
		// The action plugin only sends validate commands containing %s.
		return errors.New("validate: no argument contains the placeholder")
	}
	return nil
}

// runWriteValidate writes data to a private temporary file with the
// requested owner, group and mode, runs the Validate command on it, and
// removes it. It returns a non-nil ValidateFailure when the command could
// not be started or exited non-zero, and an error for anything that kept
// validation from being attempted.
//
// Stock copy validates the file it uploaded into Ansible's remote tmpdir,
// outside the destination directory, after applying owner/group/mode.
// Validating in a private directory keeps an unvalidated file out of the
// destination directory, where a service watching it could pick it up.
func runWriteValidate(p WriteFileParams, data []byte) (*ValidateFailure, error) {
	dir, err := os.MkdirTemp("", "fastagent-validate-")
	if err != nil {
		return nil, fmt.Errorf("validate: create temporary directory: %w", err)
	}
	defer os.RemoveAll(dir)
	// Stock names the file ".source" plus the destination's extension, so
	// a validator that looks at the extension still sees it.
	path := filepath.Join(dir, ".source"+splitextExt(filepath.Base(p.Dest)))
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return nil, fmt.Errorf("validate: write %s: %w", path, err)
	}
	if _, err := applyOwnershipAndMode(path, p.Owner, p.Group, p.Mode); err != nil {
		return nil, fmt.Errorf("validate: %w", err)
	}

	env := newStatEnv(p.Env)
	args := make([]string, len(p.Validate.Argv))
	for i, a := range p.Validate.Argv {
		// Stock substitutes the path first and expands afterwards.
		args[i] = expandModulePath(strings.ReplaceAll(a, p.Validate.Placeholder, path), env)
	}
	failure := &ValidateFailure{Path: path}
	bin, err := lookPathIn(args[0], env)
	if err != nil {
		failure.StartErrno, failure.StartError = errnoOf(err), err.Error()
		return failure, nil
	}
	cmd := &exec.Cmd{Path: bin, Args: args, Env: env.list()}
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err = cmd.Run()
	var exitErr *exec.ExitError
	switch {
	case err == nil:
		return nil, nil
	case errors.As(err, &exitErr):
		failure.RC = subprocessReturnCode(exitErr)
	default:
		failure.StartErrno, failure.StartError = errnoOf(err), err.Error()
	}
	failure.Stdout, failure.Stderr = stdout.String(), stderr.String()
	return failure, nil
}

// lookPathIn resolves name the way Python's subprocess does when given an
// env: a name containing a slash is used as is, otherwise each entry of the
// environment's PATH (not the agent's) is tried in order. exec.LookPath
// would search the agent's own PATH.
func lookPathIn(name string, env statEnv) (string, error) {
	if strings.Contains(name, "/") {
		return name, nil
	}
	path, ok := env["PATH"]
	if !ok {
		path = "/bin:/usr/bin"
	}
	var firstErr error
	for _, d := range filepath.SplitList(path) {
		if d == "" {
			d = "."
		}
		candidate := filepath.Join(d, name)
		err := unix.Access(candidate, unix.X_OK)
		if err == nil {
			if st, statErr := os.Stat(candidate); statErr == nil && !st.IsDir() {
				return candidate, nil
			}
			err = unix.EACCES
		}
		// Report the first error other than "not found", as execvpe does.
		if firstErr == nil && !errors.Is(err, unix.ENOENT) && !errors.Is(err, unix.ENOTDIR) {
			firstErr = err
		}
	}
	if firstErr == nil {
		firstErr = unix.ENOENT
	}
	return "", &os.PathError{Op: "exec", Path: name, Err: firstErr}
}

// subprocessReturnCode is the returncode Python's subprocess reports: the
// exit status, or -N for a process killed by signal N. ProcessState.Sys()
// is a syscall.WaitStatus; x/sys/unix's WaitStatus is a different type, so
// asserting to it never matches.
func subprocessReturnCode(exitErr *exec.ExitError) int {
	if ws, ok := exitErr.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		return -int(ws.Signal())
	}
	return exitErr.ExitCode()
}

// splitextExt returns what Python's os.path.splitext(name)[1] does for a
// basename: from the last dot on, unless only dots precede it (".hidden"
// has no extension; ".env.local" has ".local"; "x.tar.gz" has ".gz").
func splitextExt(name string) string {
	i := strings.LastIndex(name, ".")
	if i <= 0 || strings.TrimLeft(name[:i], ".") == "" {
		return ""
	}
	return name[i:]
}

func errnoOf(err error) int {
	if errno, ok := errors.AsType[unix.Errno](err); ok {
		return int(errno)
	}
	return int(unix.ENOENT)
}
