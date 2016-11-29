package core

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
)

func RunCommand(ctx context.Context, name string, args ...string) error {
	return RunCommandStdin(ctx, nil, name, args...)
}

func RunCommandStdin(ctx context.Context, stdin io.Reader, name string, args ...string) error {
	bufOut := new(bytes.Buffer)
	bufErr := new(bytes.Buffer)
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdin = stdin
	cmd.Stdout = bufOut
	cmd.Stderr = bufErr
	fmt.Fprintf(os.Stderr, "RUN: %s\n", strings.Join(cmd.Args, " "))
	err := cmd.Run()
	if err != nil {
		io.Copy(os.Stderr, bufOut)
		io.Copy(os.Stderr, bufErr)
	}
	return err
}
