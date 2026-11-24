package fastagent

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// systemdStateAction must match the table that tests/test_systemd_module.py
// also checks against plugins/modules/systemd.py and upstream Ansible.
func TestSystemdStateActionMatchesUpstreamTable(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("testdata", "systemd_state_actions.json"))
	if err != nil {
		t.Fatal(err)
	}
	var table struct {
		Cases []struct {
			State  string `json:"state"`
			Active string `json:"active"`
			Action string `json:"action"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &table); err != nil {
		t.Fatal(err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("no cases in testdata/systemd_state_actions.json")
	}
	for _, c := range table.Cases {
		got, err := systemdStateAction(c.State, c.Active)
		if err != nil {
			t.Errorf("systemdStateAction(%q, %q): %v", c.State, c.Active, err)
			continue
		}
		if got != c.Action {
			t.Errorf("systemdStateAction(%q, %q) = %q, want %q", c.State, c.Active, got, c.Action)
		}
	}
	if _, err := systemdStateAction("bogus", "active"); err == nil {
		t.Error("systemdStateAction accepted an unknown state")
	}
}
