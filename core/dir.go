package core

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
)

type DirOpts struct {
	// Recursively set the file attributes
	Recurse bool
	// The mode for created directories. Defaults to 0777. Actual mode of
	// created files may be lower due to masks.
	Mode os.FileMode
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return os.IsNotExist(err) == false
}

func CreateDirectory(ctx context.Context, path string, opts DirOpts) error {
	// Ansible supports relative paths, but I don't think that makes sense
	if filepath.IsAbs(path) == false {
		return fmt.Errorf("Attempted to create relative filepath %s", path)
	}
	if opts.Mode == 0 {
		opts.Mode = 0777
	}
	vol := filepath.VolumeName(path)
	// TODO recurse
	i := len(vol) + 1
	// there is probably a better way to write this.
	for i < len(path) {
		for i < len(path) && !os.IsPathSeparator(path[i]) {
			i++
		}
		part := path[len(vol):i]
		if exists(part) == false {
			fmt.Fprintf(os.Stderr, "RUN: mkdir --mode=%#o %s\n", opts.Mode, path)
			if err := os.Mkdir(part, opts.Mode); err != nil {
				return err
			}
			// TODO chmod
		}
		i++
	}
	return nil
}
