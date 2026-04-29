package fastagent

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestAptCacheFresh(t *testing.T) {
	now := time.Date(2026, 4, 28, 12, 0, 0, 0, time.UTC)
	valid := 60 * time.Second

	cases := []struct {
		name         string
		updatedAt    time.Time
		sourcesMTime time.Time
		want         bool
	}{
		{
			name: "zero updatedAt is never fresh",
			want: false,
		},
		{
			name:      "within window, no sources",
			updatedAt: now.Add(-30 * time.Second),
			want:      true,
		},
		{
			name:      "outside window",
			updatedAt: now.Add(-90 * time.Second),
			want:      false,
		},
		{
			name:         "sources older than cache: fresh",
			updatedAt:    now.Add(-30 * time.Second),
			sourcesMTime: now.Add(-5 * time.Minute),
			want:         true,
		},
		{
			name:         "sources newer than cache: stale even within window",
			updatedAt:    now.Add(-30 * time.Second),
			sourcesMTime: now.Add(-10 * time.Second),
			want:         false,
		},
		{
			name:         "sources mtime equal to cache: fresh (After is strict)",
			updatedAt:    now.Add(-30 * time.Second),
			sourcesMTime: now.Add(-30 * time.Second),
			want:         true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := aptCacheFresh(tc.updatedAt, tc.sourcesMTime, now, valid)
			if got != tc.want {
				t.Errorf("aptCacheFresh = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestLatestAptSourcesMTime(t *testing.T) {
	dir := t.TempDir()
	listPath := filepath.Join(dir, "sources.list")
	listD := filepath.Join(dir, "sources.list.d")
	if err := os.Mkdir(listD, 0o755); err != nil {
		t.Fatal(err)
	}

	old := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	mid := time.Date(2026, 2, 1, 0, 0, 0, 0, time.UTC)
	newest := time.Date(2026, 3, 1, 0, 0, 0, 0, time.UTC)

	if err := os.WriteFile(listPath, []byte("# main\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(listPath, old, old); err != nil {
		t.Fatal(err)
	}

	a := filepath.Join(listD, "a.sources")
	if err := os.WriteFile(a, []byte("a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(a, mid, mid); err != nil {
		t.Fatal(err)
	}

	b := filepath.Join(listD, "tailscale.sources")
	if err := os.WriteFile(b, []byte("b\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(b, newest, newest); err != nil {
		t.Fatal(err)
	}
	// Force the directory mtime to be old so the file mtime drives the result.
	if err := os.Chtimes(listD, old, old); err != nil {
		t.Fatal(err)
	}

	got := latestAptSourcesMTime(listPath, listD)
	if !got.Equal(newest) {
		t.Errorf("latestAptSourcesMTime = %v, want %v", got, newest)
	}

	// Missing paths must be tolerated and return zero.
	zero := latestAptSourcesMTime(filepath.Join(dir, "nope"), filepath.Join(dir, "nope.d"))
	if !zero.IsZero() {
		t.Errorf("missing paths: got %v, want zero", zero)
	}
}

// TestAptSkipBypassedByNewerSources reproduces the deb822_repository bug:
// after writing a new sources file, an in-window cache must still trigger
// apt-get update.
func TestAptSkipBypassedByNewerSources(t *testing.T) {
	dir := t.TempDir()
	listPath := filepath.Join(dir, "sources.list")
	listD := filepath.Join(dir, "sources.list.d")
	if err := os.Mkdir(listD, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(listPath, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}

	cacheUpdated := time.Now().Add(-30 * time.Second)
	// Touch a fresh .sources file as if deb822_repository just wrote it.
	newSrc := filepath.Join(listD, "tailscale.sources")
	if err := os.WriteFile(newSrc, []byte("Types: deb\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	if err := os.Chtimes(newSrc, now, now); err != nil {
		t.Fatal(err)
	}

	mtime := latestAptSourcesMTime(listPath, listD)
	if mtime.Before(cacheUpdated) {
		t.Fatalf("test setup: sources mtime %v older than cache time %v", mtime, cacheUpdated)
	}
	if aptCacheFresh(cacheUpdated, mtime, now, 60*time.Second) {
		t.Error("expected cache to be considered stale after newer .sources file written")
	}
}
