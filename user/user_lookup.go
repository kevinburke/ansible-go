package user

import (
	"bufio"
	"io"
	"os"
	"os/user"
	"strings"
)

const passwdFile = "/etc/passwd"

func lookupUser(username string) (bool, error) {
	f, err := os.Open(passwdFile)
	if err != nil {
		return false, err
	}
	defer f.Close()
	return findUserName(username, f)
}

func findUserName(name string, r io.Reader) (bool, error) {
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		text := trimSpace(removeComment(scanner.Text()))
		if len(text) == 0 {
			continue
		}
		// wheel:*:0:root
		parts := strings.SplitN(text, ":", 4)
		if len(parts) < 4 {
			continue
		}
		if parts[0] == name && parts[2] != "" {
			return &user.Group{Name: parts[0], Gid: parts[2]}, nil
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return nil, user.UnknownGroupError(name)
}
