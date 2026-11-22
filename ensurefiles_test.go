package fastagent

import (
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func ensureTestParams(paths ...string) EnsureFilesParams {
	return EnsureFilesParams{Paths: paths, Owner: strconv.Itoa(os.Getuid()),
		Group: strconv.Itoa(os.Getgid()), Mode: "0644", Workers: 4}
}

func ensureCall(t *testing.T, p EnsureFilesParams) EnsureFilesResult {
	t.Helper()
	resp := rpcCall(t, newTestServer(), "EnsureFiles", p)
	if resp.Error != nil {
		t.Fatal(resp.Error)
	}
	raw, err := json.Marshal(resp.Result)
	if err != nil {
		t.Fatal(err)
	}
	var result EnsureFilesResult
	if err := json.Unmarshal(raw, &result); err != nil {
		t.Fatal(err)
	}
	return result
}

func TestEnsureFilesCreateRepairAndPreserve(t *testing.T) {
	dir := t.TempDir()
	existing, missing := filepath.Join(dir, "existing"), filepath.Join(dir, "missing")
	if err := os.WriteFile(existing, []byte("valuable history\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	old := time.Unix(1234567890, 0)
	if err := os.Chtimes(existing, old, old); err != nil {
		t.Fatal(err)
	}
	var before unix.Stat_t
	if err := unix.Stat(existing, &before); err != nil {
		t.Fatal(err)
	}
	p := ensureTestParams(existing, missing)
	p.CheckMode = true
	result := ensureCall(t, p)
	if !result.Changed || result.Failed || len(result.Files) != 2 {
		t.Fatalf("check result: %+v", result)
	}
	if _, err := os.Stat(missing); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("check created a file: %v", err)
	}
	p.CheckMode = false
	result = ensureCall(t, p)
	if !result.Changed || result.Failed {
		t.Fatalf("apply result: %+v", result)
	}
	var after unix.Stat_t
	if err := unix.Stat(existing, &after); err != nil {
		t.Fatal(err)
	}
	if before.Atim != after.Atim || before.Mtim != after.Mtim || after.Mode&0o777 != 0o644 {
		t.Fatalf("timestamps or mode changed incorrectly: before=%+v after=%+v", before, after)
	}
	data, err := os.ReadFile(existing)
	if err != nil || string(data) != "valuable history\n" {
		t.Fatalf("contents=%q err=%v", data, err)
	}
	result = ensureCall(t, p)
	if result.Changed || result.Failed {
		t.Fatalf("second run: %+v", result)
	}
}

func TestEnsureFilesRejectsLateInvalidInputBeforeWriting(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "missing")
	symlink := filepath.Join(dir, "symlink")
	if err := os.Symlink(missing, symlink); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{symlink, dir, "relative", missing, filepath.Join(dir, "absent", "child")} {
		p := ensureTestParams(missing, path)
		resp := rpcCall(t, newTestServer(), "EnsureFiles", p)
		if resp.Error == nil {
			t.Errorf("accepted invalid path %q", path)
		}
		if _, err := os.Stat(missing); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("wrote before validation completed: %v", err)
		}
	}
}

func TestEnsureFilesRejectsUnknownFieldsAndInvalidOptions(t *testing.T) {
	p := ensureTestParams(filepath.Join(t.TempDir(), "file"))
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	var params map[string]any
	if err := json.Unmarshal(raw, &params); err != nil {
		t.Fatal(err)
	}
	for key, values := range map[string][]any{
		"workers": {-1, 17}, "mode": {"0648", "644", "4644"},
		"owner": {"caracal-no-such-account-ensure-test", "4294967295", "4294967296"}, "unexpected": {true},
	} {
		for _, value := range values {
			copy := make(map[string]any, len(params)+1)
			maps.Copy(copy, params)
			copy[key] = value
			if resp := rpcCall(t, newTestServer(), "EnsureFiles", copy); resp.Error == nil {
				t.Errorf("accepted %s=%v", key, value)
			}
		}
	}
}

func TestEnsureFilesWorkersAreBoundedAndReportEveryOutcome(t *testing.T) {
	files := make([]EnsureFileResult, 12)
	for i := range files {
		files[i].Path = strconv.Itoa(i)
	}
	entered := make(chan struct{}, len(files))
	release := make(chan struct{})
	done := make(chan struct{})
	var active, maxActive atomic.Int32
	go func() {
		ensureFilesApply(files, 3, func(path string) (bool, error) {
			n := active.Add(1)
			for prev := maxActive.Load(); n > prev; prev = maxActive.Load() {
				if maxActive.CompareAndSwap(prev, n) {
					break
				}
			}
			entered <- struct{}{}
			<-release
			active.Add(-1)
			if path == "2" {
				return true, errors.New("failed after changing metadata")
			}
			return true, nil
		})
		close(done)
	}()
	for range 3 {
		select {
		case <-entered:
		case <-time.After(5 * time.Second):
			close(release)
			t.Fatal("workers did not run concurrently")
		}
	}
	close(release)
	<-done
	if maxActive.Load() != 3 || active.Load() != 0 {
		t.Fatalf("active=%d max=%d", active.Load(), maxActive.Load())
	}
	for i, file := range files {
		if file.Path != strconv.Itoa(i) || !file.Changed || file.Failed != (i == 2) {
			t.Errorf("result %d: %+v", i, file)
		}
	}
	if files[2].Msg == "" {
		t.Fatal("partial failure lost its diagnostic")
	}
}

func TestEnsureRegularFileRefusesSymlinkAndFIFOAfterPreflight(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "target")
	if err := os.WriteFile(target, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	fifo := filepath.Join(dir, "fifo")
	if err := unix.Mkfifo(fifo, 0o600); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{link, fifo} {
		if changed, err := ensureRegularFile(path, os.Getuid(), os.Getgid(), 0o644); err == nil || changed {
			t.Errorf("path %s: changed=%v err=%v", path, changed, err)
		}
	}
	info, err := os.Stat(target)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("symlink target was modified: %v %v", info, err)
	}
}

func BenchmarkEnsureFiles(b *testing.B) {
	for _, workers := range []int{1, 4} {
		b.Run(fmt.Sprintf("100_existing/workers_%d", workers), func(b *testing.B) {
			dir := b.TempDir()
			p := ensureTestParams()
			p.Workers = workers
			for i := range 100 {
				path := filepath.Join(dir, strconv.Itoa(i))
				if err := os.WriteFile(path, nil, 0o644); err != nil {
					b.Fatal(err)
				}
				p.Paths = append(p.Paths, path)
			}
			raw, err := json.Marshal(p)
			if err != nil {
				b.Fatal(err)
			}
			s := newTestServer()
			for b.Loop() {
				result, err := s.handleEnsureFiles(raw)
				if err != nil || result.(EnsureFilesResult).Changed {
					b.Fatalf("result=%+v err=%v", result, err)
				}
			}
		})
	}
}
