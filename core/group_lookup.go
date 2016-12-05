package core

import (
	"bufio"
	"io"
	"os"
	"os/user"
	"strings"
)

const groupFile = "/etc/group"

func lookupGroup(groupname string) (*user.Group, error) {
	f, err := os.Open(groupFile)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return findGroupName(groupname, f)
}

func lookupGroupId(id string) (*user.Group, error) {
	f, err := os.Open(groupFile)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return findGroupId(id, f)
}

func findGroupId(id string, r io.Reader) (*user.Group, error) {
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		text := trimSpace(removeComment(scanner.Text()))
		// check for comment lines
		if len(text) == 0 {
			continue
		}
		// wheel:*:0:root
		parts := strings.SplitN(text, ":", 4)
		if len(parts) < 4 {
			continue
		}
		if parts[2] == id {
			return &user.Group{Name: parts[0], Gid: parts[2]}, nil
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return nil, user.UnknownGroupIdError(id)
}

func findGroupName(name string, r io.Reader) (*user.Group, error) {
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

// removeComment returns line, removing any '#' byte and any following
// bytes.
func removeComment(line string) string {
	if i := strings.Index(line, "#"); i != -1 {
		return line[:i]
	}
	return line
}

func trimSpace(x string) string {
	for len(x) > 0 && isSpace(x[0]) {
		x = x[1:]
	}
	for len(x) > 0 && isSpace(x[len(x)-1]) {
		x = x[:len(x)-1]
	}
	return x
}

// isSpace reports whether b is an ASCII space character.
func isSpace(b byte) bool {
	return b == ' ' || b == '\t' || b == '\n' || b == '\r'
}
