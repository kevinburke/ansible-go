package fastagent

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestStatExistingFile(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	path := filepath.Join(tmp, "test.txt")
	if err := os.WriteFile(path, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}

	resp := rpcCall(t, s, "Stat", StatParams{
		Path:     path,
		Checksum: true,
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result StatResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Exists {
		t.Error("expected exists=true")
	}
	if result.IsDir {
		t.Error("expected isdir=false")
	}
	if !result.IsReg {
		t.Error("expected isreg=true for a regular file")
	}
	if result.Size != 5 {
		t.Errorf("got size %d, want 5", result.Size)
	}
	if result.Checksum == "" {
		t.Error("expected checksum to be set")
	}
}

// TestStatSynthesizedFields guards against a regression where the
// fastagent stat action plugin crashed playbooks with
// "object of type 'dict' has no attribute 'executable'" because the
// Go agent didn't return the per-bit booleans, uid/gid, nlink/inode,
// or the access(2)-derived readable/writeable/executable that
// ansible.builtin.stat sets.
func TestStatSynthesizedFields(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	exe := filepath.Join(tmp, "exec")
	if err := os.WriteFile(exe, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	plain := filepath.Join(tmp, "plain")
	if err := os.WriteFile(plain, []byte("data"), 0o644); err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name           string
		path           string
		wantExecutable bool
		wantXUsr       bool
	}{
		{name: "0755_executable", path: exe, wantExecutable: true, wantXUsr: true},
		{name: "0644_not_executable", path: plain, wantExecutable: false, wantXUsr: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp := rpcCall(t, s, "Stat", StatParams{Path: tc.path})
			if resp.Error != nil {
				t.Fatalf("unexpected error: %v", resp.Error)
			}
			resultJSON, _ := json.Marshal(resp.Result)
			var result StatResult
			if err := json.Unmarshal(resultJSON, &result); err != nil {
				t.Fatal(err)
			}

			if !result.Exists || !result.IsReg {
				t.Fatalf("expected an existing regular file, got %+v", result)
			}
			if result.UID != os.Getuid() {
				t.Errorf("uid: got %d, want %d", result.UID, os.Getuid())
			}
			if result.GID != os.Getgid() {
				t.Errorf("gid: got %d, want %d", result.GID, os.Getgid())
			}
			if result.Nlink == 0 {
				t.Error("nlink should be at least 1")
			}
			if result.Inode == 0 {
				t.Error("inode should be non-zero")
			}
			if result.Ctime == 0 {
				t.Error("ctime should be non-zero")
			}
			if !result.RUsr || !result.WUsr {
				t.Errorf("rusr/wusr should be true on owner-writable file: rusr=%v wusr=%v", result.RUsr, result.WUsr)
			}
			if result.WOth {
				t.Error("woth should be false on a 0o644/0o755 file")
			}
			if !result.Readable {
				t.Error("readable should be true for an owned regular file")
			}
			if result.Executable != tc.wantExecutable {
				t.Errorf("executable: got %v, want %v", result.Executable, tc.wantExecutable)
			}
			if result.XUsr != tc.wantXUsr {
				t.Errorf("xusr: got %v, want %v", result.XUsr, tc.wantXUsr)
			}
		})
	}
}

// TestStatSymlinkTargetAndSource exercises both lnk_target (raw
// readlink result) and lnk_source (resolved realpath). The previous
// implementation populated lnk_source with the readlink output and
// shipped no lnk_target at all; the action plugin then aliased
// lnk_target to lnk_source, masking the divergence.
func TestStatSymlinkTargetAndSource(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	target := filepath.Join(tmp, "target.txt")
	if err := os.WriteFile(target, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(tmp, "link")
	if err := os.Symlink("target.txt", link); err != nil { // relative target
		t.Fatal(err)
	}

	resp := rpcCall(t, s, "Stat", StatParams{Path: link})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}
	resultJSON, _ := json.Marshal(resp.Result)
	var result StatResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.IsLink {
		t.Fatal("expected islnk=true")
	}
	if result.LnkTarget != "target.txt" {
		t.Errorf("lnk_target: got %q, want %q (raw readlink result)", result.LnkTarget, "target.txt")
	}
	wantSource, err := filepath.EvalSymlinks(link)
	if err != nil {
		t.Fatal(err)
	}
	if result.LnkSource != wantSource {
		t.Errorf("lnk_source: got %q, want %q (resolved realpath)", result.LnkSource, wantSource)
	}
}

func TestStatNonexistent(t *testing.T) {
	s := newTestServer()

	resp := rpcCall(t, s, "Stat", StatParams{
		Path: "/nonexistent-path-that-does-not-exist",
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result StatResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if result.Exists {
		t.Error("expected exists=false")
	}
}

func TestStatDirectory(t *testing.T) {
	s := newTestServer()

	resp := rpcCall(t, s, "Stat", StatParams{
		Path: t.TempDir(),
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result StatResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Exists {
		t.Error("expected exists=true")
	}
	if !result.IsDir {
		t.Error("expected isdir=true")
	}
}

func TestReadFile(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	path := filepath.Join(tmp, "test.txt")
	content := "hello world\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	resp := rpcCall(t, s, "ReadFile", ReadFileParams{Path: path})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result ReadFileResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}

	decoded, err := base64.StdEncoding.DecodeString(result.Content)
	if err != nil {
		t.Fatal(err)
	}
	if string(decoded) != content {
		t.Errorf("got content %q, want %q", string(decoded), content)
	}
	if result.Size != int64(len(content)) {
		t.Errorf("got size %d, want %d", result.Size, len(content))
	}
}

func TestWriteFileNew(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	dest := filepath.Join(tmp, "output.txt")
	content := "new file content"
	b64 := base64.StdEncoding.EncodeToString([]byte(content))

	resp := rpcCall(t, s, "WriteFile", WriteFileParams{
		Dest:    dest,
		Content: b64,
		Mode:    "0644",
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result WriteFileResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Changed {
		t.Error("expected changed=true for new file")
	}

	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != content {
		t.Errorf("got file content %q, want %q", string(got), content)
	}
}

func TestWriteFileIdempotent(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	dest := filepath.Join(tmp, "output.txt")
	content := "idempotent content"
	b64 := base64.StdEncoding.EncodeToString([]byte(content))

	// Write once.
	resp := rpcCall(t, s, "WriteFile", WriteFileParams{
		Dest:    dest,
		Content: b64,
	})
	if resp.Error != nil {
		t.Fatalf("first write: unexpected error: %v", resp.Error)
	}

	// Write again with same content.
	resp = rpcCall(t, s, "WriteFile", WriteFileParams{
		Dest:    dest,
		Content: b64,
	})
	if resp.Error != nil {
		t.Fatalf("second write: unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result WriteFileResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if result.Changed {
		t.Error("expected changed=false for idempotent write")
	}
}

func TestWriteFileBackup(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	dest := filepath.Join(tmp, "output.txt")
	if err := os.WriteFile(dest, []byte("original"), 0o644); err != nil {
		t.Fatal(err)
	}

	b64 := base64.StdEncoding.EncodeToString([]byte("updated"))
	resp := rpcCall(t, s, "WriteFile", WriteFileParams{
		Dest:    dest,
		Content: b64,
		Backup:  true,
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result WriteFileResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if result.BackupFile == "" {
		t.Error("expected backup_file to be set")
	}
	if result.BackupFile != "" {
		backup, err := os.ReadFile(result.BackupFile)
		if err != nil {
			t.Fatal(err)
		}
		if string(backup) != "original" {
			t.Errorf("backup content %q, want %q", string(backup), "original")
		}
	}
}

// TestWriteFileCreatesMissingIntermediates guards against a regression where
// WriteFile called os.MkdirAll with a fixed 0o755 and no ownership. When the
// daemon runs as root (become is in effect), any intermediate it creates
// under a non-root user's home ends up owned by root:root, which breaks the
// next stock-ssh ansible run with "mkdir ~/.ansible/tmp/ansible-tmp-*:
// Permission denied". Pre-existing ancestors must be left untouched so we
// don't retroactively change the mode on a user-managed directory.
func TestWriteFileCreatesMissingIntermediates(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	preexisting := filepath.Join(tmp, "pre")
	if err := os.Mkdir(preexisting, 0o700); err != nil {
		t.Fatal(err)
	}

	dest := filepath.Join(preexisting, "a", "b", "c", "file.txt")
	b64 := base64.StdEncoding.EncodeToString([]byte("hello"))
	resp := rpcCall(t, s, "WriteFile", WriteFileParams{
		Dest:    dest,
		Content: b64,
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	data, err := os.ReadFile(dest)
	if err != nil {
		t.Fatalf("read %s: %v", dest, err)
	}
	if string(data) != "hello" {
		t.Errorf("got %q, want %q", string(data), "hello")
	}

	// Pre-existing ancestor keeps its original mode.
	info, err := os.Stat(preexisting)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0o700 {
		t.Errorf("%s: mode changed to %#o, want %#o (pre-existing ancestor must be left alone)",
			preexisting, got, 0o700)
	}

	// Every newly-created intermediate exists and is a directory.
	for _, seg := range []string{"a", "a/b", "a/b/c"} {
		path := filepath.Join(preexisting, seg)
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat %s: %v", path, err)
		}
		if !info.IsDir() {
			t.Errorf("%s: not a directory", path)
		}
	}
}

func TestFileDirectory(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	dir := filepath.Join(tmp, "subdir", "nested")

	resp := rpcCall(t, s, "File", FileParams{
		Path:  dir,
		State: "directory",
		Mode:  "0755",
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result FileResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Changed {
		t.Error("expected changed=true for new directory")
	}

	info, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if !info.IsDir() {
		t.Error("expected directory")
	}
}

// TestFileDirectoryAppliesModeToNewIntermediates mirrors stock ansible's
// ensure_directory: every intermediate the task creates receives the task's
// owner/group/mode (via set_fs_attributes_if_different). Without this, a task
// like `file: path=/home/svc/etc/foo/env owner=svc mode=0750` would leave
// /home/svc/etc/foo owned by the agent's uid (typically root), breaking
// access by the svc user.
func TestFileDirectoryAppliesModeToNewIntermediates(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	a := filepath.Join(tmp, "a")
	b := filepath.Join(a, "b")
	c := filepath.Join(b, "c")

	resp := rpcCall(t, s, "File", FileParams{
		Path:  c,
		State: "directory",
		Mode:  "0700",
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	for _, path := range []string{a, b, c} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat %s: %v", path, err)
		}
		if got := info.Mode().Perm(); got != 0o700 {
			t.Errorf("%s: got mode %#o, want %#o", path, got, 0o700)
		}
	}
}

// TestFileDirectoryLeavesExistingAncestorsAlone mirrors ansible's behavior:
// set_fs_attributes_if_different is only called inside the `if not
// os.path.exists(b_curpath):` branch, so ancestors that already exist keep
// their current mode/owner even if the task has a different mode set.
func TestFileDirectoryLeavesExistingAncestorsAlone(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	a := filepath.Join(tmp, "a")
	b := filepath.Join(a, "b")
	c := filepath.Join(b, "c")

	if err := os.Mkdir(a, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(a, 0o755); err != nil {
		t.Fatal(err)
	}

	resp := rpcCall(t, s, "File", FileParams{
		Path:  c,
		State: "directory",
		Mode:  "0700",
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	cases := []struct {
		path string
		want os.FileMode
	}{
		{a, 0o755}, // pre-existing, untouched
		{b, 0o700}, // newly created, task mode applied
		{c, 0o700}, // leaf, task mode applied
	}
	for _, tc := range cases {
		info, err := os.Stat(tc.path)
		if err != nil {
			t.Fatalf("stat %s: %v", tc.path, err)
		}
		if got := info.Mode().Perm(); got != tc.want {
			t.Errorf("%s: got mode %#o, want %#o", tc.path, got, tc.want)
		}
	}
}

func TestFileDirectoryRecurseAppliesModeToChildren(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	dir := filepath.Join(tmp, "repo")
	nested := filepath.Join(dir, "github.com", "example.git")
	file := filepath.Join(nested, "HEAD")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(file, []byte("ref: refs/heads/main\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(nested, 0o755); err != nil {
		t.Fatal(err)
	}

	resp := rpcCall(t, s, "File", FileParams{
		Path:    dir,
		State:   "directory",
		Mode:    "0700",
		Recurse: true,
		Follow:  true,
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result FileResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Changed {
		t.Error("expected changed=true")
	}

	for _, path := range []string{dir, filepath.Join(dir, "github.com"), nested, file} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat %s: %v", path, err)
		}
		if got := info.Mode().Perm(); got != 0o700 {
			t.Errorf("%s: got mode %#o, want %#o", path, got, 0o700)
		}
	}
}

func TestFileDirectoryRecurseSkipsSymlinksWhenFollowFalse(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	dir := filepath.Join(tmp, "repo")
	targetDir := filepath.Join(tmp, "target")
	targetFile := filepath.Join(targetDir, "data")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(targetDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(targetFile, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(targetDir, filepath.Join(dir, "linked-dir")); err != nil {
		t.Fatal(err)
	}

	resp := rpcCall(t, s, "File", FileParams{
		Path:    dir,
		State:   "directory",
		Mode:    "0700",
		Recurse: true,
		Follow:  false,
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	info, err := os.Stat(targetFile)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0o600 {
		t.Errorf("symlink target got mode %#o, want %#o", got, 0o600)
	}
}

func TestFileAbsent(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	path := filepath.Join(tmp, "to-remove.txt")
	if err := os.WriteFile(path, []byte("delete me"), 0o644); err != nil {
		t.Fatal(err)
	}

	resp := rpcCall(t, s, "File", FileParams{
		Path:  path,
		State: "absent",
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result FileResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Changed {
		t.Error("expected changed=true")
	}

	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Error("expected file to be removed")
	}
}

func TestFileTouch(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	path := filepath.Join(tmp, "touched.txt")

	resp := rpcCall(t, s, "File", FileParams{
		Path:  path,
		State: "touch",
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result FileResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Changed {
		t.Error("expected changed=true")
	}

	if _, err := os.Stat(path); err != nil {
		t.Errorf("expected file to exist: %v", err)
	}
}

func TestFileSymlink(t *testing.T) {
	s := newTestServer()

	tmp := t.TempDir()
	src := filepath.Join(tmp, "source.txt")
	if err := os.WriteFile(src, []byte("source"), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(tmp, "link.txt")

	resp := rpcCall(t, s, "File", FileParams{
		Path:  link,
		State: "link",
		Src:   src,
	})
	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result FileResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Changed {
		t.Error("expected changed=true")
	}

	dest, err := os.Readlink(link)
	if err != nil {
		t.Fatal(err)
	}
	if dest != src {
		t.Errorf("link dest %q, want %q", dest, src)
	}
}

// TestApplyOwnershipNumericIDs mirrors ansible's file module: numeric strings
// passed as `owner`/`group` are treated as literal UIDs/GIDs and not looked
// up in /etc/passwd. Without this, tasks like
// `file: path=/var/data state=directory owner='1001' group='1001'` fail with
// `lookup user "1001": user: unknown user 1001` whenever the UID has no
// passwd entry (common for container/podman ranges).
func TestApplyOwnershipNumericIDs(t *testing.T) {
	tmp := t.TempDir()
	path := filepath.Join(tmp, "f")
	if err := os.WriteFile(path, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	uid := strconv.Itoa(os.Getuid())
	gid := strconv.Itoa(os.Getgid())

	if _, err := applyOwnershipAndMode(path, uid, gid, ""); err != nil {
		t.Fatalf("numeric uid/gid (matching current): %v", err)
	}

	// A numeric UID with no passwd entry must also parse cleanly. The chown
	// itself is skipped because the file already matches the current uid, so
	// this exercises the resolution path without requiring root.
	bogus := "4000000001"
	if err := os.Chown(path, os.Getuid(), os.Getgid()); err != nil {
		t.Fatal(err)
	}
	if _, err := applyOwnershipAndMode(path, bogus, bogus, ""); err == nil {
		// Non-root: chown will fail with EPERM, which is expected and not the
		// bug we're guarding against. The bug we're guarding against is
		// `lookup user "4000000001"` — a name-resolution failure.
		t.Logf("ran as root or chown unexpectedly succeeded; that's fine")
	} else if isLookupError(err) {
		t.Fatalf("numeric uid/gid should not be name-resolved: %v", err)
	}
}

func isLookupError(err error) bool {
	s := err.Error()
	return strings.Contains(s, "lookup user") || strings.Contains(s, "lookup group")
}
