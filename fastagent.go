// Package fastagent implements a persistent Go agent that accelerates Ansible
// remote execution. It speaks a newline-delimited JSON-RPC protocol over stdio.
package fastagent

import "encoding/json"

// Version is the agent version. Bump this when the protocol or behavior changes.
const Version = "0.7.2"

// Request is a JSON-RPC request from the controller.
type Request struct {
	ID     int64           `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}

// Response is a JSON-RPC response to the controller.
type Response struct {
	ID     int64      `json:"id"`
	Result any        `json:"result,omitempty"`
	Error  *ErrorInfo `json:"error,omitempty"`
}

// ErrorInfo describes an error in a Response.
type ErrorInfo struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// HelloParams is sent by the controller on connect.
type HelloParams struct {
	Version string `json:"version"`
}

// HelloResult is returned by the agent in response to Hello.
type HelloResult struct {
	Version      string   `json:"version"`
	Capabilities []string `json:"capabilities"`
}

// ExecParams describes a command to execute.
//
// BecomeUser, if set, asks the agent to run the command as that user.
// The agent wraps the invocation with `sudo -H -n -u <BecomeUser> --`;
// this only works when the agent itself runs as root, which is the
// case whenever Ansible's `become: true` is in effect.
type ExecParams struct {
	Argv            []string          `json:"argv,omitempty"`
	CmdString       string            `json:"cmd_string,omitempty"`
	UseShell        bool              `json:"use_shell,omitempty"`
	Cwd             string            `json:"cwd,omitempty"`
	Env             map[string]string `json:"env,omitempty"`
	Stdin           string            `json:"stdin,omitempty"`
	TimeoutSeconds  int               `json:"timeout,omitempty"`
	Creates         string            `json:"creates,omitempty"`
	Removes         string            `json:"removes,omitempty"`
	StdinAddNewline *bool             `json:"stdin_add_newline,omitempty"`
	StripEmptyEnds  *bool             `json:"strip_empty_ends,omitempty"`
	BecomeUser      string            `json:"become_user,omitempty"`
}

// ExecResult is the result of command execution.
type ExecResult struct {
	RC      int    `json:"rc"`
	Stdout  string `json:"stdout"`
	Stderr  string `json:"stderr"`
	Changed bool   `json:"changed"`
	Skipped bool   `json:"skipped,omitempty"`
	Msg     string `json:"msg,omitempty"`
}

// StatParams requests file status information.
//
// BecomeUser is accepted on the wire but not yet implemented: stat
// runs as the agent's uid, which could leak file existence or
// metadata that BecomeUser wouldn't otherwise be able to see. We
// should add it at some point (likely by dropping privileges in a
// helper subprocess), but for now if BecomeUser is set the agent
// returns an error rather than silently running as root, and
// callers fall back to running `stat` through the Exec RPC (which
// does support BecomeUser).
type StatParams struct {
	Path              string `json:"path"`
	Follow            bool   `json:"follow,omitempty"`
	Checksum          bool   `json:"checksum,omitempty"`
	ChecksumAlgorithm string `json:"checksum_algorithm,omitempty"`
	BecomeUser        string `json:"become_user,omitempty"`
}

// StatResult contains file status information.
//
// The field set mirrors what ansible.builtin.stat returns so that
// playbooks consuming synthesized fields like `stat.executable` or
// `stat.xusr` work transparently when fastagent shadows the builtin
// stat module.
type StatResult struct {
	Exists   bool   `json:"exists"`
	Path     string `json:"path"`
	IsDir    bool   `json:"isdir,omitempty"`
	IsLink   bool   `json:"islnk,omitempty"`
	IsReg    bool   `json:"isreg,omitempty"`
	IsBlock  bool   `json:"isblk,omitempty"`
	IsChar   bool   `json:"ischr,omitempty"`
	IsFIFO   bool   `json:"isfifo,omitempty"`
	IsSocket bool   `json:"issock,omitempty"`

	Mode  string `json:"mode,omitempty"`
	Owner string `json:"owner,omitempty"` // resolved username, or "" when uid has no passwd entry
	Group string `json:"group,omitempty"` // resolved group name, or "" when gid has no group entry
	UID   int    `json:"uid"`
	GID   int    `json:"gid"`
	Size  int64  `json:"size"`
	Inode uint64 `json:"inode,omitempty"`
	Dev   uint64 `json:"dev,omitempty"`
	Nlink uint64 `json:"nlink,omitempty"`
	Mtime int64  `json:"mtime,omitempty"`
	Atime int64  `json:"atime,omitempty"`
	Ctime int64  `json:"ctime,omitempty"`

	IsUID bool `json:"isuid,omitempty"` // setuid bit
	IsGID bool `json:"isgid,omitempty"` // setgid bit
	RUsr  bool `json:"rusr,omitempty"`
	WUsr  bool `json:"wusr,omitempty"`
	XUsr  bool `json:"xusr,omitempty"`
	RGrp  bool `json:"rgrp,omitempty"`
	WGrp  bool `json:"wgrp,omitempty"`
	XGrp  bool `json:"xgrp,omitempty"`
	ROth  bool `json:"roth,omitempty"`
	WOth  bool `json:"woth,omitempty"`
	XOth  bool `json:"xoth,omitempty"`

	// access(2)-equivalent checks from the agent's perspective.
	// Populated even when mode bits would let us derive them, because
	// access(2) (which is what ansible.builtin.stat consults via
	// os.access) honors filesystem mount options like `noexec` and
	// `ro` that mode bits don't reveal.
	Readable   bool `json:"readable,omitempty"`
	Writeable  bool `json:"writeable,omitempty"`
	Executable bool `json:"executable,omitempty"`

	Checksum string `json:"checksum,omitempty"`

	// Symlink target details, set only when the path is a symlink.
	// LnkTarget is os.Readlink (raw, possibly relative); LnkSource is
	// the resolved absolute path (filepath.EvalSymlinks). Mirrors
	// ansible.builtin.stat, which fills `lnk_target` from os.readlink
	// and `lnk_source` from os.path.realpath.
	LnkTarget string `json:"lnk_target,omitempty"`
	LnkSource string `json:"lnk_source,omitempty"`
}

// ReadFileParams requests file content.
//
// BecomeUser is accepted but not yet implemented; see StatParams
// for the rationale and the path to supporting it later.
type ReadFileParams struct {
	Path       string `json:"path"`
	BecomeUser string `json:"become_user,omitempty"`
}

// ReadFileResult contains the file content, base64-encoded.
type ReadFileResult struct {
	Content string `json:"content"`
	Size    int64  `json:"size"`
}

// WriteFileParams writes a file atomically.
type WriteFileParams struct {
	Dest         string `json:"dest"`
	Content      string `json:"content"` // base64-encoded
	Owner        string `json:"owner,omitempty"`
	Group        string `json:"group,omitempty"`
	Mode         string `json:"mode,omitempty"`
	Backup       bool   `json:"backup,omitempty"`
	UnsafeWrites bool   `json:"unsafe_writes,omitempty"`
	Validate     string `json:"validate,omitempty"`
	Checksum     string `json:"checksum,omitempty"` // expected checksum of existing file; skip write if matches
}

// WriteFileResult is the result of a file write.
type WriteFileResult struct {
	Changed    bool   `json:"changed"`
	Dest       string `json:"dest"`
	Checksum   string `json:"checksum"`
	BackupFile string `json:"backup_file,omitempty"`
}

// FileParams manages file/directory/link state.
type FileParams struct {
	Path    string `json:"path"`
	State   string `json:"state"` // file, directory, link, hard, touch, absent
	Owner   string `json:"owner,omitempty"`
	Group   string `json:"group,omitempty"`
	Mode    string `json:"mode,omitempty"`
	Recurse bool   `json:"recurse,omitempty"`
	Follow  bool   `json:"follow,omitempty"`
	Src     string `json:"src,omitempty"` // for link/hard
	Mtime   string `json:"mtime,omitempty"`
	Atime   string `json:"atime,omitempty"`
}

// FileResult is the result of a file state operation.
type FileResult struct {
	Changed bool   `json:"changed"`
	Path    string `json:"path"`
	State   string `json:"state"`
	Owner   string `json:"owner,omitempty"`
	Group   string `json:"group,omitempty"`
	Mode    string `json:"mode,omitempty"`
}

// PackageParams manages OS packages.
type PackageParams struct {
	Manager        string   `json:"manager"` // apt, dnf, yum
	Names          []string `json:"names"`
	State          string   `json:"state"`                      // present, absent, latest
	UpdateCache    bool     `json:"update_cache,omitempty"`     // run apt-get update first
	CacheValidTime int      `json:"cache_valid_time,omitempty"` // skip update if cache is newer than this (seconds)
}

// PackageResult is the result of a package operation.
type PackageResult struct {
	Changed      bool   `json:"changed"`
	Msg          string `json:"msg,omitempty"`
	CacheUpdated bool   `json:"cache_updated,omitempty"`
}

// ServiceParams manages system services.
type ServiceParams struct {
	Manager string `json:"manager,omitempty"` // systemd, sysvinit
	Name    string `json:"name"`
	State   string `json:"state,omitempty"`   // started, stopped, restarted, reloaded
	Enabled *bool  `json:"enabled,omitempty"` // pointer to distinguish unset from false
	NoBlock bool   `json:"no_block,omitempty"`
}

// ServiceResult is the result of a service operation.
type ServiceResult struct {
	Changed bool   `json:"changed"`
	Name    string `json:"name"`
	State   string `json:"state,omitempty"`
	Enabled bool   `json:"enabled,omitempty"`
}
