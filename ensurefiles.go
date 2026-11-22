package fastagent

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"golang.org/x/sys/unix"
)

// EnsureFilesParams creates regular files and repairs their metadata without
// reading, truncating or timestamping existing contents. Parents must exist.
// All paths are checked before writing. Workers only operate on independent
// files; this is not a way to run arbitrary dependent RPCs concurrently.
type EnsureFilesParams struct {
	Paths     []string `json:"paths"`
	Owner     string   `json:"owner"`
	Group     string   `json:"group"`
	Mode      string   `json:"mode"`
	Workers   int      `json:"workers,omitempty"`
	CheckMode bool     `json:"check_mode,omitempty"`
}

type EnsureFileResult struct {
	Path    string            `json:"path"`
	Changed bool              `json:"changed"`
	Failed  bool              `json:"failed,omitempty"`
	Msg     string            `json:"msg,omitempty"`
	Before  map[string]string `json:"before"`
	After   map[string]string `json:"after"`
}

// EnsureFilesResult includes every outcome, in input order. An application
// failure never hides completed writes, and all workers finish before return.
type EnsureFilesResult struct {
	Changed bool               `json:"changed"`
	Failed  bool               `json:"failed,omitempty"`
	Msg     string             `json:"msg,omitempty"`
	Files   []EnsureFileResult `json:"files"`
}

func (s *Server) handleEnsureFiles(raw json.RawMessage) (any, error) {
	var p EnsureFilesParams
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil {
		return nil, fmt.Errorf("unmarshal EnsureFilesParams: %w", err)
	}
	if p.Workers == 0 {
		p.Workers = 4
	}
	if p.Workers < 1 || p.Workers > 16 {
		return nil, errors.New("workers must be between 1 and 16")
	}
	if len(p.Mode) != 4 || p.Mode[0] != '0' {
		return nil, errors.New("mode must be four octal digits between 0000 and 0777")
	}
	mode, err := strconv.ParseUint(p.Mode, 8, 9)
	if err != nil {
		return nil, fmt.Errorf("invalid mode %q: %w", p.Mode, err)
	}
	uid, err := ensureFileID(p.Owner, false)
	if err != nil {
		return nil, err
	}
	gid, err := ensureFileID(p.Group, true)
	if err != nil {
		return nil, err
	}
	result := EnsureFilesResult{Files: make([]EnsureFileResult, len(p.Paths))}
	seen := make(map[string]bool, len(p.Paths))
	for i, path := range p.Paths {
		if !filepath.IsAbs(path) || strings.ContainsRune(path, 0) {
			return nil, fmt.Errorf("expected an absolute file path: %q", path)
		}
		path = filepath.Clean(path)
		if seen[path] {
			return nil, fmt.Errorf("duplicate file path: %s", path)
		}
		seen[path] = true
		p.Paths[i] = path
		var st unix.Stat_t
		err := unix.Lstat(path, &st)
		changed := false
		before := map[string]string{"path": path, "state": "absent"}
		if errors.Is(err, fs.ErrNotExist) {
			parent, err := os.Stat(filepath.Dir(path))
			if err != nil {
				return nil, fmt.Errorf("parent of %s: %w", path, err)
			}
			if !parent.IsDir() {
				return nil, fmt.Errorf("parent of %s is not a directory", path)
			}
			changed = true
		} else if err != nil {
			return nil, fmt.Errorf("inspect %s: %w", path, err)
		} else if st.Mode&unix.S_IFMT != unix.S_IFREG {
			return nil, fmt.Errorf("refusing symlink or non-regular file: %s", path)
		} else {
			before = map[string]string{"path": path, "state": "file", "owner": strconv.Itoa(int(st.Uid)),
				"group": strconv.Itoa(int(st.Gid)), "mode": fmt.Sprintf("%04o", st.Mode&0o7777)}
			changed = int(st.Uid) != uid || int(st.Gid) != gid || uint32(st.Mode)&0o7777 != uint32(mode)
		}
		result.Files[i] = EnsureFileResult{Path: path, Changed: changed, Before: before,
			After: map[string]string{"path": path, "state": "file", "owner": strconv.Itoa(uid),
				"group": strconv.Itoa(gid), "mode": p.Mode}}
	}
	if !p.CheckMode {
		ensureFilesApply(result.Files, p.Workers, func(path string) (bool, error) {
			return ensureRegularFile(path, uid, gid, fs.FileMode(mode))
		})
	}
	for _, file := range result.Files {
		result.Changed = result.Changed || file.Changed
		result.Failed = result.Failed || file.Failed
	}
	if result.Failed {
		result.Msg = "one or more file operations failed; inspect files for partial changes"
	}
	return result, nil
}

func ensureFileID(name string, group bool) (int, error) {
	parse := func(value string) (int, error) {
		id, err := strconv.ParseUint(value, 10, 32)
		if err != nil || id == 1<<32-1 {
			return 0, fmt.Errorf("invalid ownership ID: %q", value)
		}
		return int(id), nil
	}
	if name != "" && strings.Trim(name, "0123456789") == "" {
		return parse(name)
	}
	var id string
	if group {
		g, err := user.LookupGroup(name)
		if err != nil {
			return 0, fmt.Errorf("group %q: %w", name, err)
		}
		id = g.Gid
	} else {
		u, err := user.Lookup(name)
		if err != nil {
			return 0, fmt.Errorf("owner %q: %w", name, err)
		}
		id = u.Uid
	}
	return parse(id)
}

func ensureFilesApply(files []EnsureFileResult, workers int, apply func(string) (bool, error)) {
	jobs := make(chan int)
	var wg sync.WaitGroup
	for range min(workers, len(files)) {
		wg.Go(func() {
			for i := range jobs {
				changed, err := apply(files[i].Path)
				files[i].Changed = changed
				if err != nil {
					files[i].Failed = true
					files[i].Msg = err.Error()
				}
			}
		})
	}
	for i := range files {
		jobs <- i
	}
	close(jobs)
	wg.Wait()
}

func ensureRegularFile(path string, uid, gid int, mode fs.FileMode) (changed bool, retErr error) {
	// O_EXCL prevents truncation if a service creates the file after preflight.
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|unix.O_NOFOLLOW, mode)
	if errors.Is(err, fs.ErrExist) {
		// NONBLOCK avoids hanging if a regular file races into a FIFO. NOFOLLOW
		// prevents repairing the metadata of a symlink's target instead.
		file, err = os.OpenFile(path, os.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	} else if err == nil {
		changed = true
	}
	if err != nil {
		return changed, err
	}
	defer func() { retErr = errors.Join(retErr, file.Close()) }()
	var st unix.Stat_t
	if err := unix.Fstat(int(file.Fd()), &st); err != nil {
		return changed, err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG {
		return changed, fmt.Errorf("file changed type during reconciliation: %s", path)
	}
	if int(st.Uid) != uid || int(st.Gid) != gid {
		changed = true
		if err := file.Chown(uid, gid); err != nil {
			return changed, err
		}
	}
	if uint32(st.Mode)&0o7777 != uint32(mode) {
		changed = true
		if err := file.Chmod(mode); err != nil {
			return changed, err
		}
	}
	return changed, nil
}
