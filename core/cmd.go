package core

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
)

func RunCommand(ctx context.Context, name string, args ...string) error {
	return RunCommandStdin(ctx, nil, name, args...)
}

func RunCommandStdin(ctx context.Context, stdin io.Reader, name string, args ...string) error {
	bufOut := new(bytes.Buffer)
	bufErr := new(bytes.Buffer)
	err := RunAll(ctx, stdin, bufOut, bufErr, "", name, args...)
	if err != nil {
		io.Copy(os.Stderr, bufOut)
		io.Copy(os.Stderr, bufErr)
	}
	return err
}

func RunAll(ctx context.Context, stdin io.Reader, stdout, stderr io.Writer, dir string, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdin = stdin
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	cmd.Dir = dir
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		return err
	}
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		io.Copy(stderr, stderrPipe)
		wg.Done()
	}()
	go func() {
		io.Copy(stdout, stdoutPipe)
		wg.Done()
	}()

	cmd.Stdin = stdin
	fmt.Fprintf(os.Stderr, "RUN: %s\n", strings.Join(cmd.Args, " "))
	if err := cmd.Start(); err != nil {
		return err
	}
	wg.Wait()
	return cmd.Wait()
}
