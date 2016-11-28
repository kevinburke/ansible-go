package main

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/kevinburke/ansible-go/ssh"
)

func main() {
	cfg := flag.String("config", "config.yml", "Path to a config file")
	flag.Parse()
	if flag.NArg() != 1 {
		os.Stderr.WriteString("Please provide a directory to compile\n")
		os.Exit(2)
	}
	gopath := os.Getenv("GOPATH")
	if gopath == "" {
		log.Fatal("GOPATH not set; can't deploy")
	}

	// 1. figure out target environment
	host, err := ssh.GetHostFromFile(*cfg)
	if err != nil {
		log.Fatal(err)
	}
	ctx := context.TODO()
	goos, goarch, err := ssh.DetectOSArch(ctx, host)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Fprintf(os.Stderr, "detected GOOS=%s, GOARCH=%s, compiling binary\n", goos, goarch)
	// 2. cross compile a binary for that target
	cmd := exec.CommandContext(ctx, "go", "install", flag.Arg(0))
	cmd.Env = []string{
		"GOARCH=" + goarch,
		"GOOS=" + goos,
		"PATH=" + os.Getenv("PATH"),
		"GOPATH=" + gopath,
	}
	buf := new(bytes.Buffer)
	cmd.Stderr = buf
	fmt.Fprintf(os.Stderr, "LOCAL: %s\n", strings.Join(cmd.Args, " "))
	if err := cmd.Run(); err != nil {
		io.Copy(os.Stderr, buf)
		log.Fatal(err)
	}
	firstPath := strings.Split(gopath, ":")[0]
	// 3. scp it to host
	base := filepath.Base(flag.Arg(0))
	binary := filepath.Join(firstPath, "bin", goos+"_"+goarch, base)
	remoteBinary := "/tmp/" + base
	if err := ssh.PutFile(ctx, host, binary, remoteBinary); err != nil {
		log.Fatal(err)
	}
	remoteConfig := fmt.Sprintf("/tmp/%s-config.yml", base)
	if err := ssh.PutFile(ctx, host, *cfg, remoteConfig); err != nil {
		log.Fatal(err)
	}
	// 4. ssh to host and run binary with flags
	if err := ssh.RunCommand(ctx, host, remoteBinary, "--config", remoteConfig); err != nil {
		log.Fatal(err)
	}
}
