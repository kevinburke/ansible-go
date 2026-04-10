package fastagent

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"os/user"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// DefaultIdleTimeout is how long the daemon waits with no active connections
// before shutting itself down. This prevents orphan processes on remote hosts.
const DefaultIdleTimeout = 1 * time.Hour

// RunDaemon starts the agent as a persistent daemon listening on a Unix socket.
// If a daemon is already running on socketPath (responds to Hello), it prints
// the socket path and returns nil. Otherwise it removes any stale socket,
// creates a new listener, and serves until interrupted or idle timeout.
func RunDaemon(socketPath string, allowUser string, idleTimeout time.Duration, logger *slog.Logger) error {
	// Check if a daemon is already running.
	if isDaemonRunning(socketPath) {
		fmt.Println(socketPath)
		logger.Info("daemon already running", "socket", socketPath)
		return nil
	}

	// Remove stale socket.
	os.Remove(socketPath)

	// Ensure the socket directory exists.
	if err := os.MkdirAll(filepath.Dir(socketPath), 0o700); err != nil {
		return fmt.Errorf("mkdir for socket: %w", err)
	}

	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		return fmt.Errorf("listen %s: %w", socketPath, err)
	}
	defer listener.Close()
	defer os.Remove(socketPath)

	// Make the socket accessible to the connecting user so SSH forwarding
	// can reach it. Default to 0700 (root only); if allowUser is set,
	// chown to root:<user's group> with mode 0770.
	if allowUser != "" {
		u, err := user.Lookup(allowUser)
		if err != nil {
			logger.Warn("failed to lookup allow-user", "user", allowUser, "error", err)
		} else {
			gid, _ := strconv.Atoi(u.Gid)
			if err := os.Chown(socketPath, 0, gid); err != nil {
				logger.Warn("failed to chown socket", "error", err)
			}
			if err := os.Chmod(socketPath, 0o770); err != nil {
				logger.Warn("failed to chmod socket", "error", err)
			}
			logger.Debug("socket accessible to user", "user", allowUser, "gid", gid)
		}
	}

	// Write PID file.
	pidPath := socketPath + ".pid"
	if err := os.WriteFile(pidPath, []byte(strconv.Itoa(os.Getpid())), 0o644); err != nil {
		logger.Warn("failed to write pid file", "path", pidPath, "error", err)
	}
	defer os.Remove(pidPath)

	// Handle shutdown signals.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		sig := <-sigCh
		logger.Info("received signal, shutting down", "signal", sig)
		listener.Close()
	}()

	// Track active connections for idle timeout.
	var activeConns atomic.Int64
	var mu sync.Mutex
	lastActivity := time.Now()

	// Idle timeout goroutine.
	if idleTimeout > 0 {
		go func() {
			for {
				time.Sleep(1 * time.Minute)
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
			logger.Debug("accept error (shutting down?)", "error", err)
			return nil
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
	conn, err := net.Dial("unix", socketPath)
	if err != nil {
		return false
	}
	defer conn.Close()

	// Send Hello.
	req := Request{ID: 1, Method: "Hello", Params: json.RawMessage(`{"version":"check"}`)}
	data, _ := json.Marshal(req)
	data = append(data, '\n')
	if _, err := conn.Write(data); err != nil {
		return false
	}

	// Read response.
	buf := make([]byte, 4096)
	n, err := conn.Read(buf)
	if err != nil || n == 0 {
		return false
	}

	var resp Response
	if err := json.Unmarshal(buf[:n], &resp); err != nil {
		return false
	}
	return resp.Error == nil
}
