// fastagent is a persistent Go agent that accelerates Ansible remote
// execution. It speaks newline-delimited JSON-RPC over stdio.
//
// Usage:
//
//	fastagent --serve    # start serving on stdin/stdout
//	fastagent --version  # print version and exit
package main

import (
	"flag"
	"fmt"
	"log/slog"
	"os"

	fastagent "github.com/kevinburke/ansible-go"
)

func main() {
	serve := flag.Bool("serve", false, "start the agent, reading JSON-RPC from stdin and writing to stdout")
	version := flag.Bool("version", false, "print version and exit")
	debug := flag.Bool("debug", false, "enable debug logging to stderr")
	flag.Parse()

	if *version {
		fmt.Println("fastagent", fastagent.Version)
		os.Exit(0)
	}

	if !*serve {
		fmt.Fprintln(os.Stderr, "usage: fastagent --serve")
		fmt.Fprintln(os.Stderr, "       fastagent --version")
		os.Exit(1)
	}

	level := slog.LevelInfo
	if *debug {
		level = slog.LevelDebug
	}
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level}))

	s := &fastagent.Server{Logger: logger}
	if err := s.Serve(os.Stdin, os.Stdout); err != nil {
		logger.Error("serve failed", "error", err)
		os.Exit(1)
	}
}
