package fastagent

import (
	"os"
	"os/exec"
	"strings"
	"testing"
)

// Opt in to a read-only benchmark against a real systemd host. Both backends
// query fresh active/enabled state; neither starts, stops or changes a unit.
//
//	FASTAGENT_BENCH_SYSTEMD_UNIT=dbus.service go test -run '^$' \
//	  -bench BenchmarkServiceRead -benchtime=200ms -count=20
func BenchmarkServiceRead(b *testing.B) {
	unit := os.Getenv("FASTAGENT_BENCH_SYSTEMD_UNIT")
	if unit == "" {
		b.Skip("set FASTAGENT_BENCH_SYSTEMD_UNIT to an existing unit")
	}
	b.Run("backend=systemctl", func(b *testing.B) {
		b.ReportAllocs()
		for b.Loop() {
			for _, operation := range []string{"is-active", "is-enabled"} {
				out, err := exec.Command("systemctl", operation, unit).Output()
				if len(strings.TrimSpace(string(out))) == 0 {
					b.Fatalf("%s: %v", operation, err)
				}
				if err != nil {
					if e, ok := err.(*exec.ExitError); !ok || (e.ExitCode() != 1 && e.ExitCode() != 3) {
						b.Fatal(err)
					}
				}
			}
		}
	})
	b.Run("backend=dbus", func(b *testing.B) {
		m := &serviceManager{}
		defer m.close()
		if _, err := m.apply(b.Context(), ServiceParams{Name: unit}); err != nil {
			b.Fatal(err)
		}
		b.ReportAllocs()
		for b.Loop() {
			if _, err := m.apply(b.Context(), ServiceParams{Name: unit}); err != nil {
				b.Fatal(err)
			}
		}
	})
}
