//go:build !linux && !darwin

package fastagent

import "golang.org/x/sys/unix"

// platformStat reports no platform-dependent fields on systems whose
// Python os.stat_result layout we have not mapped.
func platformStat(st *unix.Stat_t) map[string]int64 { return nil }
