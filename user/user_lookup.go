package user

import (
	"bufio"
	"io"
	"os"
	"os/user"
	"strings"
)

const passwdFile = "/etc/passwd"

func lookupUser(username string) (*user.User, error) {
	f, err := os.Open(passwdFile)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return findUser(username, f)
}

func findUser(name string, r io.Reader) (*user.User, error) {
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		text := trimSpace(removeComment(scanner.Text()))
		if len(text) == 0 {
			continue
		}
		// name:encpass:uid:gid:name/comment:homedir
		// inburke:x:1019:1020::/home/inburke:
		parts := strings.SplitN(text, ":", 6)
		if len(parts) < 6 {
			continue
		}
		if parts[0] == name {
			return &user.User{
				Username: parts[0],
				Uid:      parts[1],
				Gid:      parts[2],
				Name:     parts[4],
				HomeDir:  parts[5],
			}, nil
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return nil, user.UnknownUserError(name)
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
