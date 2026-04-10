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

	var cmd *exec.Cmd
	if p.UseShell {
		shellCmd := p.CmdString
		if shellCmd == "" && len(p.Argv) > 0 {
			shellCmd = strings.Join(p.Argv, " ")
		}
		cmd = exec.CommandContext(ctx, "/bin/sh", "-c", shellCmd)
	} else if len(p.Argv) > 0 {
		cmd = exec.CommandContext(ctx, p.Argv[0], p.Argv[1:]...)
	} else if p.CmdString != "" {
		parts := strings.Fields(p.CmdString)
		if len(parts) == 0 {
			return nil, fmt.Errorf("empty command string")
		}
		cmd = exec.CommandContext(ctx, parts[0], parts[1:]...)
	} else {
		return nil, fmt.Errorf("no command specified: set argv or cmd_string")
	}

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
