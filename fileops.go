package fastagent

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

func (s *Server) handleStat(params json.RawMessage) (any, error) {
	var p StatParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal StatParams: %w", err)
	}
	// Stat running as the agent's uid (typically root) could leak the
	// existence or metadata of files the BecomeUser couldn't see. We
	// should support this at some point — probably by stat'ing from a
	// helper subprocess that has dropped to BecomeUser — but we
	// haven't built that yet. Until then, refuse rather than silently
	// upgrade permissions. Callers fall back to `stat` via the Exec
	// RPC, which does support BecomeUser.
	if p.BecomeUser != "" {
		return nil, fmt.Errorf("stat: BecomeUser is not yet implemented (use Exec with `stat`/`test` to run as a specific user)")
	}

	var st unix.Stat_t
	statFn := unix.Lstat
	if p.Follow {
		statFn = unix.Stat
	}
	if err := statFn(p.Path, &st); err != nil {
		if errors.Is(err, unix.ENOENT) {
			return StatResult{Exists: false, Path: p.Path}, nil
		}
		return nil, fmt.Errorf("stat %s: %w", p.Path, err)
	}

	mode := fs.FileMode(st.Mode & 0o7777)
	// Translate the type bits from the raw st_mode into Go's
	// fs.FileMode convention so we can reuse IsDir/IsRegular/etc.
	switch st.Mode & unix.S_IFMT {
	case unix.S_IFDIR:
		mode |= fs.ModeDir
	case unix.S_IFLNK:
		mode |= fs.ModeSymlink
	case unix.S_IFIFO:
		mode |= fs.ModeNamedPipe
	case unix.S_IFSOCK:
		mode |= fs.ModeSocket
	case unix.S_IFBLK:
		mode |= fs.ModeDevice
	case unix.S_IFCHR:
		mode |= fs.ModeDevice | fs.ModeCharDevice
	}
	if st.Mode&unix.S_ISUID != 0 {
		mode |= fs.ModeSetuid
	}
	if st.Mode&unix.S_ISGID != 0 {
		mode |= fs.ModeSetgid
	}

	perm := mode.Perm()
	result := StatResult{
		Exists:   true,
		Path:     p.Path,
		IsDir:    mode.IsDir(),
		IsLink:   mode&fs.ModeSymlink != 0,
		IsReg:    mode.IsRegular(),
		IsBlock:  mode&fs.ModeDevice != 0 && mode&fs.ModeCharDevice == 0,
		IsChar:   mode&fs.ModeCharDevice != 0,
		IsFIFO:   mode&fs.ModeNamedPipe != 0,
		IsSocket: mode&fs.ModeSocket != 0,
		Mode:     fmt.Sprintf("0%o", perm),
		Size:     st.Size,
		UID:      int(st.Uid),
		GID:      int(st.Gid),
		Inode:    uint64(st.Ino),
		Dev:      uint64(st.Dev),
		Nlink:    uint64(st.Nlink),
		Atime:    statAtime(&st),
		Mtime:    statMtime(&st),
		Ctime:    statCtime(&st),

		IsUID: mode&fs.ModeSetuid != 0,
		IsGID: mode&fs.ModeSetgid != 0,
		RUsr:  perm&0o400 != 0,
		WUsr:  perm&0o200 != 0,
		XUsr:  perm&0o100 != 0,
		RGrp:  perm&0o040 != 0,
		WGrp:  perm&0o020 != 0,
		XGrp:  perm&0o010 != 0,
		ROth:  perm&0o004 != 0,
		WOth:  perm&0o002 != 0,
		XOth:  perm&0o001 != 0,
	}
	if u, err := user.LookupId(strconv.Itoa(result.UID)); err == nil {
		result.Owner = u.Username
	}
	if g, err := user.LookupGroupId(strconv.Itoa(result.GID)); err == nil {
		result.Group = g.Name
	}

	// access(2) honors mount-time flags like noexec/ro that the mode
	// bits can't reveal, so we ask the kernel rather than computing
	// from `perm` directly. This matches ansible.builtin.stat, which
	// uses os.access for these three fields.
	result.Readable = unix.Access(p.Path, unix.R_OK) == nil
	result.Writeable = unix.Access(p.Path, unix.W_OK) == nil
	result.Executable = unix.Access(p.Path, unix.X_OK) == nil

	if mode&fs.ModeSymlink != 0 {
		if target, err := os.Readlink(p.Path); err == nil {
			result.LnkTarget = target
		}
		if src, err := filepath.EvalSymlinks(p.Path); err == nil {
			result.LnkSource = src
		}
	}

	if p.Checksum && mode.IsRegular() {
		checksum, err := sha256File(p.Path)
		if err != nil {
			return nil, fmt.Errorf("checksum %s: %w", p.Path, err)
		}
		result.Checksum = checksum
	}

	return result, nil
}

func (s *Server) handleReadFile(params json.RawMessage) (any, error) {
	var p ReadFileParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal ReadFileParams: %w", err)
	}
	// Same rationale as Stat: reading as the agent's uid could expose
	// file contents BecomeUser wouldn't have read access to. We should
	// support this eventually (likely via a helper subprocess that
	// drops to BecomeUser), but haven't built it yet.
	if p.BecomeUser != "" {
		return nil, fmt.Errorf("read_file: BecomeUser is not yet implemented (use Exec with `cat` to read as a specific user)")
	}

	data, err := os.ReadFile(p.Path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", p.Path, err)
	}

	return ReadFileResult{
		Content: base64.StdEncoding.EncodeToString(data),
		Size:    int64(len(data)),
	}, nil
}

func (s *Server) handleWriteFile(params json.RawMessage) (any, error) {
	var p WriteFileParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal WriteFileParams: %w", err)
	}

	data, err := base64.StdEncoding.DecodeString(p.Content)
	if err != nil {
		return nil, fmt.Errorf("decode content: %w", err)
	}

	// Compute checksum of new content.
	h := sha256.Sum256(data)
	newChecksum := hex.EncodeToString(h[:])

	// If the caller already knows the existing file's checksum, use it to
	// skip the disk read. Otherwise read the file to check.
	existingChecksum := p.Checksum
	if existingChecksum == "" {
		existingChecksum, _ = sha256File(p.Dest)
	}

	if existingChecksum == newChecksum {
		// File already has the correct content; still apply ownership/mode if needed.
		changed, err := applyOwnershipAndMode(p.Dest, p.Owner, p.Group, p.Mode)
		if err != nil {
			return nil, err
		}
		return WriteFileResult{
			Changed:  changed,
			Dest:     p.Dest,
			Checksum: newChecksum,
		}, nil
	}

	// Backup existing file if requested.
	var backupFile string
	if p.Backup {
		if _, statErr := os.Stat(p.Dest); statErr == nil {
			backupFile = p.Dest + "." + time.Now().Format("20060102150405") + "~"
			if err := copyFile(p.Dest, backupFile); err != nil {
				return nil, fmt.Errorf("backup %s: %w", p.Dest, err)
			}
		}
	}

	// Write atomically: temp file + rename.
	//
	// Create any missing parent dirs with the target file's owner/group, not
	// the daemon's uid. When become is in effect the daemon runs as root, so a
	// plain os.MkdirAll(dir, 0o755) leaves every intermediate it creates
	// owned by root:root. That breaks the very next stock-ssh Ansible run:
	// ( umask 77 && mkdir -p ~/.ansible/tmp && mkdir ~/.ansible/tmp/ansible-tmp-<ts> )
	// fails with EACCES because kevin no longer has write on his own
	// ~/.ansible/tmp/. We only chown segments we actually create; existing
	// ancestors are left untouched (matching ansible's file module).
	dir := filepath.Dir(p.Dest)
	if err := mkdirAllOwned(dir, p.Owner, p.Group); err != nil {
		return nil, err
	}

	if p.UnsafeWrites {
		if err := os.WriteFile(p.Dest, data, 0o644); err != nil {
			return nil, fmt.Errorf("write %s: %w", p.Dest, err)
		}
	} else {
		tmp, err := os.CreateTemp(dir, ".fastagent-*")
		if err != nil {
			return nil, fmt.Errorf("create temp: %w", err)
		}
		tmpName := tmp.Name()
		if _, err := tmp.Write(data); err != nil {
			tmp.Close()
			os.Remove(tmpName)
			return nil, fmt.Errorf("write temp: %w", err)
		}
		if err := tmp.Close(); err != nil {
			os.Remove(tmpName)
			return nil, fmt.Errorf("close temp: %w", err)
		}
		if err := os.Rename(tmpName, p.Dest); err != nil {
			os.Remove(tmpName)
			return nil, fmt.Errorf("rename temp to %s: %w", p.Dest, err)
		}
	}

	if _, err := applyOwnershipAndMode(p.Dest, p.Owner, p.Group, p.Mode); err != nil {
		return nil, err
	}

	return WriteFileResult{
		Changed:    true,
		Dest:       p.Dest,
		Checksum:   newChecksum,
		BackupFile: backupFile,
	}, nil
}

func (s *Server) handleFile(params json.RawMessage) (any, error) {
	var p FileParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal FileParams: %w", err)
	}

	switch p.State {
	case "directory":
		return s.handleFileDirectory(p)
	case "file":
		return s.handleFileFile(p)
	case "link", "hard":
		return s.handleFileLink(p)
	case "touch":
		return s.handleFileTouch(p)
	case "absent":
		return s.handleFileAbsent(p)
	default:
		return nil, fmt.Errorf("unknown file state: %q", p.State)
	}
}

func (s *Server) handleFileDirectory(p FileParams) (any, error) {
	changed, err := ensureDirectoryAnsible(p.Path, p.Owner, p.Group, p.Mode)
	if err != nil {
		return nil, err
	}
	return FileResult{Changed: changed, Path: p.Path, State: "directory"}, nil
}

// ensureDirectoryAnsible creates path and any missing ancestors, applying
// owner/group/mode to each ancestor it actually creates plus the leaf.
// Ancestors that already exist are left untouched. This matches stock
// ansible's file module; see ensure_directory() in
// https://github.com/ansible/ansible/blob/devel/lib/ansible/modules/file.py
// (the per-segment `if not os.path.exists(b_curpath):` block that calls
// os.mkdir followed by set_fs_attributes_if_different).
//
// The earlier implementation used os.MkdirAll with a fixed 0755, which left
// intermediates owned by the agent's uid (typically root) instead of the
// task's owner. Tasks like `file: path=/home/homeauto/etc/foo/env
// owner=homeauto mode=0750` would silently create /home/homeauto/etc/foo as
// root:root 0755, breaking any subsequent access by the homeauto user.
func ensureDirectoryAnsible(path, owner, group, mode string) (bool, error) {
	info, err := os.Stat(path)
	if err == nil {
		if !info.IsDir() {
			return false, fmt.Errorf("%s exists but is not a directory", path)
		}
		// Path already exists: ansible only touches the leaf's attrs,
		// not any ancestor's.
		return applyOwnershipAndMode(path, owner, group, mode)
	}
	if !os.IsNotExist(err) {
		return false, fmt.Errorf("stat %s: %w", path, err)
	}

	segments := strings.Split(strings.Trim(path, "/"), "/")
	curpath := ""
	if filepath.IsAbs(path) {
		curpath = "/"
	}
	changed := false
	for _, seg := range segments {
		if seg == "" {
			continue
		}
		curpath = filepath.Join(curpath, seg)
		if _, err := os.Stat(curpath); err == nil {
			continue
		} else if !os.IsNotExist(err) {
			return changed, fmt.Errorf("stat %s: %w", curpath, err)
		}
		if err := os.Mkdir(curpath, 0o755); err != nil {
			return changed, fmt.Errorf("mkdir %s: %w", curpath, err)
		}
		changed = true
		if _, err := applyOwnershipAndMode(curpath, owner, group, mode); err != nil {
			return changed, err
		}
	}
	return changed, nil
}

// mkdirAllOwned creates dir and any missing ancestors, applying owner/group
// (mode 0o755) to each segment it actually creates. Existing ancestors are
// left untouched, matching os.MkdirAll's behavior for the "already exists"
// case. Unlike os.MkdirAll this does not silently leave newly-created
// intermediates owned by the agent's uid; that matters when the daemon is
// running as root and dir lives under a non-root user's home.
func mkdirAllOwned(dir, owner, group string) error {
	if info, err := os.Stat(dir); err == nil {
		if !info.IsDir() {
			return fmt.Errorf("%s exists but is not a directory", dir)
		}
		return nil
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("stat %s: %w", dir, err)
	}

	segments := strings.Split(strings.Trim(dir, "/"), "/")
	curpath := ""
	if filepath.IsAbs(dir) {
		curpath = "/"
	}
	for _, seg := range segments {
		if seg == "" {
			continue
		}
		curpath = filepath.Join(curpath, seg)
		if _, err := os.Stat(curpath); err == nil {
			continue
		} else if !os.IsNotExist(err) {
			return fmt.Errorf("stat %s: %w", curpath, err)
		}
		if err := os.Mkdir(curpath, 0o755); err != nil {
			return fmt.Errorf("mkdir %s: %w", curpath, err)
		}
		if _, err := applyOwnershipAndMode(curpath, owner, group, ""); err != nil {
			return err
		}
	}
	return nil
}

func (s *Server) handleFileFile(p FileParams) (any, error) {
	info, err := os.Stat(p.Path)
	if os.IsNotExist(err) {
		return nil, fmt.Errorf("%s does not exist; use state=touch to create", p.Path)
	}
	if err != nil {
		return nil, fmt.Errorf("stat %s: %w", p.Path, err)
	}
	if info.IsDir() {
		return nil, fmt.Errorf("%s is a directory, cannot use state=file", p.Path)
	}

	changed, err := applyOwnershipAndMode(p.Path, p.Owner, p.Group, p.Mode)
	if err != nil {
		return nil, err
	}
	return FileResult{Changed: changed, Path: p.Path, State: "file"}, nil
}

func (s *Server) handleFileLink(p FileParams) (any, error) {
	if p.Src == "" {
		return nil, fmt.Errorf("src is required for state=%s", p.State)
	}

	changed := false
	existing, err := os.Readlink(p.Path)
	if err == nil && existing == p.Src {
		// Link already points to the right place.
	} else {
		os.Remove(p.Path)
		if p.State == "hard" {
			err = os.Link(p.Src, p.Path)
		} else {
			err = os.Symlink(p.Src, p.Path)
		}
		if err != nil {
			return nil, fmt.Errorf("create link %s -> %s: %w", p.Path, p.Src, err)
		}
		changed = true
	}

	return FileResult{Changed: changed, Path: p.Path, State: p.State}, nil
}

func (s *Server) handleFileTouch(p FileParams) (any, error) {
	changed := false
	if _, err := os.Stat(p.Path); os.IsNotExist(err) {
		f, err := os.Create(p.Path)
		if err != nil {
			return nil, fmt.Errorf("touch %s: %w", p.Path, err)
		}
		f.Close()
		changed = true
	} else {
		now := time.Now()
		if err := os.Chtimes(p.Path, now, now); err != nil {
			return nil, fmt.Errorf("touch %s: %w", p.Path, err)
		}
		changed = true
	}

	ch, err := applyOwnershipAndMode(p.Path, p.Owner, p.Group, p.Mode)
	if err != nil {
		return nil, err
	}
	changed = changed || ch

	return FileResult{Changed: changed, Path: p.Path, State: "file"}, nil
}

func (s *Server) handleFileAbsent(p FileParams) (any, error) {
	info, err := os.Lstat(p.Path)
	if os.IsNotExist(err) {
		return FileResult{Changed: false, Path: p.Path, State: "absent"}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("stat %s: %w", p.Path, err)
	}
	if info.IsDir() {
		if err := os.RemoveAll(p.Path); err != nil {
			return nil, fmt.Errorf("remove %s: %w", p.Path, err)
		}
	} else {
		if err := os.Remove(p.Path); err != nil {
			return nil, fmt.Errorf("remove %s: %w", p.Path, err)
		}
	}
	return FileResult{Changed: true, Path: p.Path, State: "absent"}, nil
}

// sha256File computes the SHA-256 hex digest of a file.
func sha256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// copyFile copies src to dst, preserving permissions.
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	info, err := in.Stat()
	if err != nil {
		return err
	}

	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, info.Mode())
	if err != nil {
		return err
	}
	defer out.Close()

	_, err = io.Copy(out, in)
	return err
}

// applyOwnershipAndMode sets owner, group, and mode on a path. Returns true if
// anything changed.
func applyOwnershipAndMode(path, owner, group, mode string) (bool, error) {
	changed := false

	if mode != "" {
		m, err := strconv.ParseUint(mode, 8, 32)
		if err != nil {
			return false, fmt.Errorf("parse mode %q: %w", mode, err)
		}
		info, err := os.Stat(path)
		if err != nil {
			return false, fmt.Errorf("stat %s: %w", path, err)
		}
		if info.Mode().Perm() != fs.FileMode(m) {
			if err := os.Chmod(path, fs.FileMode(m)); err != nil {
				return false, fmt.Errorf("chmod %s: %w", path, err)
			}
			changed = true
		}
	}

	if owner != "" || group != "" {
		uid := -1
		gid := -1
		if owner != "" {
			if n, err := strconv.Atoi(owner); err == nil {
				uid = n
			} else {
				u, err := user.Lookup(owner)
				if err != nil {
					return changed, fmt.Errorf("lookup user %q: %w", owner, err)
				}
				uid, _ = strconv.Atoi(u.Uid)
			}
		}
		if group != "" {
			if n, err := strconv.Atoi(group); err == nil {
				gid = n
			} else {
				g, err := user.LookupGroup(group)
				if err != nil {
					return changed, fmt.Errorf("lookup group %q: %w", group, err)
				}
				gid, _ = strconv.Atoi(g.Gid)
			}
		}

		// Check current ownership before changing.
		info, err := os.Stat(path)
		if err != nil {
			return changed, fmt.Errorf("stat %s: %w", path, err)
		}
		if sys, ok := info.Sys().(*syscall.Stat_t); ok {
			currentUID := int(sys.Uid)
			currentGID := int(sys.Gid)
			needChange := false
			if uid >= 0 && currentUID != uid {
				needChange = true
			}
			if gid >= 0 && currentGID != gid {
				needChange = true
			}
			if needChange {
				if err := os.Chown(path, uid, gid); err != nil {
					return changed, fmt.Errorf("chown %s: %w", path, err)
				}
				changed = true
			}
		}
	}

	return changed, nil
}
