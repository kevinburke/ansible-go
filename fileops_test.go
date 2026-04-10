package fastagent

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
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
	if result.Size != 5 {
		t.Errorf("got size %d, want 5", result.Size)
	}
	if result.Checksum == "" {
		t.Error("expected checksum to be set")
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
