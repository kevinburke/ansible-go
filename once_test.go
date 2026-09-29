package fastagent

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// serveLines sends reqs to s in one write, as the controller does with a
// pipelined Hello, and returns the responses.
func serveLines(t *testing.T, s *Server, reqs ...Request) []Response {
	t.Helper()
	var input bytes.Buffer
	for _, req := range reqs {
		data, err := json.Marshal(req)
		if err != nil {
			t.Fatal(err)
		}
		input.Write(data)
		input.WriteByte('\n')
	}
	var output bytes.Buffer
	if err := s.Serve(&input, &output); err != nil {
		t.Fatal(err)
	}
	var resps []Response
	for line := range strings.SplitSeq(strings.TrimSpace(output.String()), "\n") {
		var resp Response
		if err := json.Unmarshal([]byte(line), &resp); err != nil {
			t.Fatalf("unmarshal response %q: %v", line, err)
		}
		resps = append(resps, resp)
	}
	if len(resps) != len(reqs) {
		t.Fatalf("got %d responses, want %d", len(resps), len(reqs))
	}
	return resps
}

func helloRequest(id int64, version string) Request {
	return Request{ID: id, Method: "Hello", Params: json.RawMessage(fmt.Sprintf(`{"version":%q}`, version))}
}

// appendRequest returns an Exec request that appends "x" to path, so a test
// can count how many times the agent ran it.
func appendRequest(t *testing.T, id int64, path, once string) Request {
	t.Helper()
	params, err := json.Marshal(ExecParams{Argv: []string{"sh", "-c", `printf x >> "$1"`, "sh", path}})
	if err != nil {
		t.Fatal(err)
	}
	return Request{ID: id, Method: "Exec", Params: params, Once: once}
}

func readCount(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestPipelinedHelloAndRequest(t *testing.T) {
	s := newTestServer()
	path := filepath.Join(t.TempDir(), "count")
	resps := serveLines(t, s, helloRequest(1, Version), appendRequest(t, 2, path, "tok-1"))
	for i, resp := range resps {
		if resp.Error != nil {
			t.Fatalf("response %d: unexpected error %+v", i, resp.Error)
		}
		if resp.ID != int64(i+1) {
			t.Errorf("response %d: id %d, want %d", i, resp.ID, i+1)
		}
	}
	if got := readCount(t, path); got != "x" {
		t.Errorf("request ran %q times, want once", got)
	}
}

// A pipelined request reaches the agent before the controller has seen the
// Hello's answer, so the agent itself must refuse to run it on a version
// mismatch.
func TestHelloMismatchRefusesLaterRequests(t *testing.T) {
	s := newTestServer()
	path := filepath.Join(t.TempDir(), "count")
	resps := serveLines(t, s,
		helloRequest(1, "0.0.0-other"),
		appendRequest(t, 2, path, "tok-1"),
		appendRequest(t, 3, path, ""),
		helloRequest(4, "0.0.0-other"),
	)
	if resps[0].Error != nil {
		t.Fatalf("Hello: unexpected error %+v", resps[0].Error)
	}
	for _, resp := range resps[1:3] {
		if resp.Error == nil || resp.Error.Code != ErrCodeVersionMismatch {
			t.Errorf("request %d: got error %+v, want code %d", resp.ID, resp.Error, ErrCodeVersionMismatch)
		}
	}
	if resps[3].Error != nil {
		t.Errorf("a later Hello still gets an answer, got error %+v", resps[3].Error)
	}
	if got := readCount(t, path); got != "" {
		t.Errorf("refused requests ran %q", got)
	}
	// The refusal must not claim the token: the controller replays the
	// request on a new connection to a matching daemon.
	resps = serveLines(t, s, helloRequest(1, Version), appendRequest(t, 2, path, "tok-1"))
	if resps[1].Error != nil {
		t.Fatalf("replay after a mismatch: unexpected error %+v", resps[1].Error)
	}
	if got := readCount(t, path); got != "x" {
		t.Errorf("request ran %q times, want once", got)
	}
}

// The second copy of a request arrives on a different connection, which the
// daemon serves with a different Server sharing one OnceTokens.
func TestOnceTokenRunsOnceAcrossConnections(t *testing.T) {
	tokens := NewOnceTokens()
	logger := slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil))
	path := filepath.Join(t.TempDir(), "count")
	first := serveLines(t, &Server{Logger: logger, Tokens: tokens},
		helloRequest(1, Version), appendRequest(t, 2, path, "tok-1"))
	if first[1].Error != nil {
		t.Fatalf("first copy: unexpected error %+v", first[1].Error)
	}
	second := serveLines(t, &Server{Logger: logger, Tokens: tokens},
		helloRequest(1, Version), appendRequest(t, 2, path, "tok-1"), appendRequest(t, 3, path, "tok-2"))
	if second[1].Error == nil || second[1].Error.Code != ErrCodeDuplicate {
		t.Errorf("second copy: got error %+v, want code %d", second[1].Error, ErrCodeDuplicate)
	}
	if second[2].Error != nil {
		t.Errorf("a new token after a duplicate: unexpected error %+v", second[2].Error)
	}
	if got := readCount(t, path); got != "xx" {
		t.Errorf("file holds %q, want one run per token (xx)", got)
	}
}

func TestOnceTokenWithoutTrackingIsRefused(t *testing.T) {
	s := newTestServer()
	s.Tokens = nil
	path := filepath.Join(t.TempDir(), "count")
	resps := serveLines(t, s, appendRequest(t, 1, path, "tok-1"), appendRequest(t, 2, path, ""))
	if resps[0].Error == nil || resps[0].Error.Code != ErrCodeDuplicate {
		t.Errorf("got error %+v, want code %d", resps[0].Error, ErrCodeDuplicate)
	}
	if resps[1].Error != nil {
		t.Errorf("a request without a token: unexpected error %+v", resps[1].Error)
	}
	if got := readCount(t, path); got != "x" {
		t.Errorf("file holds %q, want only the untokened request's x", got)
	}
}

func TestOnceTokensForgetsOldest(t *testing.T) {
	o := NewOnceTokens()
	for i := range onceTokensCap {
		if !o.Claim(fmt.Sprint(i)) {
			t.Fatalf("token %d: fresh token reported as seen", i)
		}
	}
	if o.Claim("0") {
		t.Fatal("token 0 claimed twice while within capacity")
	}
	if !o.Claim("new") {
		t.Fatal("fresh token reported as seen")
	}
	// "new" evicted "0", the oldest.
	if !o.Claim("0") {
		t.Error("oldest token still remembered past capacity")
	}
	if o.Claim(fmt.Sprint(onceTokensCap - 1)) {
		t.Error("a recent token was forgotten")
	}
	if len(o.seen) != onceTokensCap {
		t.Errorf("remembers %d tokens, want %d", len(o.seen), onceTokensCap)
	}
}
