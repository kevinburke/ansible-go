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
type Server struct {
	Logger *slog.Logger
}

// Serve reads newline-delimited JSON requests from r and writes responses to w.
// It blocks until r is closed or an unrecoverable error occurs.
func (s *Server) Serve(r io.Reader, w io.Writer) error {
	scanner := bufio.NewScanner(r)
	// Allow up to 64MB messages (for large file transfers).
	scanner.Buffer(make([]byte, 64*1024), 64*1024*1024)
	enc := json.NewEncoder(w)

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
		resp := s.dispatch(req)
		if err := enc.Encode(resp); err != nil {
			return fmt.Errorf("writing response: %w", err)
		}
	}

	if err := scanner.Err(); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("reading requests: %w", err)
	}
	return nil
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
