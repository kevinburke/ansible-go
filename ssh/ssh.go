package ssh

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"io/ioutil"
	"os"
	"os/exec"
	"strings"
	"sync"

	yaml "gopkg.in/yaml.v2"
)

const debug = true
const debugSSH = true

type Host struct {
	Name string `yaml:"host"`
	User string `yaml:"user"`
}

func GetHostFromFile(filename string) (Host, error) {
	data, err := ioutil.ReadFile(filename)
	if err != nil {
		return Host{}, err
	}
	h := new(Host)
	err = yaml.Unmarshal(data, h)
	return *h, err
}

func DetectOSArch(ctx context.Context, host Host) (string, string, error) {
	buf := new(bytes.Buffer)
	err := RunCommandStdout(ctx, host, buf, "uname", "-sm")
	if err != nil {
		return "", "", err
	}
	str := strings.TrimSpace(buf.String())
	parts := strings.Split(str, " ")
	if len(parts) != 2 {
		return "", "", fmt.Errorf("Invalid uname response: %s", str)
	}
	var goos, goarch string
	switch strings.ToLower(parts[0]) {
	case "darwin":
		goos = "darwin"
	case "linux":
		goos = "linux"
	case "freebsd":
		goos = "freebsd"
	case "dragonfly":
		goos = "dragonfly"
	default:
		return "", "", fmt.Errorf("Unknown os: %s. Please report this", parts[0])
	}
	switch strings.ToLower(parts[1]) {
	case "x86_64":
		goarch = "amd64"
	case "i386":
		goarch = "386"
	case "armv7l", "armv6l":
		goarch = "arm"
	case "2097":
		goarch = "s390x"
	default:
		return "", "", fmt.Errorf("Unknown arch: %s. Please report this so we can fix it", parts[0])
	}
	return goos, goarch, nil
}

// PutFile will copy a file from the local filesystem to the remote host.
func PutFile(ctx context.Context, host Host, localfile string, remoteLocation string) error {
	bufOut := new(bytes.Buffer)
	bufErr := new(bytes.Buffer)
	var hostArg string
	if host.User == "" {
		hostArg = host.Name
	} else {
		hostArg = host.User + "@" + host.Name
	}
	err := scpCmdRunner.RunAll(ctx, host, nil, bufOut, bufErr, "", localfile, fmt.Sprintf("%s:%s", hostArg, remoteLocation))
	if err != nil {
		io.Copy(os.Stderr, bufOut)
		io.Copy(os.Stderr, bufErr)
	}
	return err
}

func RunCommand(ctx context.Context, host Host, name string, args ...string) error {
	return RunCommandStdin(ctx, host, nil, name, args...)
}

func RunCommandStdout(ctx context.Context, host Host, stdout io.Writer, name string, args ...string) error {
	buf := new(bytes.Buffer)
	err := sshCmdRunner.RunAll(ctx, host, nil, stdout, buf, name, args...)
	if err != nil {
		io.Copy(os.Stderr, buf)
	}
	return err
}

func RunCommandStdin(ctx context.Context, host Host, stdin io.Reader, name string, args ...string) error {
	bufOut := new(bytes.Buffer)
	bufErr := new(bytes.Buffer)
	err := sshCmdRunner.RunAll(ctx, host, stdin, bufOut, bufErr, name, args...)
	if err != nil {
		io.Copy(os.Stderr, bufOut)
		io.Copy(os.Stderr, bufErr)
	}
	return err
}

type cmdRunner struct {
	cmd string
}

var sshCmdRunner = cmdRunner{cmd: "ssh"}
var scpCmdRunner = cmdRunner{cmd: "scp"}

func (c cmdRunner) RunAll(ctx context.Context, host Host, stdin io.Reader, stdout, stderr io.Writer, name string, args ...string) error {
	fmt.Fprintf(os.Stderr, "RUN: %s %s\n", name, strings.Join(args, " "))
	var hostArg string
	if host.User == "" {
		hostArg = host.Name
	} else {
		hostArg = host.User + "@" + host.Name
	}
	var cmd *exec.Cmd
	if c.cmd == "ssh" {
		args0 := append([]string{"-C", "-o", "ControlMaster=no", hostArg, name}, args...)
		if debugSSH {
			args0 = append([]string{"-vvv"}, args0...)
		}
		cmd = exec.CommandContext(ctx, "ssh", args0...)
	} else if c.cmd == "scp" {
		// name ignored for scp
		cmd = exec.CommandContext(ctx, "scp", args...)
	}
	if debug {
		fmt.Fprintf(os.Stderr, "CMD: %s\n", strings.Join(cmd.Args, " "))
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
		wg.Done()
	}()
	go func() {
		io.Copy(stdout, stdoutPipe)
		wg.Done()
	}()

	cmd.Stdin = stdin
	if err := cmd.Start(); err != nil {
		return err
	}
	wg.Wait()
	return cmd.Wait()
}
