package fastagent

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"os"
	"os/user"
	"strings"
	"testing"
)

func newTestServer() *Server {
	return &Server{
		Logger: slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil)),
	}
}

func rpcCall(t *testing.T, s *Server, method string, params any) Response {
	t.Helper()

	paramsJSON, err := json.Marshal(params)
	if err != nil {
		t.Fatal(err)
	}

	req := Request{ID: 1, Method: method, Params: paramsJSON}
	reqJSON, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	reqJSON = append(reqJSON, '\n')

	input := bytes.NewReader(reqJSON)
	var output bytes.Buffer

	if err := s.Serve(input, &output); err != nil {
		t.Fatal(err)
	}

	var resp Response
	if err := json.Unmarshal(output.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal response %q: %v", output.String(), err)
	}
	return resp
}

func rpcCallRawParams(t *testing.T, s *Server, method string, params json.RawMessage) Response {
	t.Helper()

	req := Request{ID: 1, Method: method, Params: params}
	reqJSON, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	reqJSON = append(reqJSON, '\n')

	input := bytes.NewReader(reqJSON)
	var output bytes.Buffer

	if err := s.Serve(input, &output); err != nil {
		t.Fatal(err)
	}

	var resp Response
	if err := json.Unmarshal(output.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal response %q: %v", output.String(), err)
	}
	return resp
}

func TestHello(t *testing.T) {
	s := newTestServer()
	resp := rpcCall(t, s, "Hello", HelloParams{Version: "test"})

	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result HelloResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if result.Version != Version {
		t.Errorf("got version %q, want %q", result.Version, Version)
	}
	if len(result.Capabilities) == 0 {
		t.Error("expected capabilities, got none")
	}
}

func TestExecSimple(t *testing.T) {
	s := newTestServer()
	resp := rpcCall(t, s, "Exec", ExecParams{
		Argv: []string{"echo", "hello world"},
	})

	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result ExecResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if result.RC != 0 {
		t.Errorf("got rc %d, want 0", result.RC)
	}
	if !strings.Contains(result.Stdout, "hello world") {
		t.Errorf("stdout %q does not contain %q", result.Stdout, "hello world")
	}
	if !result.Changed {
		t.Error("expected changed=true")
	}
}

func TestExecCoercesNativeArgvScalars(t *testing.T) {
	s := newTestServer()
	resp := rpcCallRawParams(t, s, "Exec", json.RawMessage(`{
		"argv": ["printf", "%s|%s|%s|%s", 120, true, null, 1.5]
	}`))

	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result ExecResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if result.Stdout != "120|True|None|1.5" {
		t.Errorf("got stdout %q, want native argv scalars stringified", result.Stdout)
	}
}

func TestExecRejectsNonScalarArgvElement(t *testing.T) {
	s := newTestServer()
	resp := rpcCallRawParams(t, s, "Exec", json.RawMessage(`{
		"argv": ["echo", {"bad": "value"}]
	}`))

	if resp.Error == nil {
		t.Fatal("expected error for non-scalar argv element")
	}
	if !strings.Contains(resp.Error.Message, "argv[1]: expected scalar, got object") {
		t.Fatalf("error %q does not identify bad argv element", resp.Error.Message)
	}
}

func TestExecShell(t *testing.T) {
	s := newTestServer()
	resp := rpcCall(t, s, "Exec", ExecParams{
		CmdString: "echo $((2 + 3))",
		UseShell:  true,
	})

	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result ExecResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if result.Stdout != "5" {
		t.Errorf("got stdout %q, want %q", result.Stdout, "5")
	}
}

func TestExecRejectsUnparsedCommandString(t *testing.T) {
	s := newTestServer()
	resp := rpcCall(t, s, "Exec", ExecParams{
		CmdString: "printf '%s\\n' 'hello world'",
	})

	if resp.Error == nil {
		t.Fatal("expected error for non-shell cmd_string without argv")
	}
	if !strings.Contains(resp.Error.Message, "send argv") {
		t.Fatalf("error %q does not explain argv requirement", resp.Error.Message)
	}
}

func TestExecCreatesSkip(t *testing.T) {
	s := newTestServer()
	resp := rpcCall(t, s, "Exec", ExecParams{
		Argv:    []string{"echo", "should not run"},
		Creates: "/dev/null", // always exists
	})

	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result ExecResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Skipped {
		t.Error("expected skipped=true when creates path exists")
	}
	if result.Changed {
		t.Error("expected changed=false when creates skips execution")
	}
}

func TestExecCreatesSkipUsesGlobRelativeToCwd(t *testing.T) {
	s := newTestServer()
	dir := t.TempDir()
	if err := os.WriteFile(dir+"/created.txt", []byte("ok"), 0o600); err != nil {
		t.Fatal(err)
	}
	resp := rpcCall(t, s, "Exec", ExecParams{
		Argv:    []string{"echo", "should not run"},
		Cwd:     dir,
		Creates: "created.*",
	})

	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result ExecResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Skipped {
		t.Error("expected skipped=true when creates glob matches")
	}
	if result.Stdout != "skipped, since created.* exists" {
		t.Errorf("stdout = %q, want creates skip message", result.Stdout)
	}
}

func TestExecRemovesSkip(t *testing.T) {
	s := newTestServer()
	resp := rpcCall(t, s, "Exec", ExecParams{
		Argv:    []string{"echo", "should not run"},
		Removes: "/nonexistent-path-that-does-not-exist",
	})

	if resp.Error != nil {
		t.Fatalf("unexpected error: %v", resp.Error)
	}

	resultJSON, _ := json.Marshal(resp.Result)
	var result ExecResult
	if err := json.Unmarshal(resultJSON, &result); err != nil {
		t.Fatal(err)
	}
	if !result.Skipped {
		t.Error("expected skipped=true when removes path does not exist")
	}
}

func TestExecCreatesWithBecomeFailsLoudly(t *testing.T) {
	s := newTestServer()
	resp := rpcCall(t, s, "Exec", ExecParams{
		Argv:       []string{"echo", "should not run"},
		Creates:    "/dev/null",
		BecomeUser: "nobody",
	})

	if resp.Error == nil {
		t.Fatal("expected error for creates with become_user")
	}
	if !strings.Contains(resp.Error.Message, "creates with become_user") {
		t.Fatalf("error %q does not explain unsupported creates/become", resp.Error.Message)
	}
}

func TestBecomeUserCwdResolvesHome(t *testing.T) {
	cur, err := user.Current()
	if err != nil {
		t.Fatal(err)
	}
	if cur.HomeDir == "" {
		t.Skip("current user has no home directory")
	}
	got := becomeUserCwd(cur.Username)
	if got != cur.HomeDir {
		t.Errorf("becomeUserCwd(%q) = %q, want %q", cur.Username, got, cur.HomeDir)
	}
}

func TestBecomeUserCwdFallsBackToRoot(t *testing.T) {
	// A username that user.Lookup will not find. The intent is to
	// guarantee a deterministic cwd ("/") rather than leaving cmd.Dir
	// empty and inheriting whatever the daemon happened to be in.
	got := becomeUserCwd("definitely-not-a-real-user-fastagent-test")
	if got != "/" {
		t.Errorf("becomeUserCwd(missing) = %q, want %q", got, "/")
	}
}

func TestUnknownMethod(t *testing.T) {
	s := newTestServer()
	resp := rpcCall(t, s, "Bogus", nil)

	if resp.Error == nil {
		t.Fatal("expected error for unknown method")
	}
	if resp.Error.Code != -32601 {
		t.Errorf("got error code %d, want -32601", resp.Error.Code)
	}
}

func TestMultipleRequests(t *testing.T) {
	s := newTestServer()

	var input bytes.Buffer
	for i := range 3 {
		req := Request{
			ID:     int64(i + 1),
			Method: "Hello",
			Params: json.RawMessage(`{"version":"test"}`),
		}
		data, _ := json.Marshal(req)
		input.Write(data)
		input.WriteByte('\n')
	}

	var output bytes.Buffer
	if err := s.Serve(&input, &output); err != nil {
		t.Fatal(err)
	}

	lines := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(lines) != 3 {
		t.Fatalf("got %d responses, want 3", len(lines))
	}

	for i, line := range lines {
		var resp Response
		if err := json.Unmarshal([]byte(line), &resp); err != nil {
			t.Fatalf("line %d: %v", i, err)
		}
		if resp.ID != int64(i+1) {
			t.Errorf("line %d: got id %d, want %d", i, resp.ID, i+1)
		}
		if resp.Error != nil {
			t.Errorf("line %d: unexpected error: %v", i, resp.Error)
		}
	}
}
