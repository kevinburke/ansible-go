package fastagent

import "syscall"

func statAtime(sys *syscall.Stat_t) int64 {
	return sys.Atimespec.Sec
}
