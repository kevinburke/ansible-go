package fastagent

import (
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
)

func (s *Server) handleService(params json.RawMessage) (any, error) {
	var p ServiceParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal ServiceParams: %w", err)
	}
	if p.Name == "" {
		return nil, fmt.Errorf("service name is required")
	}
	if p.Manager == "" {
		p.Manager = "systemd"
	}

	switch p.Manager {
	case "systemd":
		return s.handleServiceSystemd(p)
	default:
		return nil, fmt.Errorf("unsupported service manager: %q", p.Manager)
	}
}

func (s *Server) handleServiceSystemd(p ServiceParams) (any, error) {
	changed := false

	// Get current state.
	activeOut, _ := exec.Command("systemctl", "is-active", p.Name).Output()
	currentActive := strings.TrimSpace(string(activeOut))

	enabledOut, _ := exec.Command("systemctl", "is-enabled", p.Name).Output()
	currentEnabled := strings.TrimSpace(string(enabledOut)) == "enabled"

	// Handle state changes.
	if p.State != "" {
		action, err := systemdStateAction(p.State, currentActive)
		if err != nil {
			return nil, err
		}
		needsAction := action != ""

		if needsAction {
			cmd := systemctlCommand(p, action, p.Name)
			if out, err := cmd.CombinedOutput(); err != nil {
				return nil, fmt.Errorf("systemctl %s %s: %s\n%s", action, p.Name, err, string(out))
			}
			changed = true
		}
	}

	// Handle enabled changes.
	if p.Enabled != nil {
		want := *p.Enabled
		if want != currentEnabled {
			action := "disable"
			if want {
				action = "enable"
			}
			cmd := systemctlCommand(p, action, p.Name)
			if out, err := cmd.CombinedOutput(); err != nil {
				return nil, fmt.Errorf("systemctl %s %s: %s\n%s", action, p.Name, err, string(out))
			}
			changed = true
			currentEnabled = want
		}
	}

	// Re-check active state after changes.
	if changed {
		activeOut, _ = exec.Command("systemctl", "is-active", p.Name).Output()
		currentActive = strings.TrimSpace(string(activeOut))
	}

	return ServiceResult{
		Changed: changed,
		Name:    p.Name,
		State:   currentActive,
		Enabled: currentEnabled,
	}, nil
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

func systemctlCommand(p ServiceParams, args ...string) *exec.Cmd {
	cmdArgs := make([]string, 0, len(args)+1)
	if p.NoBlock {
		cmdArgs = append(cmdArgs, "--no-block")
	}
	cmdArgs = append(cmdArgs, args...)
	return exec.Command("systemctl", cmdArgs...)
}
