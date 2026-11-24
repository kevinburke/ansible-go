package fastagent

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
)

func (s *Server) handleService(params json.RawMessage) (any, error) {
	var p ServiceParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal ServiceParams: %w", err)
	}
	if p.Manager != "" && p.Manager != "systemd" {
		return nil, fmt.Errorf("unsupported service manager: %q", p.Manager)
	}
	if p.Name == "" && (!p.DaemonReload || p.State != "" || p.Enabled != nil) {
		return nil, fmt.Errorf("service name is required")
	}
	if p.State != "" {
		if _, err := systemdStateAction(p.State, "inactive"); err != nil {
			return nil, err
		}
	}
	name := p.Name
	p.Name = systemdUnitName(name)
	result, err := s.services.apply(context.Background(), p)
	result.Name = name
	return result, err
}

// Match systemctl's default .service suffix, including dotted instance names.
func systemdUnitName(name string) string {
	if name == "" {
		return name
	}
	for _, suffix := range []string{".service", ".socket", ".target", ".device", ".mount", ".automount", ".swap", ".timer", ".path", ".slice", ".scope"} {
		if strings.HasSuffix(name, suffix) {
			return name
		}
	}
	return name + ".service"
}

func (m *serviceManager) apply(ctx context.Context, p ServiceParams) (result ServiceResult, err error) {
	m.initialize()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stop := context.AfterFunc(m.lifetime, cancel)
	defer stop()
	m.mu.Lock()
	defer m.mu.Unlock()
	result = ServiceResult{Name: p.Name}
	if err := ctx.Err(); err != nil {
		return result, err
	}
	bus, err := m.connect(ctx)
	if err != nil {
		return result, err
	}
	// Do not replay any part of a failed request. Even a successful mutation
	// followed by a failed status read has an ambiguous final outcome.
	defer func() {
		if err != nil {
			bus.Close()
			m.bus = nil
		}
	}()
	if p.DaemonReload {
		if err := bus.Reload(ctx); err != nil {
			return result, fmt.Errorf("daemon-reload: %w (operation not retried)", err)
		}
	}
	if p.Name == "" {
		return result, nil
	}
	result.State, result.Enabled, err = bus.State(ctx, p.Name)
	if err != nil {
		return result, err
	}
	if p.State != "" {
		action, err := systemdStateAction(p.State, result.State)
		if err != nil {
			return result, err
		}
		if action != "" {
			if err := bus.SetState(ctx, p.Name, action, p.NoBlock); err != nil {
				return result, err
			}
			result.Changed = true
		}
	}
	if p.Enabled != nil && *p.Enabled != result.Enabled {
		if err := bus.SetEnabled(ctx, p.Name, *p.Enabled); err != nil {
			return result, fmt.Errorf("set %s enabled=%v: %w (operation not retried)", p.Name, *p.Enabled, err)
		}
		result.Changed = true
	}
	if result.Changed {
		result.State, result.Enabled, err = bus.State(ctx, p.Name)
		if err != nil {
			return result, fmt.Errorf("read state after changes: %w (operation not retried)", err)
		}
	}
	return result, nil
}

// systemdStateAction returns the systemctl verb that brings a unit whose
// `systemctl is-active` state is active to the requested state, or "" when
// nothing needs to run. It mirrors the decision in upstream Ansible's
// systemd_service module (lib/ansible/modules/systemd_service.py, main()):
//
//   - started: start unless the unit is running (active or activating).
//   - stopped: stop if the unit is running or deactivating.
//   - restarted, reloaded: start a unit that is not running; otherwise
//     restart or reload it.
//
// A unit crash-looping under Restart= is "activating" (auto-restart) between
// attempts, so it counts as running. plugins/modules/systemd.py implements
// the same table as state_action; testdata/systemd_state_actions.json
// checks both.
func systemdStateAction(state, active string) (string, error) {
	running := active == "active" || active == "activating"
	switch state {
	case "started":
		if !running {
			return "start", nil
		}
	case "stopped":
		if running || active == "deactivating" {
			return "stop", nil
		}
	case "restarted", "reloaded":
		if !running {
			return "start", nil
		}
		return strings.TrimSuffix(state, "ed"), nil
	default:
		return "", fmt.Errorf("unsupported service state: %q", state)
	}
	return "", nil
}
