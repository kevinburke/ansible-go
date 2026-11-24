package fastagent

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

type fakeServiceBus struct {
	names     []string
	done      chan struct{}
	active    string
	enabled   bool
	calls     []string
	stateErr  error
	mutateErr error
	wait      func(context.Context) error
	noBlock   bool
}

func (b *fakeServiceBus) Close() {
	select {
	case <-b.done:
	default:
		close(b.done)
	}
}
func (b *fakeServiceBus) Done() <-chan struct{} { return b.done }
func (b *fakeServiceBus) State(ctx context.Context, name string) (string, bool, error) {
	b.calls = append(b.calls, "state")
	b.names = append(b.names, name)
	return b.active, b.enabled, b.stateErr
}
func (b *fakeServiceBus) SetState(ctx context.Context, name, action string, noBlock bool) error {
	b.calls = append(b.calls, action)
	b.noBlock = noBlock
	if b.wait != nil {
		return b.wait(ctx)
	}
	return b.mutateErr
}
func (b *fakeServiceBus) SetEnabled(ctx context.Context, name string, enabled bool) error {
	b.calls = append(b.calls, "enable")
	b.enabled = enabled
	return b.mutateErr
}
func (b *fakeServiceBus) Reload(context.Context) error {
	b.calls = append(b.calls, "reload")
	return b.mutateErr
}

func testServiceManager(t *testing.T, b *fakeServiceBus) *serviceManager {
	t.Helper()
	m := &serviceManager{dial: func(context.Context) (serviceBus, error) { return b, nil }}
	t.Cleanup(m.close)
	return m
}

func TestServiceConnectionReuseAndFreshState(t *testing.T) {
	b := &fakeServiceBus{done: make(chan struct{}), active: "active", enabled: true}
	m := testServiceManager(t, b)
	dials := 0
	m.dial = func(context.Context) (serviceBus, error) { dials++; return b, nil }
	if dials != 0 {
		t.Fatal("eagerly connected")
	}
	for _, state := range []string{"active", "inactive"} {
		b.active = state
		r, err := m.apply(t.Context(), ServiceParams{Name: "demo.service"})
		if err != nil || r.State != state || r.Changed {
			t.Fatalf("result=%+v error=%v", r, err)
		}
	}
	if dials != 1 {
		t.Fatalf("connections=%d", dials)
	}
}

func TestServiceReconnectsOnlyOnNextRequest(t *testing.T) {
	first := &fakeServiceBus{done: make(chan struct{}), active: "inactive", mutateErr: errors.New("lost reply")}
	second := &fakeServiceBus{done: make(chan struct{}), active: "active"}
	m := testServiceManager(t, first)
	dials := 0
	m.dial = func(context.Context) (serviceBus, error) {
		dials++
		if dials == 1 {
			return first, nil
		}
		return second, nil
	}
	_, err := m.apply(t.Context(), ServiceParams{Name: "demo.service", State: "restarted"})
	if err == nil || dials != 1 || strings.Join(first.calls, ",") != "state,start" {
		t.Fatalf("error=%v dials=%d calls=%v", err, dials, first.calls)
	}
	first.Close()
	r, err := m.apply(t.Context(), ServiceParams{Name: "demo.service", State: "started"})
	if err != nil || r.Changed || dials != 2 {
		t.Fatalf("result=%+v error=%v dials=%d", r, err, dials)
	}
}

func TestServiceReadFailurePreventsMutation(t *testing.T) {
	b := &fakeServiceBus{done: make(chan struct{}), stateErr: errors.New("permission denied")}
	m := testServiceManager(t, b)
	_, err := m.apply(t.Context(), ServiceParams{Name: "demo.service", State: "started"})
	if err == nil || strings.Join(b.calls, ",") != "state" {
		t.Fatalf("error=%v calls=%v", err, b.calls)
	}
}

func TestServiceReloadAndStateOrdering(t *testing.T) {
	b := &fakeServiceBus{done: make(chan struct{}), active: "inactive"}
	m := testServiceManager(t, b)
	enabled := true
	r, err := m.apply(t.Context(), ServiceParams{Name: "demo.service", DaemonReload: true, State: "started", Enabled: &enabled, NoBlock: true})
	if err != nil || !r.Changed || !b.noBlock {
		t.Fatalf("result=%+v error=%v", r, err)
	}
	if got := strings.Join(b.calls, ","); got != "reload,state,start,enable,state" {
		t.Fatal(got)
	}
	b.calls = nil
	r, err = m.apply(t.Context(), ServiceParams{DaemonReload: true})
	if err != nil || r.Changed || strings.Join(b.calls, ",") != "reload" {
		t.Fatalf("result=%+v error=%v calls=%v", r, err, b.calls)
	}
}

func TestServiceStopsActivatingAndDeactivatingUnits(t *testing.T) {
	for _, active := range []string{"active", "activating", "deactivating", "inactive", "failed"} {
		t.Run(active, func(t *testing.T) {
			b := &fakeServiceBus{done: make(chan struct{}), active: active}
			m := testServiceManager(t, b)
			r, err := m.apply(t.Context(), ServiceParams{Name: "demo.service", State: "stopped"})
			want := active == "active" || active == "activating" || active == "deactivating"
			if err != nil || r.Changed != want {
				t.Fatalf("result=%+v error=%v", r, err)
			}
		})
	}
}

func TestServiceRequestsAreSerialized(t *testing.T) {
	entered := make(chan struct{}, 2)
	release := make(chan struct{})
	b := &fakeServiceBus{done: make(chan struct{}), active: "inactive", wait: func(ctx context.Context) error {
		entered <- struct{}{}
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}}
	m := testServiceManager(t, b)
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	results := make(chan error, 2)
	request := func() { _, err := m.apply(ctx, ServiceParams{Name: "demo.service", State: "started"}); results <- err }
	go request()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	go request()
	select {
	case <-entered:
		t.Fatal("second service ran before the first completed")
	case <-time.After(30 * time.Millisecond):
	}
	close(release)
	for range 2 {
		select {
		case err := <-results:
			if err != nil {
				t.Fatal(err)
			}
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		}
	}
}

func TestServiceCloseInterruptsJobWait(t *testing.T) {
	entered := make(chan struct{})
	b := &fakeServiceBus{done: make(chan struct{}), active: "inactive", wait: func(ctx context.Context) error { close(entered); <-ctx.Done(); return ctx.Err() }}
	m := testServiceManager(t, b)
	results := make(chan error, 1)
	go func() {
		_, err := m.apply(t.Context(), ServiceParams{Name: "demo.service", State: "started"})
		results <- err
	}()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("request never entered")
	}
	closed := make(chan struct{})
	go func() { m.close(); close(closed) }()
	select {
	case err := <-results:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("shutdown did not interrupt wait")
	}
	<-closed
	if _, err := m.apply(t.Context(), ServiceParams{Name: "demo.service"}); err == nil {
		t.Fatal("reopened closed manager")
	}
}

func TestServiceNormalizesUnitNames(t *testing.T) {
	for name, want := range map[string]string{"demo": "demo.service", "demo.service": "demo.service", "postgresql@16.main": "postgresql@16.main.service", "demo.timer": "demo.timer"} {
		t.Run(name, func(t *testing.T) {
			b := &fakeServiceBus{done: make(chan struct{}), active: "active"}
			s := &Server{}
			s.services.dial = func(context.Context) (serviceBus, error) { return b, nil }
			t.Cleanup(s.Close)
			params, err := json.Marshal(ServiceParams{Name: name})
			if err != nil {
				t.Fatal(err)
			}
			result, err := s.handleService(params)
			if err != nil {
				t.Fatal(err)
			}
			if b.names[0] != want || result.(ServiceResult).Name != name {
				t.Fatalf("name=%s result=%+v", b.names[0], result)
			}
		})
	}
}

func TestServiceSessionSurvivesRPCConnections(t *testing.T) {
	b := &fakeServiceBus{done: make(chan struct{}), active: "active", enabled: true}
	s := newTestServer()
	t.Cleanup(s.Close)
	dials := 0
	s.services.dial = func(context.Context) (serviceBus, error) { dials++; return b, nil }
	if resp := rpcCall(t, s, "Hello", HelloParams{Version: Version}); resp.Error != nil {
		t.Fatal(resp.Error)
	}
	if dials != 0 {
		t.Fatal("Hello opened D-Bus")
	}
	for range 2 {
		// rpcCall serves a new input/output stream on every invocation.
		if resp := rpcCall(t, s, "Service", ServiceParams{Name: "demo", State: "started"}); resp.Error != nil {
			t.Fatal(resp.Error)
		}
	}
	if dials != 1 {
		t.Fatalf("dialed %d sessions across two RPC connections", dials)
	}
	if resp := rpcCall(t, s, "Service", ServiceParams{DaemonReload: true}); resp.Error != nil {
		t.Fatal(resp.Error)
	}
	if got := strings.Join(b.calls, ","); got != "state,state,reload" {
		t.Fatal(got)
	}
}
