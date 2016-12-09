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
		if parts[0] == name {
			return true, nil
		}
	}
	if err := scanner.Err(); err != nil {
		return false, err
	}
	return false, user.UnknownUserError(name)
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
