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
		var action string
		needsAction := false

		switch p.State {
		case "started":
			if currentActive != "active" {
				action = "start"
				needsAction = true
			}
		case "stopped":
			if currentActive == "active" {
				action = "stop"
				needsAction = true
			}
		case "restarted":
			action = "restart"
			needsAction = true
		case "reloaded":
			action = "reload"
			needsAction = true
		default:
			return nil, fmt.Errorf("unsupported service state: %q", p.State)
		}

		if needsAction {
			cmd := exec.Command("systemctl", action, p.Name)
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
			cmd := exec.Command("systemctl", action, p.Name)
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
