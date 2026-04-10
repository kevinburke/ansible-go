package fastagent

import (
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
)

func (s *Server) handlePackage(params json.RawMessage) (any, error) {
	var p PackageParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal PackageParams: %w", err)
	}
	if len(p.Names) == 0 {
		return nil, fmt.Errorf("no package names specified")
	}
	if p.State == "" {
		p.State = "present"
	}

	switch p.Manager {
	case "apt":
		return s.handlePackageApt(p)
	case "dnf", "yum":
		return s.handlePackageDnf(p)
	default:
		return nil, fmt.Errorf("unsupported package manager: %q", p.Manager)
	}
}

func (s *Server) handlePackageApt(p PackageParams) (any, error) {
	var args []string
	switch p.State {
	case "present":
		args = append([]string{"install", "--yes"}, p.Names...)
	case "absent":
		args = append([]string{"remove", "--yes"}, p.Names...)
	case "latest":
		args = append([]string{"install", "--yes", "--upgrade"}, p.Names...)
	default:
		return nil, fmt.Errorf("unsupported state %q for apt", p.State)
	}

	cmd := exec.Command("apt-get", args...)
	cmd.Env = append(cmd.Environ(), "DEBIAN_FRONTEND=noninteractive")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("apt-get %s: %s\n%s", args[0], err, string(out))
	}

	// Detect whether anything actually changed by looking for "0 newly installed"
	// and "0 to remove" in apt-get output.
	changed := !strings.Contains(string(out), "0 newly installed") ||
		!strings.Contains(string(out), "0 to remove")

	return PackageResult{
		Changed: changed,
		Msg:     string(out),
	}, nil
}

func (s *Server) handlePackageDnf(p PackageParams) (any, error) {
	manager := p.Manager
	if manager == "" {
		manager = "dnf"
	}

	var args []string
	switch p.State {
	case "present":
		args = append([]string{"install", "-y"}, p.Names...)
	case "absent":
		args = append([]string{"remove", "-y"}, p.Names...)
	case "latest":
		args = append([]string{"install", "-y", "--best"}, p.Names...)
	default:
		return nil, fmt.Errorf("unsupported state %q for %s", p.State, manager)
	}

	cmd := exec.Command(manager, args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("%s %s: %s\n%s", manager, args[0], err, string(out))
	}

	changed := !strings.Contains(string(out), "Nothing to do")

	return PackageResult{
		Changed: changed,
		Msg:     string(out),
	}, nil
}
