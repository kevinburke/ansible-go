package fastagent

import (
	"errors"
	"fmt"
	"io"
	"net"
	"os"
)

// RunConnect dials the daemon's Unix socket and bridges stdin/stdout to it.
// It copies stdin → socket and socket → stdout concurrently, exiting when
// the daemon closes its response stream, after draining any final response.
func RunConnect(socketPath string) error {
	conn, err := net.Dial("unix", socketPath)
	if err != nil {
		return fmt.Errorf("connect to daemon at %s: %w", socketPath, err)
	}
	defer conn.Close()

	return bridge(conn.(*net.UnixConn), os.Stdin, os.Stdout)
}

// bridge preserves request EOF while draining the final response. Closing both
// input and the remote socket interrupts the other copy after a peer failure.
func bridge(conn *net.UnixConn, input io.ReadCloser, output io.Writer) error {
	type result struct {
		input bool
		err   error
	}
	done := make(chan result, 2)
	go func() {
		_, err := io.Copy(conn, input)
		if err == nil {
			err = conn.CloseWrite()
		}
		done <- result{true, err}
	}()
	go func() {
		_, err := io.Copy(output, conn)
		done <- result{false, err}
	}()
	first := <-done
	if !first.input || first.err != nil {
		conn.Close()
		input.Close()
	}
	second := <-done
	if first.err != nil {
		return fmt.Errorf("bridge copy: %w", first.err)
	}
	if second.err != nil && (first.input || (!errors.Is(second.err, net.ErrClosed) &&
		!errors.Is(second.err, os.ErrClosed) && !errors.Is(second.err, io.ErrClosedPipe))) {
		return fmt.Errorf("bridge response: %w", second.err)
	}
	return nil // Errors caused by our own close after response EOF are expected.
}
