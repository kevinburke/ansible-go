package fastagent

import (
	"fmt"
	"io"
	"net"
	"os"
	"sync"
)

// RunConnect dials the daemon's Unix socket and bridges stdin/stdout to it.
// It copies stdin → socket and socket → stdout concurrently, exiting when
// either direction closes.
func RunConnect(socketPath string) error {
	conn, err := net.Dial("unix", socketPath)
	if err != nil {
		return fmt.Errorf("connect to daemon at %s: %w", socketPath, err)
	}
	defer conn.Close()

	var wg sync.WaitGroup
	wg.Add(2)

	// stdin → socket
	go func() {
		defer wg.Done()
		io.Copy(conn, os.Stdin)
		// Close the write side so the daemon sees EOF for this request stream.
		if uc, ok := conn.(*net.UnixConn); ok {
			uc.CloseWrite()
		}
	}()

	// socket → stdout
	go func() {
		defer wg.Done()
		io.Copy(os.Stdout, conn)
	}()

	wg.Wait()
	return nil
}
