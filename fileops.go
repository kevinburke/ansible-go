package fastagent

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

func (s *Server) handleStat(params json.RawMessage) (any, error) {
	var p StatParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal StatParams: %w", err)
	}

	statFn := os.Lstat
	if p.Follow {
		statFn = os.Stat
	}

	info, err := statFn(p.Path)
	if err != nil {
		if os.IsNotExist(err) {
			return StatResult{Exists: false, Path: p.Path}, nil
		}
		return nil, fmt.Errorf("stat %s: %w", p.Path, err)
	}

	result := StatResult{
		Exists: true,
		Path:   p.Path,
		IsDir:  info.IsDir(),
		IsLink: info.Mode()&fs.ModeSymlink != 0,
		Mode:   fmt.Sprintf("0%o", info.Mode().Perm()),
		Size:   info.Size(),
		Mtime:  info.ModTime().Unix(),
	}

	if sys, ok := info.Sys().(*syscall.Stat_t); ok {
		result.Atime = statAtime(sys)
		if u, err := user.LookupId(strconv.Itoa(int(sys.Uid))); err == nil {
			result.Owner = u.Username
		}
		if g, err := user.LookupGroupId(strconv.Itoa(int(sys.Gid))); err == nil {
			result.Group = g.Name
		}
	}

	if info.Mode()&fs.ModeSymlink != 0 {
		if dest, err := os.Readlink(p.Path); err == nil {
			result.LinkDest = dest
		}
	}

	if p.Checksum && !info.IsDir() && info.Mode().IsRegular() {
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
	dir := filepath.Dir(p.Dest)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("mkdir %s: %w", dir, err)
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
	changed := false
	info, err := os.Stat(p.Path)
	if os.IsNotExist(err) {
		perm := fs.FileMode(0o755)
		if p.Mode != "" {
			if m, err := strconv.ParseUint(p.Mode, 8, 32); err == nil {
				perm = fs.FileMode(m)
			}
		}
		if err := os.MkdirAll(p.Path, perm); err != nil {
			return nil, fmt.Errorf("mkdir %s: %w", p.Path, err)
		}
		changed = true
	} else if err != nil {
		return nil, fmt.Errorf("stat %s: %w", p.Path, err)
	} else if !info.IsDir() {
		return nil, fmt.Errorf("%s exists but is not a directory", p.Path)
	}

	ch, err := applyOwnershipAndMode(p.Path, p.Owner, p.Group, p.Mode)
	if err != nil {
		return nil, err
	}
	changed = changed || ch

	return FileResult{Changed: changed, Path: p.Path, State: "directory"}, nil
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
			u, err := user.Lookup(owner)
			if err != nil {
				return changed, fmt.Errorf("lookup user %q: %w", owner, err)
			}
			uid, _ = strconv.Atoi(u.Uid)
		}
		if group != "" {
			g, err := user.LookupGroup(group)
			if err != nil {
				return changed, fmt.Errorf("lookup group %q: %w", group, err)
			}
			gid, _ = strconv.Atoi(g.Gid)
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
