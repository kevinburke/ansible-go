package fastagent

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
)

func TestHandleServiceSystemdPassesNoBlock(t *testing.T) {
	tmp := t.TempDir()
	logPath := filepath.Join(tmp, "systemctl.log")
	systemctlPath := filepath.Join(tmp, "systemctl")
	script := "#!/bin/sh\n" +
		"printf '%s\\n' \"$*\" >> " + logPath + "\n" +
		"case \"$1 $2\" in\n" +
		"  'is-active demo.service') echo inactive; exit 3 ;;\n" +
		"  'is-enabled demo.service') echo disabled; exit 1 ;;\n" +
		"  '--no-block start') exit 0 ;;\n" +
		"esac\n" +
		"exit 42\n"
	if err := os.WriteFile(systemctlPath, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", tmp+string(os.PathListSeparator)+os.Getenv("PATH"))

	params, err := json.Marshal(ServiceParams{
		Name:    "demo.service",
		State:   "started",
		NoBlock: true,
	})
	if err != nil {
		t.Fatal(err)
	}

	_, err = (&Server{}).handleService(params)
	if err != nil {
		t.Fatal(err)
	}

	got, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	want := "is-active demo.service\nis-enabled demo.service\n--no-block start demo.service\nis-active demo.service\n"
	if string(got) != want {
		t.Fatalf("systemctl calls:\n%s\nwant:\n%s", got, want)
	}
}

// A unit that crash-loops under Restart= reports "activating" between
// restarts. state=stopped must still stop it (2026-10-22: a retire play
// left netatmo-notifier looping on tiger after 3 million restarts).
func TestHandleServiceSystemdStopsUnitsThatAreNotInactive(t *testing.T) {
	for _, tc := range []struct {
		active   string
		wantStop bool
	}{
		{"active", true},
		{"activating", true},
		{"deactivating", true},
		{"reloading", true},
		{"refreshing", true},
		{"inactive", false},
		{"failed", false},
	} {
		t.Run(tc.active, func(t *testing.T) {
			tmp := t.TempDir()
			logPath := filepath.Join(tmp, "systemctl.log")
			systemctlPath := filepath.Join(tmp, "systemctl")
			script := "#!/bin/sh\n" +
				"printf '%s\\n' \"$*\" >> " + logPath + "\n" +
				"case \"$1 $2\" in\n" +
				"  'is-active demo.service') echo " + tc.active + "; exit 3 ;;\n" +
				"  'is-enabled demo.service') echo disabled; exit 1 ;;\n" +
				"  'stop demo.service') exit 0 ;;\n" +
				"esac\n" +
				"exit 42\n"
			if err := os.WriteFile(systemctlPath, []byte(script), 0o755); err != nil {
				t.Fatal(err)
			}
			t.Setenv("PATH", tmp+string(os.PathListSeparator)+os.Getenv("PATH"))

			params, err := json.Marshal(ServiceParams{Name: "demo.service", State: "stopped"})
			if err != nil {
				t.Fatal(err)
			}
			res, err := (&Server{}).handleService(params)
			if err != nil {
				t.Fatal(err)
			}
			if got := res.(ServiceResult).Changed; got != tc.wantStop {
				t.Errorf("changed = %v, want %v", got, tc.wantStop)
			}
			got, err := os.ReadFile(logPath)
			if err != nil {
				t.Fatal(err)
			}
			stopped := strings.Contains(string(got), "stop demo.service\n")
			if stopped != tc.wantStop {
				t.Fatalf("ran stop = %v, want %v; systemctl calls:\n%s", stopped, tc.wantStop, got)
			}
		})
	}
}

// The Python systemd module (used when the agent is not available) must
// stop the same states as the Go handler.
func TestPythonSystemdModuleRunningStatesMatchGo(t *testing.T) {
	src, err := os.ReadFile(filepath.Join("plugins", "modules", "systemd.py"))
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`(?s)RUNNING_STATES = frozenset\(\s*\((.*?)\)\s*\)`).FindSubmatch(src)
	if m == nil {
		t.Fatal("RUNNING_STATES not found in plugins/modules/systemd.py")
	}
	var py []string
	for _, q := range regexp.MustCompile(`"([a-z-]+)"`).FindAllSubmatch(m[1], -1) {
		py = append(py, string(q[1]))
	}
	if len(py) == 0 {
		t.Fatal("RUNNING_STATES is empty")
	}
	for _, s := range py {
		if !systemdUnitRunning(s) {
			t.Errorf("Python RUNNING_STATES has %q, systemdUnitRunning does not", s)
		}
	}
	for _, s := range []string{"active", "activating", "deactivating", "reloading", "refreshing", "inactive", "failed", "maintenance"} {
		if systemdUnitRunning(s) && !slices.Contains(py, s) {
			t.Errorf("systemdUnitRunning(%q) is true, Python RUNNING_STATES lacks it", s)
		}
	}
	if !strings.Contains(string(src), "current_active in RUNNING_STATES") {
		t.Error("plugins/modules/systemd.py no longer checks RUNNING_STATES for state=stopped")
	}
}
