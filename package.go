package fastagent

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// aptState tracks apt cache freshness and installed packages so we can
// skip redundant operations within the same daemon session.
var (
	aptMu             sync.Mutex
	aptCacheUpdated   time.Time
	aptInstalledPkgs  map[string]bool // package name → true if installed
	aptInstalledValid bool            // whether the map is populated
)

// Default apt sources locations. Variables (not constants) so tests can
// override them.
var (
	aptSourcesList = "/etc/apt/sources.list"
	aptSourcesDir  = "/etc/apt/sources.list.d"
	aptListsLock   = "/var/lib/apt/lists/lock"
)

// latestAptSourcesMTime returns the most recent mtime across the apt
// source list file and one level of entries in the sources.list.d
// directory (plus the directory itself, to catch additions/removals).
// Missing paths are ignored. Returns the zero time if nothing readable
// is found.
func latestAptSourcesMTime(list, dir string) time.Time {
	var latest time.Time
	bump := func(t time.Time) {
		if t.After(latest) {
			latest = t
		}
	}

	if info, err := os.Stat(list); err == nil {
		bump(info.ModTime())
	}
	if info, err := os.Stat(dir); err == nil {
		bump(info.ModTime())
	}
	if entries, err := os.ReadDir(dir); err == nil {
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			info, err := e.Info()
			if err != nil {
				continue
			}
			bump(info.ModTime())
		}
	}
	return latest
}

// aptCacheFresh reports whether an apt cache last refreshed at updatedAt
// can be considered fresh at time now. The cache is fresh only when it
// is non-zero, within the validity window, and not older than the
// latest apt sources mtime — a sources change (e.g. a newly written
// deb822 .sources file) invalidates the cache regardless of age.
func aptCacheFresh(updatedAt, sourcesMTime, now time.Time, validTime time.Duration) bool {
	if updatedAt.IsZero() {
		return false
	}
	if now.Sub(updatedAt) >= validTime {
		return false
	}
	if !sourcesMTime.IsZero() && sourcesMTime.After(updatedAt) {
		return false
	}
	return true
}

// loadInstalledPackages reads dpkg status to build the installed package set.
// Must be called with aptMu held.
func loadInstalledPackages(logger interface{ Debug(string, ...any) }) {
	pkgs := make(map[string]bool)

	f, err := os.Open("/var/lib/dpkg/status")
	if err != nil {
		logger.Debug("cannot read dpkg status, disabling package cache", "error", err)
		aptInstalledValid = false
		return
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	var currentPkg string
	var installed bool

	for scanner.Scan() {
		line := scanner.Text()
		if pkg, ok := strings.CutPrefix(line, "Package: "); ok {
			currentPkg = pkg
			installed = false
		} else if strings.HasPrefix(line, "Status: ") {
			// "Status: install ok installed" means the package is installed.
			installed = strings.Contains(line, " installed")
		} else if line == "" {
			if currentPkg != "" && installed {
				pkgs[currentPkg] = true
			}
			currentPkg = ""
			installed = false
		}
	}
	// Handle last entry if file doesn't end with blank line.
	if currentPkg != "" && installed {
		pkgs[currentPkg] = true
	}

	aptInstalledPkgs = pkgs
	aptInstalledValid = true
	logger.Debug("loaded dpkg package cache", "count", len(pkgs))
}

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

		validTime := p.CacheValidTime
		if validTime <= 0 {
			validTime = 60
		}
		validDur := time.Duration(validTime) * time.Second
		now := time.Now()
		sourcesMTime := latestAptSourcesMTime(aptSourcesList, aptSourcesDir)

		aptMu.Lock()
		recorded := aptCacheUpdated
		aptMu.Unlock()

		if aptCacheFresh(recorded, sourcesMTime, now, validDur) {
			skip = true
			s.Logger.Debug("apt cache fresh, skipping update",
				"age", now.Sub(recorded).String(),
				"valid_time", validTime)
		} else if !recorded.IsZero() && sourcesMTime.After(recorded) {
			s.Logger.Debug("apt sources changed since last update, refreshing",
				"sources_mtime", sourcesMTime,
				"cache_updated", recorded)
		}

		if !skip {
			if info, err := os.Stat(aptListsLock); err == nil {
				if aptCacheFresh(info.ModTime(), sourcesMTime, now, validDur) {
					skip = true
					s.Logger.Debug("apt lists cache fresh on disk, skipping update",
						"age", now.Sub(info.ModTime()).String())
				} else if sourcesMTime.After(info.ModTime()) {
					s.Logger.Debug("apt sources changed since on-disk update, refreshing",
						"sources_mtime", sourcesMTime,
						"lock_mtime", info.ModTime())
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
			aptMu.Lock()
			aptCacheUpdated = time.Now()
			// Invalidate the installed packages cache since repos may have changed.
			aptInstalledValid = false
			aptMu.Unlock()
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

	// For state=present, check if all packages are already installed using
	// the dpkg cache. This avoids shelling out to apt-get entirely.
	if p.State == "present" {
		aptMu.Lock()
		if !aptInstalledValid {
			loadInstalledPackages(s.Logger)
		}
		if aptInstalledValid {
			allInstalled := true
			for _, name := range p.Names {
				if !aptInstalledPkgs[name] {
					allInstalled = false
					break
				}
			}
			if allInstalled {
				aptMu.Unlock()
				s.Logger.Debug("all packages already installed (dpkg cache)",
					"packages", strings.Join(p.Names, ", "))
				return PackageResult{
					Changed:      cacheUpdated,
					CacheUpdated: cacheUpdated,
					Msg:          "All packages already installed",
				}, nil
			}
		}
		aptMu.Unlock()
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

	// If packages were installed or removed, invalidate the cache.
	if changed {
		aptMu.Lock()
		aptInstalledValid = false
		aptMu.Unlock()
	}

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
