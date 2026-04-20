package fastagent

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"
)

func (s *Server) handleExec(params json.RawMessage) (any, error) {
	var p ExecParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal ExecParams: %w", err)
	}

	// Handle creates/removes short-circuit.
	if p.Creates != "" {
		if _, err := os.Stat(p.Creates); err == nil {
			return ExecResult{
				RC:      0,
				Skipped: true,
				Msg:     fmt.Sprintf("skipped, %s exists", p.Creates),
			}, nil
		}
	}
	if p.Removes != "" {
		if _, err := os.Stat(p.Removes); os.IsNotExist(err) {
			return ExecResult{
				RC:      0,
				Skipped: true,
				Msg:     fmt.Sprintf("skipped, %s does not exist", p.Removes),
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
		parts := strings.Fields(p.CmdString)
		if len(parts) == 0 {
			return nil, fmt.Errorf("empty command string")
		}
		finalArgv = parts
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
