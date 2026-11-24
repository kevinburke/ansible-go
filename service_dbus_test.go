package fastagent

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/godbus/dbus/v5"
)

// The integration tests use a private bus with a fake systemd API. They exercise
// authentication, signal ordering and job failures without changing host units.
type testSystemd struct {
	conn                       *dbus.Conn
	mu                         sync.Mutex
	active, fileState, outcome string
	hold                       bool
	job                        uint32
	started                    chan dbus.ObjectPath
	calls                      []string
	stateError                 bool
}

func privateSystemd(t *testing.T) (string, *testSystemd) {
	t.Helper()
	binary, err := exec.LookPath("dbus-daemon")
	if err != nil {
		t.Skip("dbus-daemon is needed for private-bus integration tests")
	}
	// The daemon defaults to /tmp regardless of TMPDIR. Use the existing short
	// socket helper so CI's writable temporary directory is honored.
	addressArg := "unix:path=" + dbus.EscapeBusAddressValue(daemonTestSocket(t))
	cmd := exec.Command(binary, "--session", "--nofork", "--nopidfile", "--print-address=1", "--address="+addressArg)
	out, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	address, err := bufio.NewReader(out).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	address = strings.TrimSpace(address)
	conn, err := dbus.Connect(address)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	f := &testSystemd{conn: conn, active: "inactive", fileState: "disabled", outcome: "done", started: make(chan dbus.ObjectPath, 4)}
	if err := conn.Export(f, systemdPath, systemdManager); err != nil {
		t.Fatal(err)
	}
	if err := conn.Export(f, "/org/freedesktop/systemd1/unit/demo_2eservice", "org.freedesktop.DBus.Properties"); err != nil {
		t.Fatal(err)
	}
	reply, err := conn.RequestName(systemdDestination, dbus.NameFlagDoNotQueue)
	if err != nil || reply != dbus.RequestNameReplyPrimaryOwner {
		t.Fatalf("claim name: %v %v", reply, err)
	}
	return address, f
}

func (f *testSystemd) Subscribe() *dbus.Error { return nil }
func (f *testSystemd) LoadUnit(name string) (dbus.ObjectPath, *dbus.Error) {
	if name != "demo.service" {
		return "", dbus.NewError(systemdDestination+".NoSuchUnit", []any{"missing"})
	}
	return "/org/freedesktop/systemd1/unit/demo_2eservice", nil
}
func (f *testSystemd) Get(iface, property string) (dbus.Variant, *dbus.Error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.stateError {
		return dbus.Variant{}, dbus.NewError("org.freedesktop.DBus.Error.AccessDenied", []any{"denied"})
	}
	return dbus.MakeVariant(f.active), nil
}
func (f *testSystemd) GetUnitFileState(name string) (string, *dbus.Error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.fileState, nil
}
func (f *testSystemd) start(name, action string) (dbus.ObjectPath, *dbus.Error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.job++
	path := dbus.ObjectPath(fmt.Sprintf("/org/freedesktop/systemd1/job/%d", f.job))
	f.calls = append(f.calls, action)
	if !f.hold {
		// Emit before replying to the method: a fast job must not be missed.
		if err := f.conn.Emit(systemdPath, systemdManager+".JobRemoved", f.job, path, name, f.outcome); err != nil {
			return "", dbus.MakeFailedError(err)
		}
	}
	f.started <- path
	return path, nil
}
func (f *testSystemd) StartUnit(name, mode string) (dbus.ObjectPath, *dbus.Error) {
	return f.start(name, "start")
}
func (f *testSystemd) StopUnit(name, mode string) (dbus.ObjectPath, *dbus.Error) {
	return f.start(name, "stop")
}
func (f *testSystemd) RestartUnit(name, mode string) (dbus.ObjectPath, *dbus.Error) {
	return f.start(name, "restart")
}
func (f *testSystemd) ReloadUnit(name, mode string) (dbus.ObjectPath, *dbus.Error) {
	return f.start(name, "reload-unit")
}
func (f *testSystemd) Reload() *dbus.Error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "daemon-reload")
	return nil
}

type testUnitFileChange struct{ Type, File, Target string }

func (f *testSystemd) EnableUnitFiles(names []string, runtime, force bool) (bool, []testUnitFileChange, *dbus.Error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "enable")
	f.fileState = "enabled"
	return true, []testUnitFileChange{{"symlink", "/etc/systemd/system/demo.service", "/usr/lib/systemd/system/demo.service"}}, nil
}
func (f *testSystemd) DisableUnitFiles(names []string, runtime bool) ([]testUnitFileChange, *dbus.Error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "disable")
	f.fileState = "disabled"
	return nil, nil
}

func privateSystemdClient(t *testing.T, address string) *systemdBus {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
	defer cancel()
	b, err := newSystemdBus(ctx, func(lifetime context.Context) (*dbus.Conn, error) {
		return dbus.Connect(address, dbus.WithContext(lifetime))
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(b.Close)
	return b
}

func TestDBusJobResultsAndEarlyCompletion(t *testing.T) {
	address, f := privateSystemd(t)
	b := privateSystemdClient(t, address)
	for _, outcome := range []string{"done", "skipped", "failed", "dependency", "timeout", "canceled", "unknown"} {
		t.Run(outcome, func(t *testing.T) {
			f.mu.Lock()
			f.outcome = outcome
			f.mu.Unlock()
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			err := b.SetState(ctx, "demo.service", "restart", false)
			wantError := outcome != "done" && outcome != "skipped"
			if (err != nil) != wantError {
				t.Fatalf("outcome=%s err=%v", outcome, err)
			}
			if wantError && !strings.Contains(err.Error(), outcome) {
				t.Fatal(err)
			}
			<-f.started
		})
	}
}

func TestDBusWaitsForJobAndPreservesNoBlock(t *testing.T) {
	address, f := privateSystemd(t)
	f.mu.Lock()
	f.hold = true
	f.mu.Unlock()
	b := privateSystemdClient(t, address)
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- b.SetState(ctx, "demo.service", "start", false) }()
	path := <-f.started
	select {
	case err := <-done:
		t.Fatalf("returned before completion: %v", err)
	case <-time.After(30 * time.Millisecond):
	}
	if err := f.conn.Emit(systemdPath, systemdManager+".JobRemoved", uint32(1), path, "demo.service", "done"); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if err := b.SetState(ctx, "demo.service", "stop", true); err != nil {
		t.Fatal(err)
	}
}

func TestDBusDisconnectWhileWaiting(t *testing.T) {
	address, f := privateSystemd(t)
	f.mu.Lock()
	f.hold = true
	f.mu.Unlock()
	b := privateSystemdClient(t, address)
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- b.SetState(ctx, "demo.service", "restart", false) }()
	<-f.started
	if err := b.calls.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "outcome unknown") {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("disconnect did not terminate job wait")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.calls) != 1 {
		t.Fatalf("mutation repeated: %v", f.calls)
	}
}

func TestDBusJobDeadline(t *testing.T) {
	address, f := privateSystemd(t)
	f.mu.Lock()
	f.hold = true
	f.mu.Unlock()
	b := privateSystemdClient(t, address)
	ctx, cancel := context.WithTimeout(t.Context(), 50*time.Millisecond)
	defer cancel()
	err := b.SetState(ctx, "demo.service", "restart", false)
	if err == nil || !strings.Contains(err.Error(), "outcome unknown") {
		t.Fatal(err)
	}
}

func TestDBusReadsStateAndReloadsAfterEnablement(t *testing.T) {
	address, f := privateSystemd(t)
	b := privateSystemdClient(t, address)
	ctx := t.Context()
	active, enabled, err := b.State(ctx, "demo.service")
	if err != nil || active != "inactive" || enabled {
		t.Fatalf("%s %v %v", active, enabled, err)
	}
	for _, want := range []bool{true, false} {
		if err := b.SetEnabled(ctx, "demo.service", want); err != nil {
			t.Fatal(err)
		}
		_, enabled, err = b.State(ctx, "demo.service")
		if err != nil || enabled != want {
			t.Fatalf("enabled=%v err=%v", enabled, err)
		}
	}
	f.mu.Lock()
	calls := strings.Join(f.calls, ",")
	f.stateError = true
	f.mu.Unlock()
	if calls != "enable,daemon-reload,disable,daemon-reload" {
		t.Fatal(calls)
	}
	if _, _, err := b.State(ctx, "demo.service"); err == nil {
		t.Fatal("swallowed read failure")
	}
	active, enabled, err = b.State(ctx, "missing.service")
	if err != nil || active != "inactive" || enabled {
		t.Fatalf("missing unit: %s %v %v", active, enabled, err)
	}
}

func TestDBusRejectsOtherSendersJobSignals(t *testing.T) {
	address, f := privateSystemd(t)
	f.mu.Lock()
	f.hold = true
	f.mu.Unlock()
	b := privateSystemdClient(t, address)
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- b.SetState(ctx, "demo.service", "start", false) }()
	path := <-f.started
	other, err := dbus.Connect(address)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	if err := other.Emit(systemdPath, systemdManager+".JobRemoved", uint32(1), path, "demo.service", "done"); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		t.Fatalf("accepted another sender's job result: %v", err)
	case <-time.After(30 * time.Millisecond):
	}
	if err := f.conn.Emit(systemdPath, systemdManager+".JobRemoved", uint32(1), path, "demo.service", "done"); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}
