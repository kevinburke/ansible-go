package fastagent

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testPlaceholder = "@@FASTAGENT_VALIDATE_test@@"

// validatorFixture writes a validator script that records its argv, the
// mode and content of every existing file argument, and exits with the
// code in rcFile (0 if absent).
type validatorFixture struct {
	dir, script, log, rcFile string
}

func newValidatorFixture(t *testing.T) validatorFixture {
	t.Helper()
	dir := t.TempDir()
	f := validatorFixture{
		dir:    dir,
		script: filepath.Join(dir, "validator"),
		log:    filepath.Join(dir, "log"),
		rcFile: filepath.Join(dir, "rc"),
	}
	script := `#!/bin/sh
for a in "$@"; do
  echo "arg=$a" >> ` + f.log + `
  if [ -f "$a" ]; then
    echo "mode=$(ls -l "$a" | cut -c1-10)" >> ` + f.log + `
    echo "content=$(cat "$a")" >> ` + f.log + `
  fi
done
echo "validator stdout"
echo "validator stderr" >&2
exit $(cat ` + f.rcFile + ` 2>/dev/null || echo 0)
`
	if err := os.WriteFile(f.script, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return f
}

func (f validatorFixture) setRC(t *testing.T, rc string) {
	t.Helper()
	if err := os.WriteFile(f.rcFile, []byte(rc), 0o644); err != nil {
		t.Fatal(err)
	}
}

func (f validatorFixture) readLog(t *testing.T) string {
	t.Helper()
	b, err := os.ReadFile(f.log)
	if os.IsNotExist(err) {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func writeFileValidate(t *testing.T, p WriteFileParams) (WriteFileResult, *ErrorInfo) {
	t.Helper()
	resp := rpcCall(t, newTestServer(), "WriteFile", p)
	if resp.Error != nil {
		return WriteFileResult{}, resp.Error
	}
	b, _ := json.Marshal(resp.Result)
	var r WriteFileResult
	if err := json.Unmarshal(b, &r); err != nil {
		t.Fatal(err)
	}
	return r, nil
}

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

func readString(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestWriteFileValidatePasses(t *testing.T) {
	f := newValidatorFixture(t)
	destDir := t.TempDir()
	dest := filepath.Join(destDir, "app.conf")
	if err := os.WriteFile(dest, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r, rpcErr := writeFileValidate(t, WriteFileParams{
		Dest: dest, Content: b64("new\n"),
		Validate: &WriteValidate{
			Argv:        []string{f.script, "--file=" + testPlaceholder, testPlaceholder},
			Placeholder: testPlaceholder,
		},
	})
	if rpcErr != nil {
		t.Fatal(rpcErr)
	}
	if !r.Changed || r.ValidateFailed != nil {
		t.Fatalf("got %+v, want changed with no validation failure", r)
	}
	if got := readString(t, dest); got != "new\n" {
		t.Errorf("dest = %q, want new content", got)
	}

	log := f.readLog(t)
	lines := strings.Split(strings.TrimSpace(log), "\n")
	if len(lines) != 4 || !strings.HasPrefix(lines[0], "arg=--file=/") || !strings.HasPrefix(lines[1], "arg=/") {
		t.Fatalf("validator log:\n%s", log)
	}
	tmpPath := strings.TrimPrefix(lines[1], "arg=")
	if lines[0] != "arg=--file="+tmpPath {
		t.Errorf("placeholder inside an argument not substituted: %q", lines[0])
	}
	if lines[3] != "content=new" {
		t.Errorf("validator saw %q, want the new content", lines[3])
	}
	// Like stock, the file is validated outside the destination directory,
	// so a directory-watching service never sees an unvalidated file there,
	// and the temporary copy is removed afterwards.
	if filepath.Dir(filepath.Dir(tmpPath)) == destDir || filepath.Dir(tmpPath) == destDir {
		t.Errorf("validated %s inside the destination directory", tmpPath)
	}
	if _, err := os.Stat(filepath.Dir(tmpPath)); !os.IsNotExist(err) {
		t.Errorf("temporary validation directory %s still exists (err=%v)", filepath.Dir(tmpPath), err)
	}
	entries, _ := os.ReadDir(destDir)
	if len(entries) != 1 {
		t.Errorf("destination directory has leftovers: %v", entries)
	}
}

func TestWriteFileValidateFails(t *testing.T) {
	f := newValidatorFixture(t)
	f.setRC(t, "3")
	destDir := t.TempDir()
	dest := filepath.Join(destDir, "app.conf")
	if err := os.WriteFile(dest, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r, rpcErr := writeFileValidate(t, WriteFileParams{
		Dest: dest, Content: b64("new\n"),
		Validate: &WriteValidate{Argv: []string{f.script, testPlaceholder}, Placeholder: testPlaceholder},
	})
	if rpcErr != nil {
		t.Fatal(rpcErr)
	}
	if r.Changed {
		t.Error("changed = true after a failed validation")
	}
	vf := r.ValidateFailed
	if vf == nil {
		t.Fatalf("got %+v, want a validation failure", r)
	}
	if vf.RC != 3 || vf.Stdout != "validator stdout\n" || vf.Stderr != "validator stderr\n" || vf.StartErrno != 0 {
		t.Errorf("failure = %+v", vf)
	}
	if !strings.HasPrefix(vf.Path, "/") {
		t.Errorf("failure path = %q, want the validated temporary path", vf.Path)
	}
	if got := readString(t, dest); got != "old\n" {
		t.Errorf("dest = %q, want it untouched", got)
	}
	entries, _ := os.ReadDir(destDir)
	if len(entries) != 1 {
		t.Errorf("destination directory has leftovers: %v", entries)
	}
}

func TestWriteFileValidateNewFileFailsLeavesNothing(t *testing.T) {
	f := newValidatorFixture(t)
	f.setRC(t, "1")
	dest := filepath.Join(t.TempDir(), "sub", "new.conf")
	r, rpcErr := writeFileValidate(t, WriteFileParams{
		Dest: dest, Content: b64("new\n"),
		Validate: &WriteValidate{Argv: []string{f.script, testPlaceholder}, Placeholder: testPlaceholder},
	})
	if rpcErr != nil || r.ValidateFailed == nil {
		t.Fatalf("got %+v, %v; want a validation failure", r, rpcErr)
	}
	if _, err := os.Stat(filepath.Dir(dest)); !os.IsNotExist(err) {
		t.Errorf("parent directory created for a file that failed validation (err=%v)", err)
	}
}

func TestWriteFileValidateSkippedWhenUnchanged(t *testing.T) {
	// Stock runs validate only when the content would change.
	f := newValidatorFixture(t)
	f.setRC(t, "3")
	dest := filepath.Join(t.TempDir(), "app.conf")
	if err := os.WriteFile(dest, []byte("same\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r, rpcErr := writeFileValidate(t, WriteFileParams{
		Dest: dest, Content: b64("same\n"), Mode: "0600",
		Validate: &WriteValidate{Argv: []string{f.script, testPlaceholder}, Placeholder: testPlaceholder},
	})
	if rpcErr != nil {
		t.Fatal(rpcErr)
	}
	if r.ValidateFailed != nil || !r.Changed {
		t.Errorf("got %+v, want a mode-only change with no validation", r)
	}
	if log := f.readLog(t); log != "" {
		t.Errorf("validator ran for unchanged content:\n%s", log)
	}
}

func TestWriteFileValidateSeesRequestedMode(t *testing.T) {
	f := newValidatorFixture(t)
	dest := filepath.Join(t.TempDir(), "app.conf")
	if _, rpcErr := writeFileValidate(t, WriteFileParams{
		Dest: dest, Content: b64("new\n"), Mode: "0640",
		Validate: &WriteValidate{Argv: []string{f.script, testPlaceholder}, Placeholder: testPlaceholder},
	}); rpcErr != nil {
		t.Fatal(rpcErr)
	}
	if log := f.readLog(t); !strings.Contains(log, "mode=-rw-r-----") {
		t.Errorf("validator did not see mode 0640:\n%s", log)
	}
}

func TestWriteFileValidateBackupStillMade(t *testing.T) {
	// Stock makes the backup before validating, so a failed validation
	// still leaves one.
	f := newValidatorFixture(t)
	f.setRC(t, "3")
	destDir := t.TempDir()
	dest := filepath.Join(destDir, "app.conf")
	if err := os.WriteFile(dest, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r, rpcErr := writeFileValidate(t, WriteFileParams{
		Dest: dest, Content: b64("new\n"), Backup: true,
		Validate: &WriteValidate{Argv: []string{f.script, testPlaceholder}, Placeholder: testPlaceholder},
	})
	if rpcErr != nil || r.ValidateFailed == nil {
		t.Fatalf("got %+v, %v; want a validation failure", r, rpcErr)
	}
	if r.BackupFile == "" || readString(t, r.BackupFile) != "old\n" {
		t.Errorf("backup = %q, want a copy of the old content", r.BackupFile)
	}
}

func TestWriteFileValidateStartError(t *testing.T) {
	for _, argv0 := range []string{"/nonexistent/validator", "no-such-validator-on-path"} {
		t.Run(argv0, func(t *testing.T) {
			dest := filepath.Join(t.TempDir(), "app.conf")
			r, rpcErr := writeFileValidate(t, WriteFileParams{
				Dest: dest, Content: b64("new\n"),
				Env: map[string]string{"PATH": t.TempDir()},
				Validate: &WriteValidate{
					Argv: []string{argv0, testPlaceholder}, Placeholder: testPlaceholder,
				},
			})
			if rpcErr != nil {
				t.Fatal(rpcErr)
			}
			vf := r.ValidateFailed
			if vf == nil || vf.StartErrno != 2 {
				t.Fatalf("got %+v, want a start failure with ENOENT", vf)
			}
			if _, err := os.Stat(dest); !os.IsNotExist(err) {
				t.Error("dest written although the validator never ran")
			}
		})
	}
}

func TestWriteFileValidateSignal(t *testing.T) {
	dest := filepath.Join(t.TempDir(), "app.conf")
	r, rpcErr := writeFileValidate(t, WriteFileParams{
		Dest: dest, Content: b64("new\n"),
		Validate: &WriteValidate{
			Argv: []string{"/bin/sh", "-c", "kill -9 $$", testPlaceholder}, Placeholder: testPlaceholder,
		},
	})
	if rpcErr != nil {
		t.Fatal(rpcErr)
	}
	if r.ValidateFailed == nil || r.ValidateFailed.RC != -9 {
		t.Fatalf("got %+v, want rc -9", r.ValidateFailed)
	}
}

func TestWriteFileValidateUsesTaskEnvironment(t *testing.T) {
	// Like run_command: argv[0] is looked up on the task's PATH, and each
	// argument gets $VAR and ~ expansion.
	f := newValidatorFixture(t)
	if err := os.Rename(f.script, filepath.Join(f.dir, "my-validator")); err != nil {
		t.Fatal(err)
	}
	dest := filepath.Join(t.TempDir(), "app.conf")
	r, rpcErr := writeFileValidate(t, WriteFileParams{
		Dest: dest, Content: b64("new\n"),
		Env: map[string]string{"PATH": f.dir, "FOO": "bar", "HOME": "/home/x"},
		Validate: &WriteValidate{
			Argv:        []string{"my-validator", "$FOO", "~/y", testPlaceholder},
			Placeholder: testPlaceholder,
		},
	})
	if rpcErr != nil || r.ValidateFailed != nil {
		t.Fatalf("got %+v, %v", r, rpcErr)
	}
	log := f.readLog(t)
	if !strings.Contains(log, "arg=bar\n") || !strings.Contains(log, "arg=/home/x/y\n") {
		t.Errorf("arguments not expanded:\n%s", log)
	}
}

func TestWriteFileValidateRequiresPlaceholder(t *testing.T) {
	dest := filepath.Join(t.TempDir(), "app.conf")
	for name, v := range map[string]*WriteValidate{
		"no placeholder":    {Argv: []string{"/bin/true", "x"}, Placeholder: testPlaceholder},
		"empty placeholder": {Argv: []string{"/bin/true", "x"}},
		"empty argv":        {Placeholder: testPlaceholder},
	} {
		t.Run(name, func(t *testing.T) {
			_, rpcErr := writeFileValidate(t, WriteFileParams{Dest: dest, Content: b64("new\n"), Validate: v})
			if rpcErr == nil {
				t.Fatal("want an error, so the file is never written unvalidated")
			}
			if _, err := os.Stat(dest); !os.IsNotExist(err) {
				t.Error("dest written")
			}
		})
	}
}

func TestWriteFileRejectsUnknownFields(t *testing.T) {
	// A field the agent does not implement must fail the call, not be
	// ignored: an ignored "validate" would install a file unvalidated.
	dest := filepath.Join(t.TempDir(), "app.conf")
	for _, raw := range []string{
		`{"dest":"` + dest + `","content":"` + b64("x") + `","validate":"/usr/sbin/visudo -cf %s"}`,
		`{"dest":"` + dest + `","content":"` + b64("x") + `","bogus":true}`,
	} {
		resp := rpcCallRawParams(t, newTestServer(), "WriteFile", json.RawMessage(raw))
		if resp.Error == nil {
			t.Errorf("params %s accepted", raw)
		}
	}
	if _, err := os.Stat(dest); !os.IsNotExist(err) {
		t.Error("dest written")
	}
}

func TestWriteFileValidateKeepsExtension(t *testing.T) {
	// Observed on stock: the validated file is ".source" plus the
	// destination's extension as os.path.splitext sees it.
	for dest, want := range map[string]string{
		"app.conf":   ".source.conf",
		"plain":      ".source",
		".hidden":    ".source",
		"x.tar.gz":   ".source.gz",
		".env.local": ".source.local",
	} {
		t.Run(dest, func(t *testing.T) {
			f := newValidatorFixture(t)
			if _, rpcErr := writeFileValidate(t, WriteFileParams{
				Dest: filepath.Join(t.TempDir(), dest), Content: b64("new\n"),
				Validate: &WriteValidate{Argv: []string{f.script, testPlaceholder}, Placeholder: testPlaceholder},
			}); rpcErr != nil {
				t.Fatal(rpcErr)
			}
			first, _, _ := strings.Cut(f.readLog(t), "\n")
			if filepath.Base(strings.TrimPrefix(first, "arg=")) != want {
				t.Errorf("validated %q, want basename %q", first, want)
			}
		})
	}
}
