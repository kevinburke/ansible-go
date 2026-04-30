package fastagent

import (
	"encoding/json"
	"os"
	"path/filepath"
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
