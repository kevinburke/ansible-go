package fastagent

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// aptCacheState tracks when apt-get update was last run so we can skip
// redundant updates within the same daemon session.
var (
	aptCacheMu      sync.Mutex
	aptCacheUpdated time.Time
)

func (s *Server) handlePackage(params json.RawMessage) (any, error) {
	var p PackageParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal PackageParams: %w", err)
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
	cacheUpdated := false

	if p.UpdateCache {
		skip := false

		// Check if the apt cache is fresh enough to skip the update.
		// Use whichever is more recent: our in-memory timestamp or the
		// on-disk apt cache timestamp.
		validTime := p.CacheValidTime
		if validTime <= 0 {
			// Default: skip if we already ran apt-get update in this
			// daemon session within the last 60 seconds.
			validTime = 60
		}

		aptCacheMu.Lock()
		if !aptCacheUpdated.IsZero() && time.Since(aptCacheUpdated) < time.Duration(validTime)*time.Second {
			skip = true
			s.Logger.Debug("apt cache fresh, skipping update",
				"age", time.Since(aptCacheUpdated).String(),
				"valid_time", validTime)
		}
		aptCacheMu.Unlock()

		if !skip {
			// Also check the on-disk cache timestamp.
			if info, err := os.Stat("/var/lib/apt/lists/lock"); err == nil {
				if time.Since(info.ModTime()) < time.Duration(validTime)*time.Second {
					skip = true
					s.Logger.Debug("apt lists cache fresh on disk, skipping update",
						"age", time.Since(info.ModTime()).String())
				}
			}
		}

		if !skip {
			s.Logger.Debug("running apt-get update")
			cmd := exec.Command("apt-get", "update")
			cmd.Env = append(cmd.Environ(), "DEBIAN_FRONTEND=noninteractive")
			out, err := cmd.CombinedOutput()
			if err != nil {
				return nil, fmt.Errorf("apt-get update: %s\n%s", err, string(out))
			}
			aptCacheMu.Lock()
			aptCacheUpdated = time.Now()
			aptCacheMu.Unlock()
			cacheUpdated = true
		}
	}

	if len(p.Names) == 0 {
		return PackageResult{
			Changed:      cacheUpdated,
			CacheUpdated: cacheUpdated,
			Msg:          "Cache updated",
		}, nil
	}

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

	// Detect whether anything actually changed.
	changed := !strings.Contains(string(out), "0 newly installed") ||
		!strings.Contains(string(out), "0 to remove")

	return PackageResult{
		Changed:      changed || cacheUpdated,
		CacheUpdated: cacheUpdated,
		Msg:          string(out),
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
