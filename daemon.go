package fastagent

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"os/user"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/sys/unix"
)

// DefaultIdleTimeout is how long the daemon waits with no active connections
// before shutting itself down. This prevents orphan processes on remote hosts.
const DefaultIdleTimeout = 1 * time.Hour

// RunDaemon starts the agent as a persistent daemon listening on a Unix socket.
// If a daemon is already running on socketPath (responds to Hello), it prints
// the socket path and returns nil. Otherwise it removes any stale socket,
// creates a new listener, and serves until interrupted or idle timeout.
func RunDaemon(socketPath string, allowUser string, idleTimeout time.Duration, logger *slog.Logger) error {
	if err := os.MkdirAll(filepath.Dir(socketPath), 0o700); err != nil {
		return fmt.Errorf("mkdir for socket: %w", err)
	}
	lock, err := openDaemonLock(socketPath + ".lock")
	if err != nil {
		return fmt.Errorf("daemon lock: %w", err)
	}
	defer lock.Close()
	if err := unix.Flock(int(lock.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		if errors.Is(err, unix.EWOULDBLOCK) {
			for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); {
				if isDaemonRunning(socketPath) {
					fmt.Println(socketPath)
					return nil
				}
				time.Sleep(100 * time.Millisecond)
			}
		}
		return fmt.Errorf("daemon already starting or inaccessible: %w", err)
	}
	// Check if a daemon is already running.
	if isDaemonRunning(socketPath) {
		fmt.Println(socketPath)
		logger.Info("daemon already running", "socket", socketPath)
		return nil
	}

	// Only the lock owner may replace a stale socket.
	if st, err := os.Lstat(socketPath); err == nil {
		if st.Mode()&os.ModeSocket == 0 {
			return fmt.Errorf("daemon path exists and is not a socket")
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("inspect daemon socket: %w", err)
	}
	if err := os.Remove(socketPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove stale socket: %w", err)
	}

	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		return fmt.Errorf("listen %s: %w", socketPath, err)
	}
	defer listener.Close()
	defer os.Remove(socketPath)

	// Make the socket accessible to the connecting user so SSH forwarding
	// can reach it. Default to 0700 (daemon owner only); if allowUser is set,
	// chown to the daemon UID and the user's group with mode 0770.
	if err := os.Chmod(socketPath, 0o700); err != nil {
		return fmt.Errorf("chmod daemon socket: %w", err)
	}
	if allowUser != "" {
		u, err := user.Lookup(allowUser)
		if err != nil {
			return fmt.Errorf("lookup allow-user: %w", err)
		} else {
			gid, err := strconv.Atoi(u.Gid)
			if err != nil {
				return fmt.Errorf("parse allow-user gid: %w", err)
			}
			if err := os.Chown(socketPath, os.Getuid(), gid); err != nil {
				return fmt.Errorf("chown daemon socket: %w", err)
			}
			if err := os.Chmod(socketPath, 0o770); err != nil {
				return fmt.Errorf("chmod daemon socket: %w", err)
			}
			logger.Debug("socket accessible to user", "user", allowUser, "gid", gid)
		}
	}

	// Write PID file.
	pidPath := socketPath + ".pid"
	if err := os.WriteFile(pidPath, []byte(strconv.Itoa(os.Getpid())), 0o644); err != nil {
		return fmt.Errorf("write daemon pid file: %w", err)
	}
	defer os.Remove(pidPath)

	// Handle shutdown signals.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, unix.SIGINT, unix.SIGTERM)
	defer signal.Stop(sigCh)
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case sig := <-sigCh:
			logger.Info("received signal, shutting down", "signal", sig)
			listener.Close()
		case <-done:
		}
	}()

	// Track active connections for idle timeout.
	var activeConns atomic.Int64
	var mu sync.Mutex
	lastActivity := time.Now()

	// Idle timeout goroutine.
	if idleTimeout > 0 {
		go func() {
			ticker := time.NewTicker(time.Minute)
			defer ticker.Stop()
			for {
				select {
				case <-done:
					return
				case <-ticker.C:
				}
				if activeConns.Load() == 0 {
					mu.Lock()
					idle := time.Since(lastActivity)
					mu.Unlock()
					if idle >= idleTimeout {
						logger.Info("idle timeout reached, shutting down",
							"idle", idle.String(), "timeout", idleTimeout.String())
						listener.Close()
						return
					}
				}
			}
		}()
	}

	logger.Info("daemon started", "socket", socketPath, "pid", os.Getpid(),
		"idle_timeout", idleTimeout.String())
	fmt.Println(socketPath)

	for {
		conn, err := listener.Accept()
		if err != nil {
			// Expected when listener is closed by signal or idle timeout.
			if errors.Is(err, net.ErrClosed) {
				return nil
			}
			return fmt.Errorf("accept daemon connection: %w", err)
		}

		activeConns.Add(1)
		mu.Lock()
		lastActivity = time.Now()
		mu.Unlock()

		logger.Debug("accepted connection", "active", activeConns.Load())
		go func() {
			defer func() {
				conn.Close()
				activeConns.Add(-1)
				mu.Lock()
				lastActivity = time.Now()
				mu.Unlock()
				logger.Debug("connection closed", "active", activeConns.Load())
			}()
			s := &Server{Logger: logger}
			if err := s.Serve(conn, conn); err != nil {
				logger.Error("connection serve error", "error", err)
			}
		}()
	}
}

// isDaemonRunning checks if a daemon is already listening on socketPath by
// connecting and sending a Hello request.
func isDaemonRunning(socketPath string) bool {
	conn, err := net.DialTimeout("unix", socketPath, time.Second)
	if err != nil {
		return false
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(time.Second)); err != nil {
		return false
	}

	// Send Hello.
	req := Request{ID: 1, Method: "Hello", Params: json.RawMessage(`{"version":"check"}`)}
	data, _ := json.Marshal(req)
	data = append(data, '\n')
	if _, err := conn.Write(data); err != nil {
		return false
	}

	var resp struct {
		ID     int64        `json:"id"`
		Result *HelloResult `json:"result"`
		Error  *ErrorInfo   `json:"error"`
	}
	if err := json.NewDecoder(io.LimitReader(conn, 4096)).Decode(&resp); err != nil {
		return false
	}
	return resp.ID == 1 && resp.Error == nil && resp.Result != nil && resp.Result.Version != ""
}

// openDaemonLock leaves the inode in place so racing starters always flock the
// same file. Reject symlinks, special files and files owned by another user.
func openDaemonLock(path string) (*os.File, error) {
	fd, err := unix.Open(path, unix.O_CREAT|unix.O_RDWR|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0o600)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), path)
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		f.Close()
		return nil, err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG || st.Uid != uint32(os.Getuid()) || st.Mode&0o077 != 0 {
		f.Close()
		return nil, fmt.Errorf("unsafe daemon lock file %s", path)
	}
	return f, nil
}
