package fastagent

import "golang.org/x/sys/unix"

// platformStat returns the os.stat_result fields Python exposes on Linux
// beyond the portable set, which ansible.builtin.stat copies through.
func platformStat(st *unix.Stat_t) map[string]int64 {
	return map[string]int64{
		"blocks":      st.Blocks,
		"block_size":  int64(st.Blksize),
		"device_type": int64(st.Rdev),
	}
}
