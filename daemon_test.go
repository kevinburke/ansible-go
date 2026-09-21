package fastagent

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func daemonTestSocket(t *testing.T) string {
	t.Helper()
	// Honor TMPDIR: CI may make /tmp read-only. Avoid t.TempDir's test-name
	// prefix so these paths also fit macOS's shorter Unix socket path limit.
	dir, err := os.MkdirTemp("", "fastagent-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return filepath.Join(dir, "agent.sock")
}

func TestDaemonProcess(t *testing.T) {
	path := os.Getenv("FASTAGENT_TEST_SOCKET")
	if path == "" {
		return
	}
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	if err := RunDaemon(path, os.Getenv("FASTAGENT_TEST_ALLOW_USER"), 0, logger); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(0)
}

type daemonTestProcess struct {
	cmd    *exec.Cmd
	ready  chan string
	done   chan error
	stderr bytes.Buffer
}

func startTestDaemon(t *testing.T, path, user string) *daemonTestProcess {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	p := &daemonTestProcess{ready: make(chan string, 1), done: make(chan error, 1)}
	p.cmd = exec.Command(exe, "-test.run=^TestDaemonProcess$")
	p.cmd.Env = append(os.Environ(), "FASTAGENT_TEST_SOCKET="+path, "FASTAGENT_TEST_ALLOW_USER="+user)
	p.cmd.Stderr = &p.stderr
	out, err := p.cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := p.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() {
		scanner := bufio.NewScanner(out)
		if scanner.Scan() {
			p.ready <- scanner.Text()
		}
		io.Copy(io.Discard, out)
		p.done <- p.cmd.Wait()
		close(p.done)
	}()
	t.Cleanup(func() {
		p.cmd.Process.Kill()
		select {
		case <-p.done:
		case <-time.After(5 * time.Second):
			t.Error("daemon did not exit after cleanup")
		}
	})
	return p
}

func TestDaemonConcurrentStartup(t *testing.T) {
	path := daemonTestSocket(t)
	var processes []*daemonTestProcess
	for range 6 {
		processes = append(processes, startTestDaemon(t, path, ""))
	}
	for _, p := range processes {
		select {
		case ready := <-p.ready:
			if ready != path {
				t.Fatalf("readiness %q, want %q", ready, path)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("daemon startup did not complete")
		}
	}
	data, err := os.ReadFile(path + ".pid")
	if err != nil {
		t.Fatal(err)
	}
	pid, err := strconv.Atoi(string(data))
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range processes {
		if p.cmd.Process.Pid == pid {
			continue
		}
		select {
		case err := <-p.done:
			if err != nil {
				t.Fatalf("duplicate startup: %v: %s", err, &p.stderr)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("more than one daemon owns the socket")
		}
	}
	if !isDaemonRunning(path) {
		t.Fatal("winning daemon is unreachable")
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o700 {
		t.Fatalf("socket mode %o, want 700", info.Mode().Perm())
	}
}

func TestDaemonRejectsInvalidAllowUser(t *testing.T) {
	path := daemonTestSocket(t)
	p := startTestDaemon(t, path, "fastagent-no-such-user-934861")
	select {
	case err := <-p.done:
		if err == nil || !strings.Contains(p.stderr.String(), "allow-user") {
			t.Fatalf("expected allow-user failure, got %v: %s", err, &p.stderr)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("invalid allow-user did not fail startup")
	}
	if _, err := os.Lstat(path); !os.IsNotExist(err) {
		t.Fatalf("failed startup left socket: %v", err)
	}
}

func TestDaemonAllowUserSocketPermissions(t *testing.T) {
	u, err := user.Current()
	if err != nil {
		t.Fatal(err)
	}
	path := daemonTestSocket(t)
	p := startTestDaemon(t, path, u.Username)
	select {
	case <-p.ready:
	case <-time.After(5 * time.Second):
		t.Fatal("daemon did not start for current user")
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o770 {
		t.Fatalf("socket mode %o, want 770", info.Mode().Perm())
	}
	if !isDaemonRunning(path) {
		t.Fatal("allowed user cannot reach daemon")
	}
}

func TestDaemonPIDFileFailureIsFatal(t *testing.T) {
	path := daemonTestSocket(t)
	if err := os.Mkdir(path+".pid", 0o700); err != nil {
		t.Fatal(err)
	}
	p := startTestDaemon(t, path, "")
	select {
	case err := <-p.done:
		if err == nil || !strings.Contains(p.stderr.String(), "pid file") {
			t.Fatalf("expected PID-file failure: %v: %s", err, &p.stderr)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("PID-file failure did not fail startup")
	}
}

func TestDaemonRejectsInvalidHello(t *testing.T) {
	for _, response := range []string{
		`{"id":1}`, `{"id":2,"result":{"version":"test"}}`,
		`{"id":1,"result":null}`, `{"id":1,"result":{"version":""}}`,
		`{"id":1,"error":{"code":1,"message":"failed"}}`,
	} {
		t.Run(response, func(t *testing.T) {
			path := daemonTestSocket(t)
			listener, err := net.Listen("unix", path)
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			done := make(chan error, 1)
			go func() {
				conn, err := listener.Accept()
				if err != nil {
					done <- err
					return
				}
				defer conn.Close()
				conn.SetDeadline(time.Now().Add(3 * time.Second))
				var request Request
				if err := json.NewDecoder(conn).Decode(&request); err != nil {
					done <- err
					return
				}
				_, err = io.WriteString(conn, response+"\n")
				done <- err
			}()
			if isDaemonRunning(path) {
				t.Error("invalid Hello accepted")
			}
			if err := <-done; err != nil {
				t.Error(err)
			}
		})
	}
}

func TestDaemonPreservesNonSocketPath(t *testing.T) {
	path := daemonTestSocket(t)
	if err := os.WriteFile(path, []byte("keep me"), 0o600); err != nil {
		t.Fatal(err)
	}
	p := startTestDaemon(t, path, "")
	select {
	case err := <-p.done:
		if err == nil {
			t.Fatal("non-socket path was accepted")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("non-socket path did not fail startup")
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "keep me" {
		t.Fatalf("existing file changed: %q, %v", data, err)
	}
}

func TestDaemonProbeReadsCompleteResponse(t *testing.T) {
	path := daemonTestSocket(t)
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	done := make(chan error, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			done <- err
			return
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(3 * time.Second))
		var request Request
		if err := json.NewDecoder(conn).Decode(&request); err != nil {
			done <- err
			return
		}
		if _, err := io.WriteString(conn, `{"id":1,`); err != nil {
			done <- err
			return
		}
		// Force a partial first read; a single Read is not a JSON frame.
		time.Sleep(20 * time.Millisecond)
		_, err = io.WriteString(conn, `"result":{"version":"`+Version+`","capabilities":[]}}`+"\n")
		done <- err
	}()
	if !isDaemonRunning(path) {
		t.Error("fragmented Hello response rejected")
	}
	if err := <-done; err != nil {
		t.Error(err)
	}
}

func TestDaemonLockRejectsSymlink(t *testing.T) {
	path := daemonTestSocket(t)
	target := filepath.Join(filepath.Dir(path), "target")
	if err := os.WriteFile(target, []byte("keep me"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, path+".lock"); err != nil {
		t.Fatal(err)
	}
	p := startTestDaemon(t, path, "")
	select {
	case err := <-p.done:
		if err == nil {
			t.Fatal("symlink lock accepted")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("symlink lock did not fail startup")
	}
	data, err := os.ReadFile(target)
	if err != nil || string(data) != "keep me" {
		t.Fatalf("lock target changed: %q, %v", data, err)
	}
}

func TestDaemonReleasesLockOnShutdown(t *testing.T) {
	path := daemonTestSocket(t)
	p := startTestDaemon(t, path, "")
	select {
	case <-p.ready:
	case <-time.After(5 * time.Second):
		t.Fatal("daemon did not start")
	}
	if err := p.cmd.Process.Signal(unix.SIGTERM); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-p.done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("daemon did not stop")
	}
	next := startTestDaemon(t, path, "")
	select {
	case <-next.ready:
	case <-time.After(5 * time.Second):
		t.Fatal("daemon could not restart")
	}
}
