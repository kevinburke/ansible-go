package fastagent

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strconv"
	"sync"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
)

const (
	systemdDestination                 = "org.freedesktop.systemd1"
	systemdManager                     = systemdDestination + ".Manager"
	systemdPath        dbus.ObjectPath = "/org/freedesktop/systemd1"
	// Bound D-Bus method replies, not service startup. Systemd's configured
	// unit/job timeouts continue to govern how long a blocking job may take.
	systemdCallTimeout = 30 * time.Second
)

type serviceBus interface {
	Close()
	Done() <-chan struct{}
	State(context.Context, string) (string, bool, error)
	SetState(context.Context, string, string, bool) error
	SetEnabled(context.Context, string, bool) error
	Reload(context.Context) error
}

// serviceManager owns a lazy D-Bus session across controller connections. Its
// lock covers the entire request, including waiting for systemd jobs. File and
// command RPCs do not take this lock. Unit state is never cached.
type serviceManager struct {
	mu       sync.Mutex
	bus      serviceBus
	dial     func(context.Context) (serviceBus, error)
	closed   bool
	init     sync.Once
	lifetime context.Context
	cancel   context.CancelFunc
}

func (m *serviceManager) initialize() {
	m.init.Do(func() { m.lifetime, m.cancel = context.WithCancel(context.Background()) })
}

func (m *serviceManager) close() {
	m.initialize()
	m.cancel()
	m.mu.Lock()
	defer m.mu.Unlock()
	m.closed = true
	if m.bus != nil {
		m.bus.Close()
		m.bus = nil
	}
}

func (m *serviceManager) connect(ctx context.Context) (serviceBus, error) {
	if m.closed {
		return nil, errors.New("systemd client is closed")
	}
	if m.bus != nil {
		select {
		case <-m.bus.Done():
			m.bus.Close()
			m.bus = nil
		default:
			return m.bus, nil
		}
	}
	dial := m.dial
	if dial == nil {
		dial = dialSystemd
	}
	bus, err := dial(ctx)
	if err != nil {
		return nil, fmt.Errorf("connect to systemd D-Bus: %w", err)
	}
	m.bus = bus
	return bus, nil
}

// systemdBus uses go-systemd's separate method and signal connections as one
// session. The library registers job waiters before processing early completion
// signals. Closing either connection invalidates the whole session.
type systemdBus struct {
	conn   *systemd.Conn
	calls  *dbus.Conn
	ctx    context.Context
	cancel context.CancelFunc
}

func dialSystemd(ctx context.Context) (serviceBus, error) {
	return newSystemdBus(ctx, func(lifetime context.Context) (*dbus.Conn, error) {
		return dbus.ConnectSystemBus(dbus.WithContext(lifetime),
			dbus.WithAuth(dbus.AuthExternal(strconv.Itoa(os.Geteuid()))))
	})
}

func newSystemdBus(ctx context.Context, dial func(context.Context) (*dbus.Conn, error)) (*systemdBus, error) {
	ctx, cancelSetup := context.WithTimeout(ctx, systemdCallTimeout)
	defer cancelSetup()
	// A request deadline must not become the lifetime of a successful session.
	lifetime, cancel := context.WithCancel(context.Background())
	stop := context.AfterFunc(ctx, cancel)
	var connections []*dbus.Conn
	conn, err := systemd.NewConnection(func() (*dbus.Conn, error) {
		c, err := dial(lifetime)
		if err == nil {
			connections = append(connections, c)
			context.AfterFunc(c.Context(), cancel)
		}
		return c, err
	})
	if !stop() && err == nil {
		err = ctx.Err()
	}
	if err != nil {
		cancel()
		for _, c := range connections {
			_ = c.Close()
		}
		return nil, err
	}
	b := &systemdBus{conn: conn, calls: connections[0], ctx: lifetime, cancel: cancel}
	// Check match installation explicitly: go-systemd's constructor does not
	// return AddMatch errors. Never report a queued job as a completed job.
	signals := connections[1]
	if err := signals.AddMatchSignalContext(ctx,
		dbus.WithMatchSender(systemdDestination), dbus.WithMatchObjectPath(systemdPath),
		dbus.WithMatchInterface(systemdManager), dbus.WithMatchMember("JobRemoved")); err != nil {
		b.Close()
		return nil, fmt.Errorf("subscribe to job completions: %w", err)
	}
	// Replace the library's broad match with a sender/path-scoped match.
	if err := signals.RemoveMatchSignalContext(ctx,
		dbus.WithMatchInterface(systemdManager), dbus.WithMatchMember("JobRemoved")); err != nil {
		b.Close()
		return nil, fmt.Errorf("replace job signal match: %w", err)
	}
	if err := signals.Object(systemdDestination, systemdPath).
		CallWithContext(ctx, systemdManager+".Subscribe", 0).Err; err != nil {
		b.Close()
		return nil, fmt.Errorf("subscribe to systemd: %w", err)
	}
	return b, nil
}

func (b *systemdBus) Close()                { b.cancel(); b.conn.Close() }
func (b *systemdBus) Done() <-chan struct{} { return b.ctx.Done() }

func (b *systemdBus) State(ctx context.Context, name string) (string, bool, error) {
	ctx, cancel := context.WithTimeout(ctx, systemdCallTimeout)
	defer cancel()
	var path dbus.ObjectPath
	manager := b.calls.Object(systemdDestination, systemdPath)
	if err := manager.CallWithContext(ctx, systemdManager+".LoadUnit", 0, name).Store(&path); err != nil {
		if missingUnit(err) {
			return "inactive", false, nil
		}
		return "", false, fmt.Errorf("load unit %s: %w", name, err)
	}
	var active dbus.Variant
	if err := b.calls.Object(systemdDestination, path).CallWithContext(ctx,
		"org.freedesktop.DBus.Properties.Get", 0, systemdDestination+".Unit", "ActiveState").Store(&active); err != nil {
		return "", false, fmt.Errorf("read %s active state: %w", name, err)
	}
	state, ok := active.Value().(string)
	if !ok || state == "" {
		return "", false, fmt.Errorf("invalid active state for %s: %v", name, active.Value())
	}
	var fileState string
	if err := manager.CallWithContext(ctx, systemdManager+".GetUnitFileState", 0, name).Store(&fileState); err != nil {
		if !missingUnit(err) {
			return "", false, fmt.Errorf("read %s enablement: %w", name, err)
		}
	}
	// Match the existing Service RPC: only persistent enablement counts as
	// enabled; runtime enablement must still be made persistent when requested.
	return state, fileState == "enabled", nil
}

func missingUnit(err error) bool {
	var e dbus.Error
	if !errors.As(err, &e) {
		return false
	}
	switch e.Name {
	case systemdDestination + ".NoSuchUnit", systemdDestination + ".NoSuchUnitFile", "org.freedesktop.DBus.Error.FileNotFound":
		return true
	}
	return false
}

func (b *systemdBus) SetState(ctx context.Context, name, action string, noBlock bool) error {
	callCtx, cancelCall := context.WithTimeout(ctx, systemdCallTimeout)
	defer cancelCall()
	var result chan string
	if !noBlock {
		result = make(chan string, 1)
	}
	var err error
	switch action {
	case "start":
		_, err = b.conn.StartUnitContext(callCtx, name, "replace", result)
	case "stop":
		_, err = b.conn.StopUnitContext(callCtx, name, "replace", result)
	case "restart":
		_, err = b.conn.RestartUnitContext(callCtx, name, "replace", result)
	case "reload":
		_, err = b.conn.ReloadUnitContext(callCtx, name, "replace", result)
	default:
		return fmt.Errorf("unsupported systemd action %q", action)
	}
	cancelCall()
	if err != nil {
		return systemdMutationError(action+" "+name, err)
	}
	if noBlock {
		return nil
	}
	select {
	case outcome := <-result:
		// systemctl also treats a job skipped by systemd as success.
		if outcome != "done" && outcome != "skipped" {
			return fmt.Errorf("%s %s: systemd job result %q", action, name, outcome)
		}
		return nil
	case <-b.Done():
		return fmt.Errorf("%s %s: D-Bus disconnected; outcome unknown, operation not retried", action, name)
	case <-ctx.Done():
		return fmt.Errorf("%s %s: %w; outcome unknown, operation not retried", action, name, ctx.Err())
	}
}

func (b *systemdBus) SetEnabled(ctx context.Context, name string, enabled bool) error {
	ctx, cancel := context.WithTimeout(ctx, systemdCallTimeout)
	defer cancel()
	if enabled {
		if _, _, err := b.conn.EnableUnitFilesContext(ctx, []string{name}, false, false); err != nil {
			return systemdMutationError("enable "+name, err)
		}
	} else {
		if _, err := b.conn.DisableUnitFilesContext(ctx, []string{name}, false); err != nil {
			return systemdMutationError("disable "+name, err)
		}
	}
	// systemctl enable/disable implicitly reloads the manager after changing
	// unit-file links. The D-Bus methods themselves do not do that.
	return b.Reload(ctx)
}

func (b *systemdBus) Reload(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, systemdCallTimeout)
	defer cancel()
	if err := b.conn.ReloadContext(ctx); err != nil {
		return systemdMutationError("daemon-reload", err)
	}
	return nil
}

// A method error is a definite systemd refusal. A missing reply can mean the
// mutation already ran, so callers must not replay it over a new connection.
func systemdMutationError(operation string, err error) error {
	if _, ok := errors.AsType[dbus.Error](err); ok {
		return fmt.Errorf("%s: %w (operation not retried)", operation, err)
	}
	return fmt.Errorf("%s: %w; outcome unknown, operation not retried", operation, err)
}
