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
		io.Copy(buf, os.Stderr)
		log.Fatal(err)
	}
	fmt.Println("Compiled successfully")
	firstPath := strings.Split(gopath, ":")[0]
	binary := filepath.Join(firstPath, "bin", goos+"_"+goarch)
	f, err := os.Open(binary)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	if err := ssh.RunCommandStdin(ctx, host, f, "cat", ">", "/tmp/remote-bin"); err != nil {
		log.Fatal(err)
	}
	// 3. scp it to host
	// 4. ssh to host and run binary with flags
}
