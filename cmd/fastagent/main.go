// fastagent is a persistent Go agent that accelerates Ansible remote
// execution. It speaks newline-delimited JSON-RPC over stdio or a Unix socket.
//
// Usage:
//
//	fastagent --serve       # serve on stdin/stdout (one-shot, launched per task)
//	fastagent --daemon      # start persistent daemon on a Unix socket
//	fastagent --connect     # bridge stdin/stdout to a running daemon
//	fastagent --version     # print version and exit
package main

import (
	"flag"
	"fmt"
	"log/slog"
	"os"

	fastagent "github.com/kevinburke/ansible-go"
)

func main() {
	serve := flag.Bool("serve", false, "serve on stdin/stdout (one-shot)")
	daemon := flag.Bool("daemon", false, "start persistent daemon on a Unix socket")
	connect := flag.Bool("connect", false, "bridge stdin/stdout to a running daemon")
	socket := flag.String("socket", "", "Unix socket path (for --daemon and --connect)")
	idleTimeout := flag.Duration("idle-timeout", fastagent.DefaultIdleTimeout, "daemon auto-shutdown after this idle duration")
	version := flag.Bool("version", false, "print version and exit")
	debug := flag.Bool("debug", false, "enable debug logging to stderr")
	flag.Parse()

	if *version {
		fmt.Println("fastagent", fastagent.Version)
		os.Exit(0)
	}

	level := slog.LevelInfo
	if *debug {
		level = slog.LevelDebug
	}
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: level,
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if a.Key == slog.TimeKey {
				a.Value = slog.StringValue(a.Value.Time().Format("2006-01-02T15:04:05.000Z07:00"))
			}
			return a
		},
	}))

	switch {
	case *serve:
		s := &fastagent.Server{Logger: logger}
		if err := s.Serve(os.Stdin, os.Stdout); err != nil {
			logger.Error("serve failed", "error", err)
			os.Exit(1)
		}

	case *daemon:
		socketPath := *socket
		if socketPath == "" {
			socketPath = fmt.Sprintf("/tmp/fastagent-%d.sock", os.Getuid())
		}
		if err := fastagent.RunDaemon(socketPath, *idleTimeout, logger); err != nil {
			logger.Error("daemon failed", "error", err)
			os.Exit(1)
		}

	case *connect:
		socketPath := *socket
		if socketPath == "" {
			socketPath = fmt.Sprintf("/tmp/fastagent-%d.sock", os.Getuid())
		}
		if err := fastagent.RunConnect(socketPath); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}

	default:
		fmt.Fprintln(os.Stderr, "usage: fastagent --serve")
		fmt.Fprintln(os.Stderr, "       fastagent --daemon [--socket PATH]")
		fmt.Fprintln(os.Stderr, "       fastagent --connect [--socket PATH]")
		fmt.Fprintln(os.Stderr, "       fastagent --version")
		os.Exit(1)
	}
}
