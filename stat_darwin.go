package fastagent

import "golang.org/x/sys/unix"

// platformStat returns the os.stat_result fields Python exposes on macOS
// beyond the portable set, which ansible.builtin.stat copies through.
// birthtime is sent as seconds and nanoseconds; the action plugin builds
// the float.
func platformStat(st *unix.Stat_t) map[string]int64 {
	return map[string]int64{
		"blocks":         st.Blocks,
		"block_size":     int64(st.Blksize),
		"device_type":    int64(st.Rdev),
		"flags":          int64(st.Flags),
		"generation":     int64(st.Gen),
		"birthtime_sec":  st.Btim.Sec,
		"birthtime_nsec": st.Btim.Nsec,
	}
}
