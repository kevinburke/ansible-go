package user

import (
	"io"
	"os"
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
	return false, nil
}
