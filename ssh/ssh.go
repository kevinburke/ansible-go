package ssh

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

const debug = true

type Host struct {
	Name string
	User string
}

func RunCommand(ctx context.Context, host Host, name string, args ...string) error {
	return RunCommandStdin(ctx, host, nil, name, args...)
}

func RunCommandStdout(ctx context.Context, host Host, stdout io.Writer, name string, args ...string) error {
	buf := new(bytes.Buffer)
	err := RunAll(ctx, host, nil, stdout, buf, name, args...)
	if err != nil {
		io.Copy(os.Stderr, buf)
	}
	return err
}

func RunCommandStdin(ctx context.Context, host Host, stdin io.Reader, name string, args ...string) error {
	bufOut := new(bytes.Buffer)
	bufErr := new(bytes.Buffer)
	err := RunAll(ctx, host, stdin, bufOut, bufErr, name, args...)
	if err != nil {
		io.Copy(os.Stderr, bufOut)
		io.Copy(os.Stderr, bufErr)
	}
	return err
}

func RunAll(ctx context.Context, host Host, stdin io.Reader, stdout, stderr io.Writer, name string, args ...string) error {
	var hostArg string
	if host.User == "" {
		hostArg = host.Name
	} else {
		hostArg = host.User + "@" + host.Name
	}
	args0 := append([]string{hostArg, name}, args...)
	cmd := exec.CommandContext(ctx, "ssh", args0...)
	if debug {
		fmt.Printf("RUN: %s\n", strings.Join(cmd.Args, " "))
	}
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
		fmt.Println("stderr done")
		wg.Done()
	}()
	go func() {
		io.Copy(stdout, stdoutPipe)
		fmt.Println("stdout done")
		wg.Done()
	}()

	cmd.Stdin = stdin
	if err := cmd.Start(); err != nil {
		return err
	}
	fmt.Println("wg wait")
	wg.Wait()
	fmt.Println("cmd wait")
	return cmd.Wait()
}
