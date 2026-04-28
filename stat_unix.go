package fastagent

import "golang.org/x/sys/unix"

// Linux and Darwin both expose Atim/Mtim/Ctim Timespec fields on
// unix.Stat_t. The syscall package's Stat_t spelled the Darwin
// variants Atimespec/Mtimespec/Ctimespec, which is the only reason
// we historically needed a per-OS file.
func statAtime(st *unix.Stat_t) int64 { return st.Atim.Sec }
func statMtime(st *unix.Stat_t) int64 { return st.Mtim.Sec }
func statCtime(st *unix.Stat_t) int64 { return st.Ctim.Sec }
