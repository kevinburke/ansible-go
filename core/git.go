package core

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
)

type GitOpts struct {
	Bare  bool
	Depth uint
	// Defaults to 'origin' if unspecified
	Remote string
	// Defaults to 'HEAD' if unspecified. If this is a SHA-1, you must specify
	// a Refspec.
	Version string
	// If Version points to a SHA, Refspec must be specified so we know which
	// version to check out. Example "refs/meta/config".
	Refspec string

	// If the reference repository is on the local machine, automatically
	// setup .git/objects/info/alternates to obtain objects from the reference
	// repository. Using an already existing repository as an alternate
	// will require fewer objects to be copied from the repository being
	// cloned, reducing network and local storage costs. When using the
	// --reference-if-able, a non existing directory is skipped with a warning
	// instead of aborting the clone.
	Reference string
}

func (g GitOpts) FormatDepth() string {
	return strconv.FormatUint(uint64(g.Depth), 10)
}

func isRemoteBranch(ctx context.Context, repo, dest, remote, version string) (bool, error) {
	cmd := exec.CommandContext(ctx, "git", "ls-remote", remote, "-h", "refs/heads/"+version)
	buf := new(bytes.Buffer)
	cmd.Stdout = buf
	cmd.Stderr = buf
	cmd.Dir = dest
	err := cmd.Run()
	if err != nil {
		io.Copy(os.Stderr, buf)
	}
	return strings.Contains(buf.String(), version), err
}

func isRemoteTag(ctx context.Context, repo, dest, remote, version string) (bool, error) {
	cmd := exec.CommandContext(ctx, "git", "ls-remote", remote, "-h", "refs/tags/"+version)
	buf := new(bytes.Buffer)
	cmd.Stdout = buf
	cmd.Stderr = buf
	cmd.Dir = dest
	err := cmd.Run()
	if err != nil {
		io.Copy(os.Stderr, buf)
	}
	return strings.Contains(buf.String(), version), err
}

func shouldAddDepth(ctx context.Context, repo, dest string, opts GitOpts) (bool, error) {
	if opts.Version == "HEAD" || opts.Refspec != "" {
		return true, nil
	}
	b, err := isRemoteBranch(ctx, repo, dest, opts.Remote, opts.Version)
	if err != nil {
		return false, err
	}
	if b {
		return true, nil
	}
	b, err = isRemoteTag(ctx, repo, dest, opts.Remote, opts.Version)
	if err != nil {
		return false, err
	}
	return b, nil
}

func clone(ctx context.Context, repo, dest string, opts GitOpts) error {
	if err := CreateDirectory(ctx, dest, DirOpts{}); err != nil {
		return err
	}
	args := []string{"clone"}
	if opts.Bare {
		args = append(args, "--bare")
	} else {
		args = append(args, "--origin", opts.Remote)
	}
	if opts.Depth > 0 {
		shouldAdd, err := shouldAddDepth(ctx, repo, dest, opts)
		if err != nil {
			return fmt.Errorf("Error finding ref for --depth: %s", err.Error())
		}
		if shouldAdd {
			args = append(args, "--depth", opts.FormatDepth())
		} else {
			fmt.Fprintln(os.Stderr, "WARN: Ignoring --depth argument, since we can't figure out what ref to clone")
		}
	}
	if opts.Reference != "" {
		args = append(args, "--reference")
	}
	args = append(args, repo, dest)
	if err := RunCommand(ctx, "git", args...); err != nil {
		return err
	}
	if opts.Bare && opts.Remote != "origin" {
		if err := RunCommand(ctx, "git", "remote", "add", opts.Remote, repo); err != nil {
			return err
		}
	}
	if opts.Refspec != "" {
		args := []string{"fetch"}
		if opts.Depth > 0 {
			args = append(args, "--depth", opts.FormatDepth())
		}
		args = append(args, opts.Remote, opts.Refspec)
		if err := RunCommand(ctx, "git", args...); err != nil {
			return err
		}
	}
	// TODO verify commit
	return nil
}

func isFile(path string) bool {
	// http://stackoverflow.com/a/8824952/329700
	fi, err := os.Stat(path)
	if err != nil {
		return false
	}
	return fi.Mode().IsRegular()

}

func getGitBranches(ctx context.Context, dest string) ([]string, error) {
	buf := new(bytes.Buffer)
	err := RunAll(ctx, nil, buf, nil, "", "git", "branch", "--no-color", "-a")
	if err != nil {
		return []string{}, err
	}
	scanner := bufio.NewScanner(buf)
	var branches []string
	for scanner.Scan() {
		text := scanner.Text()
		if text != "" {
			branches = append(branches, text)
		}
	}
	return branches, scanner.Err()
}

func isNotABranch(ctx context.Context, dest string) (bool, error) {
	branches, err := getGitBranches(ctx, dest)
	if err != nil {
		return false, err
	}
	for _, branch := range branches {
		if strings.HasPrefix(branch, "* ") && (strings.Contains(branch, "no branch") ||
			strings.Contains(branch, "detached from")) {
			return true, nil
		}
	}
	return false, nil
}

func headSplitter(ctx context.Context, headfile string, remote string) (string, error) {
	if !exists(headfile) {
		return "", nil
	}
	f, err := os.Open(headfile)
	if err != nil {
		return "", err
	}
	defer f.Close()
	line, err := bufio.NewReader(f).ReadString('\n')
	if err != nil {
		return "", err
	}
	replaced := strings.Replace(line, "refs/remotes/"+remote, "", 1)
	parts := strings.Split(replaced, " ")
	newRef := parts[len(parts)-1]
}

func gitGetHeadBranch(ctx context.Context, repo, dest string, opts GitOpts) (string, error) {
	var repoPath string
	if opts.Bare {
		repoPath = dest
	} else {
		repoPath = filepath.Join(dest, ".git")
	}
	if isFile(repoPath) {
		// submodule
		return "", errors.New("Submodule support not implemented")
	}
	head := filepath.Join(repoPath, "HEAD")
	isNotBranch, _ := isNotABranch(ctx, dest)
	if isNotBranch {
		head := filepath.Join(repoPath, "refs", "remotes", remote, "HEAD")
	}
	hd, err := headSplitter(ctx, head)
	if err != nil {
		return "", err
	}
	return head, nil
}

func gitSwitchVersion(ctx context.Context, opts GitOpts) error {
	if opts.Version == "HEAD" {

	}
	return nil
}

// Git checks out the given repository. Repo represents a remote Git
// repository, dest is the location on the filesystem to check out the
// repository. Set repo to the empty string to avoid cloning a git repository -
// if so Git will assume dest represents an already checked out repository.
func Git(ctx context.Context, repo, dest string, opts GitOpts) error {
	dest, err := filepath.Abs(dest)
	if err != nil {
		return err
	}
	if strings.HasPrefix(repo, "/") {
		repo = "file://" + repo
	}
	if opts.Remote == "" {
		opts.Remote = "origin"
	}
	if opts.Version == "" {
		opts.Version = "HEAD"
	}

	var fp string
	if opts.Bare {
		fp = filepath.Join(dest, "config")
	} else {
		fp = filepath.Join(dest, ".git", "config")
	}

	// TODO ssh opts
	if exists(fp) == false {
		if err := clone(ctx, repo, dest, opts); err != nil {
			return err
		}
	}

	if opts.Bare == false {
		if err := gitSwitchVersion(ctx, opts); err != nil {
			return err
		}
	}
	return nil
}
