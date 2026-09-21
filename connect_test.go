package fastagent

import (
	"bytes"
	"errors"
	"io"
	"net"
	"strings"
	"testing"
	"time"
)

func unixTestPair(t *testing.T) (*net.UnixConn, *net.UnixConn) {
	t.Helper()
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: daemonTestSocket(t), Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	a, err := net.DialUnix("unix", nil, listener.Addr().(*net.UnixAddr))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { a.Close() })
	b, err := listener.AcceptUnix()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { b.Close() })
	a.SetDeadline(time.Now().Add(5 * time.Second))
	b.SetDeadline(time.Now().Add(5 * time.Second))
	return a, b
}

func TestBridgeDrainsResponseAfterInputEOF(t *testing.T) {
	a, b := unixTestPair(t)
	go func() {
		defer b.Close()
		data, err := io.ReadAll(b)
		if err == nil {
			b.Write(append(data, []byte(" reply")...))
		}
	}()
	var output bytes.Buffer
	if err := bridge(a, io.NopCloser(strings.NewReader("request")), &output); err != nil {
		t.Fatal(err)
	}
	if output.String() != "request reply" {
		t.Fatal(output.String())
	}
}

func TestBridgeRemoteEOFInterruptsInput(t *testing.T) {
	a, b := unixTestPair(t)
	input, writer := io.Pipe()
	defer writer.Close()
	done := make(chan error, 1)
	go func() { done <- bridge(a, input, io.Discard) }()
	b.Close()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("bridge hung after remote EOF")
	}
}

type failedWriter struct{}

func (failedWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }

func TestBridgeReportsOutputFailure(t *testing.T) {
	a, b := unixTestPair(t)
	input, writer := io.Pipe()
	defer writer.Close()
	go func() { b.Write([]byte("response")) }()
	if err := bridge(a, input, failedWriter{}); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("expected output error, got %v", err)
	}
}

var errInput = errors.New("input read failed")

type failedInput struct{ closed chan struct{} }

func (f failedInput) Read([]byte) (int, error) { <-f.closed; return 0, errInput }
func (f failedInput) Close() error             { close(f.closed); return nil }

func TestBridgePreservesInputFailureDuringShutdown(t *testing.T) {
	a, b := unixTestPair(t)
	b.Close()
	if err := bridge(a, failedInput{closed: make(chan struct{})}, io.Discard); !errors.Is(err, errInput) {
		t.Fatalf("input error was lost during shutdown: %v", err)
	}
}
