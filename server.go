package fastagent

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
)

// Server handles JSON-RPC requests from an Ansible controller.
//
// Tokens records the Once tokens of requests received (see Request.Once). A
// daemon shares one across every connection it serves. With a nil Tokens the
// server refuses any request that carries a token rather than run it without
// the at-most-once guarantee the controller relies on.
type Server struct {
	Logger *slog.Logger
	Tokens *OnceTokens
}

// Serve reads newline-delimited JSON requests from r and writes responses to w.
// It blocks until r is closed or an unrecoverable error occurs.
func (s *Server) Serve(r io.Reader, w io.Writer) error {
	scanner := bufio.NewScanner(r)
	// Allow up to 64MB messages (for large file transfers).
	scanner.Buffer(make([]byte, 64*1024), 64*1024*1024)
	enc := json.NewEncoder(w)
	// Set by a Hello naming a version other than ours. The controller may
	// send its first request in the same write as the Hello, before it has
	// seen our version, so we refuse that request and every later one
	// here: an agent of another version could silently ignore request
	// fields it does not know.
	var mismatch string

	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}

		var req Request
		if err := json.Unmarshal(line, &req); err != nil {
			s.Logger.Error("failed to unmarshal request", "error", err)
			resp := Response{
				ID:    0,
				Error: &ErrorInfo{Code: -32700, Message: "parse error: " + err.Error()},
			}
			if err := enc.Encode(resp); err != nil {
				return fmt.Errorf("writing error response: %w", err)
			}
			continue
		}

		s.Logger.Debug("received request", "id", req.ID, "method", req.Method)
		resp, ok := s.refuse(req, mismatch)
		if !ok {
			resp = s.dispatch(req)
			if req.Method == "Hello" && resp.Error == nil {
				if v := helloVersion(req.Params); v != Version {
					mismatch = v
				}
			}
		}
		if err := enc.Encode(resp); err != nil {
			return fmt.Errorf("writing response: %w", err)
		}
	}

	if err := scanner.Err(); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("reading requests: %w", err)
	}
	return nil
}

// refuse returns the error response for a request the server must not run,
// and false if it may run it. mismatch is the version named by an earlier
// Hello on the connection that did not match ours, or "".
func (s *Server) refuse(req Request, mismatch string) (Response, bool) {
	if mismatch != "" && req.Method != "Hello" {
		return Response{ID: req.ID, Error: &ErrorInfo{
			Code: ErrCodeVersionMismatch,
			Message: fmt.Sprintf("controller version %q does not match agent version %q; "+
				"refusing requests on this connection (not run)", mismatch, Version),
		}}, true
	}
	if req.Once == "" {
		return Response{}, false
	}
	if s.Tokens == nil {
		return Response{ID: req.ID, Error: &ErrorInfo{
			Code:    ErrCodeDuplicate,
			Message: "request has a once token but this server does not track them (not run)",
		}}, true
	}
	if !s.Tokens.Claim(req.Once) {
		s.Logger.Warn("refusing a request already received", "id", req.ID,
			"method", req.Method, "once", req.Once)
		return Response{ID: req.ID, Error: &ErrorInfo{
			Code: ErrCodeDuplicate,
			Message: "request with once token " + req.Once + " was already received; " +
				"not running it again, and the outcome of the first copy is unknown",
		}}, true
	}
	return Response{}, false
}

// helloVersion returns the version a Hello's params name, or "" if they do
// not parse; dispatch has already answered such a Hello with an error.
func helloVersion(params json.RawMessage) string {
	var p HelloParams
	if err := json.Unmarshal(params, &p); err != nil {
		return ""
	}
	return p.Version
}

func (s *Server) dispatch(req Request) Response {
	var result any
	var err error

	switch req.Method {
	case "Hello":
		result, err = s.handleHello(req.Params)
	case "Exec":
		result, err = s.handleExec(req.Params)
	case "Stat":
		result, err = s.handleStat(req.Params)
	case "ReadFile":
		result, err = s.handleReadFile(req.Params)
	case "WriteFile":
		result, err = s.handleWriteFile(req.Params)
	case "File":
		result, err = s.handleFile(req.Params)
	case "Package":
		result, err = s.handlePackage(req.Params)
	case "Service":
		result, err = s.handleService(req.Params)
	default:
		return Response{
			ID:    req.ID,
			Error: &ErrorInfo{Code: -32601, Message: "unknown method: " + req.Method},
		}
	}

	if err != nil {
		s.Logger.Error("handler error", "method", req.Method, "error", err)
		return Response{
			ID:    req.ID,
			Error: &ErrorInfo{Code: 1, Message: err.Error()},
		}
	}

	return Response{
		ID:     req.ID,
		Result: result,
	}
}

func (s *Server) handleHello(params json.RawMessage) (any, error) {
	var p HelloParams
	if err := json.Unmarshal(params, &p); err != nil {
		return nil, fmt.Errorf("unmarshal HelloParams: %w", err)
	}
	s.Logger.Info("hello from controller", "controller_version", p.Version)
	return HelloResult{
		Version: Version,
		Capabilities: []string{
			"exec", "stat", "read_file", "write_file", "file",
			"package", "service",
		},
	}, nil
}
