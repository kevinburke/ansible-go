package fastagent

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strings"
	"time"
)

// becomeUserCwd picks a working directory for an Exec when the caller
// requested BecomeUser but no explicit Cwd. The daemon's own cwd is
// often a directory the become user can't traverse (e.g. the deploy
// user's home at mode 0700) — inheriting it surfaces as EACCES in any
// child that fork/execs or walks the filesystem, even though the
// binary being run is itself readable. Prefer the target user's home
// directory; fall back to "/" which is always traversable.
func becomeUserCwd(username string) string {
	if u, err := user.Lookup(username); err == nil && u.HomeDir != "" {
		if fi, statErr := os.Stat(u.HomeDir); statErr == nil && fi.IsDir() {
			return u.HomeDir
		}
	}
	return "/"
}

func commandPathMatches(pattern, cwd string) (bool, error) {
	if pattern == "" {
		return false, nil
	}
	if cwd != "" && !filepath.IsAbs(pattern) {
		pattern = filepath.Join(cwd, pattern)
	}
	matches, err := filepath.Glob(pattern)
	if err != nil {
		return false, fmt.Errorf("bad path pattern %q: %w", pattern, err)
	}
	return len(matches) > 0, nil
}

func (s *Server) handleExec(params json.RawMessage) (any, error) {
	var p ExecParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal ExecParams: %w", err)
	}
	if p.Cwd != "" {
		fi, err := os.Stat(p.Cwd)
		if err != nil {
			return nil, fmt.Errorf("exec: chdir %q: %w", p.Cwd, err)
		}
		if !fi.IsDir() {
			return nil, fmt.Errorf("exec: chdir %q: not a directory", p.Cwd)
		}
	}

	// Handle creates/removes short-circuit.
	if p.Creates != "" {
		if p.BecomeUser != "" {
			return nil, fmt.Errorf("exec: creates with become_user is not implemented; use the builtin command module")
		}
		ok, err := commandPathMatches(p.Creates, p.Cwd)
		if err != nil {
			return nil, err
		}
		if ok {
			return ExecResult{
				RC:      0,
				Stdout:  fmt.Sprintf("skipped, since %s exists", p.Creates),
				Skipped: true,
				Msg:     fmt.Sprintf("Did not run command since '%s' exists", p.Creates),
			}, nil
		}
	}
	if p.Removes != "" {
		if p.BecomeUser != "" {
			return nil, fmt.Errorf("exec: removes with become_user is not implemented; use the builtin command module")
		}
		ok, err := commandPathMatches(p.Removes, p.Cwd)
		if err != nil {
			return nil, err
		}
		if !ok {
			return ExecResult{
				RC:      0,
				Stdout:  fmt.Sprintf("skipped, since %s does not exist", p.Removes),
				Skipped: true,
				Msg:     fmt.Sprintf("Did not run command since '%s' does not exist", p.Removes),
			}, nil
		}
	}

	ctx := context.Background()
	if p.TimeoutSeconds > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(p.TimeoutSeconds)*time.Second)
		defer cancel()
	}

	// Build the final argv. This mirrors roughly what ansible's classic
	// SSH path does: resolve the command (either as an argv or via
	// `/bin/sh -c` when use_shell is set), then wrap it with
	// `sudo --user <user>` when a become_user is requested. The
	// difference is that ansible builds the wrapped command on the
	// controller and ships it over SSH, whereas we build it here on the
	// target after receiving the Exec RPC.
	//
	// Resolve UseShell/Argv/CmdString first, then (if BecomeUser is
	// set) wrap the result with sudo so the invocation runs as that
	// user. The flags are:
	//   --set-home        set HOME to the target user's home directory,
	//                     matching ansible's classic become path.
	//   --non-interactive fail rather than prompt for a password. A
	//                     prompt would deadlock the RPC; the caller is
	//                     expected to ensure the agent has passwordless
	//                     root-level sudo (true whenever ansible
	//                     invoked us with `become: true`).
	//   --user <user>     run as <user>.
	//   --                end of sudo options; everything after is the
	//                     command argv, so a command that starts with
	//                     `-` isn't mis-parsed as a sudo flag.
	var finalArgv []string
	switch {
	case p.UseShell:
		shellCmd := p.CmdString
		if shellCmd == "" && len(p.Argv) > 0 {
			shellCmd = strings.Join(p.Argv, " ")
		}
		finalArgv = []string{"/bin/sh", "-c", shellCmd}
	case len(p.Argv) > 0:
		finalArgv = p.Argv
	case p.CmdString != "":
		return nil, fmt.Errorf("exec: cmd_string without use_shell is not supported; send argv")
	default:
		return nil, fmt.Errorf("no command specified: set argv or cmd_string")
	}
	if p.BecomeUser != "" {
		finalArgv = append(
			[]string{
				"sudo",
				"--set-home",
				"--non-interactive",
				"--user", p.BecomeUser,
				"--",
			},
			finalArgv...,
		)
	}
	cmd := exec.CommandContext(ctx, finalArgv[0], finalArgv[1:]...)

	if p.Cwd != "" {
		cmd.Dir = p.Cwd
	} else if p.BecomeUser != "" {
		cmd.Dir = becomeUserCwd(p.BecomeUser)
	}
	if len(p.Env) > 0 {
		cmd.Env = os.Environ()
		for k, v := range p.Env {
			cmd.Env = append(cmd.Env, k+"="+v)
		}
	}
	if p.Stdin != "" {
		stdin := p.Stdin
		// Default stdin_add_newline to true (matches Ansible).
		if (p.StdinAddNewline == nil || *p.StdinAddNewline) && !strings.HasSuffix(stdin, "\n") {
			stdin += "\n"
		}
		cmd.Stdin = strings.NewReader(stdin)
	}

	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	rc := 0
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			rc = exitErr.ExitCode()
		} else {
			return nil, fmt.Errorf("exec: %w", err)
		}
	}

	stdoutStr := stdout.String()
	stderrStr := stderr.String()

	// Default strip_empty_ends to true (matches Ansible).
	if p.StripEmptyEnds == nil || *p.StripEmptyEnds {
		stdoutStr = strings.TrimRight(stdoutStr, "\r\n")
		stderrStr = strings.TrimRight(stderrStr, "\r\n")
	}

	return ExecResult{
		RC:      rc,
		Stdout:  stdoutStr,
		Stderr:  stderrStr,
		Changed: true,
	}, nil
}
