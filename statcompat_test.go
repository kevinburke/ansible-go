package fastagent

import (
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

func TestPyExpandVars(t *testing.T) {
	env := statEnv{"A": "alpha", "B_2": "b", "EMPTY": "", "NESTED": "$A"}
	for in, want := range map[string]string{
		"no vars":         "no vars",
		"$A/x":            "alpha/x",
		"${A}x":           "alphax",
		"$A$B_2":          "alphab",
		"$UNSET/x":        "$UNSET/x",
		"${UNSET}":        "${UNSET}",
		"${A":             "${A",
		"$EMPTY|":         "|",
		"$":               "$",
		"$-x":             "$-x",
		"$NESTED":         "$A", // replacement text is not rescanned
		"${}":             "${}",
		"café$A":          "caféalpha",
		"$Aé":             "alphaé", // re.ASCII: \w stops at non-ASCII
		"${A}${B_2}${A}.": "alphabalpha.",
	} {
		if got := pyExpandVars(in, env); got != want {
			t.Errorf("pyExpandVars(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestPyExpandUser(t *testing.T) {
	env := statEnv{"HOME": "/home/me/"}
	for in, want := range map[string]string{
		"~":                        "/home/me",
		"~/x":                      "/home/me/x",
		"x/~":                      "x/~",
		"~no-such-user-fastagent/": "~no-such-user-fastagent/",
	} {
		if got := pyExpandUser(in, env); got != want {
			t.Errorf("pyExpandUser(%q) = %q, want %q", in, got, want)
		}
	}
	if got := pyExpandUser("~", statEnv{"HOME": "/"}); got != "/" {
		t.Errorf(`HOME=/: got %q, want "/"`, got)
	}
}

func TestPyRealpath(t *testing.T) {
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.Mkdir(filepath.Join(dir, "d"), 0o755))
	must(os.WriteFile(filepath.Join(dir, "f"), nil, 0o644))
	must(os.Symlink("d", filepath.Join(dir, "ld")))
	must(os.Symlink("../f", filepath.Join(dir, "d", "up")))
	must(os.Symlink("missing/deeper", filepath.Join(dir, "dangling")))
	must(os.Symlink("b", filepath.Join(dir, "a")))
	must(os.Symlink("a", filepath.Join(dir, "b")))
	must(os.Symlink(filepath.Join(dir, "d"), filepath.Join(dir, "abs")))
	for in, want := range map[string]string{
		dir + "/ld/up":        dir + "/f",
		dir + "/ld/../f":      dir + "/f", // .. applies after resolving ld
		dir + "/dangling":     dir + "/missing/deeper",
		dir + "/dangling/x":   dir + "/missing/deeper/x",
		dir + "/a":            dir + "/a", // loop: left unresolved
		dir + "/a/x":          dir + "/a/x",
		dir + "/abs/./up":     dir + "/f",
		dir + "//d///":        dir + "/d",
		"/":                   "/",
		"/..":                 "/",
		dir + "/f/not-a-dir/": dir + "/f/not-a-dir",
	} {
		if got := pyRealpath(in); got != want {
			t.Errorf("pyRealpath(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestGetBinPath(t *testing.T) {
	dir := t.TempDir()
	noexec := filepath.Join(dir, "noexec")
	withexec := filepath.Join(dir, "withexec")
	for _, d := range []string{noexec, withexec, filepath.Join(withexec, "sub")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(noexec, "tool"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(withexec, "tool"), nil, 0o700); err != nil {
		t.Fatal(err)
	}
	env := statEnv{"PATH": ":" + noexec + ":" + withexec + "/"}
	if got, want := getBinPath("tool", env), withexec+"/tool"; got != want {
		t.Errorf("getBinPath(tool) = %q, want %q", got, want)
	}
	if got := getBinPath("sub", env); got != "" {
		t.Errorf("getBinPath matched a directory: %q", got)
	}
	if got := getBinPath("no-such-tool-fastagent", env); got != "" {
		t.Errorf("getBinPath(missing) = %q", got)
	}
}

func TestLibcStrerror(t *testing.T) {
	for errno, want := range map[unix.Errno]string{
		unix.ENOTDIR: "Not a directory",
		unix.EACCES:  "Permission denied",
		unix.ELOOP:   "Too many levels of symbolic links",
	} {
		if got := libcStrerror(errno); got != want {
			t.Errorf("libcStrerror(%d) = %q, want %q", errno, got, want)
		}
	}
}

func TestRunModuleCommandExpandsArgsAndReportsRC(t *testing.T) {
	env := newStatEnv(map[string]string{"FASTAGENT_ARG": "expanded"})
	out, err := runModuleCommand([]string{"/bin/sh", "-c", "echo $0; exit 3", "$FASTAGENT_ARG"}, env)
	if err != nil {
		t.Fatal(err)
	}
	if out.RC != 3 || out.Stdout != "expanded\n" {
		t.Errorf("got rc=%d stdout=%q", out.RC, out.Stdout)
	}
	if _, err := runModuleCommand([]string{"/nonexistent/fastagent-tool"}, env); err == nil {
		t.Error("expected an error for a command that cannot start")
	}
}

func TestRunModuleCommandReportsSignalAsNegative(t *testing.T) {
	out, err := runModuleCommand([]string{"/bin/sh", "-c", "kill -9 $$"}, newStatEnv(nil))
	if err != nil {
		t.Fatal(err)
	}
	if out.RC != -9 {
		t.Errorf("rc = %d, want -9 as Python's subprocess reports", out.RC)
	}
}
