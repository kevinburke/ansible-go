package fastagent

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// Code translated or copied from another project stays under that project's
// license. These tests make it hard to add such code without saying so:
// every file that claims upstream code must be listed in
// THIRD_PARTY_NOTICES, and every translated Go declaration must be listed
// there by name and say what it changed.

// upstreamClaim matches the phrasings used to mark upstream code.
var upstreamClaim = regexp.MustCompile(`(?i)\b(translated|copied|ported|adapted|vendored) from\b|\bports? (of )?(posixpath|ansible|cpython|python)`)

func readNotices(t *testing.T) string {
	t.Helper()
	b, err := os.ReadFile("THIRD_PARTY_NOTICES")
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// sourceFiles returns the repo's non-test Go and Python files, skipping
// directories that hold local environments or other checkouts.
func sourceFiles(t *testing.T) []string {
	t.Helper()
	skip := map[string]bool{".git": true, "venv": true, ".python-venv": true, "worktrees": true, "tmp": true, "tests": true, "__pycache__": true}
	var out []string
	err := filepath.WalkDir(".", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if path != "." && (skip[d.Name()] || strings.HasPrefix(d.Name(), ".")) {
				return filepath.SkipDir
			}
			return nil
		}
		name := d.Name()
		switch {
		case strings.HasSuffix(name, "_test.go"), strings.HasSuffix(name, "_test.py"):
		case strings.HasSuffix(name, ".go"), strings.HasSuffix(name, ".py"):
			out = append(out, path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func TestUpstreamCodeIsListedInNotices(t *testing.T) {
	notices := readNotices(t)
	found := false
	for _, path := range sourceFiles(t) {
		b, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if !upstreamClaim.Match(b) {
			continue
		}
		found = true
		if !strings.Contains(notices, filepath.ToSlash(path)) {
			t.Errorf("%s marks code as coming from another project but is not named in THIRD_PARTY_NOTICES; add its source, license text and translated functions there", path)
		}
		if !strings.Contains(string(b), "THIRD_PARTY_NOTICES") {
			t.Errorf("%s marks code as coming from another project but does not point to THIRD_PARTY_NOTICES", path)
		}
	}
	if !found {
		t.Fatal("found no files marked as containing upstream code; statcompat.go should be one, so the scan is broken")
	}
}

func TestTranslatedDeclsListEditsAndNotices(t *testing.T) {
	notices := readNotices(t)
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, "statcompat.go", nil, parser.ParseComments)
	if err != nil {
		t.Fatal(err)
	}
	var translated []string
	check := func(name string, doc *ast.CommentGroup) {
		if doc == nil || !upstreamClaim.MatchString(doc.Text()) {
			return
		}
		translated = append(translated, name)
		if !strings.Contains(doc.Text(), "Edits:") {
			t.Errorf("%s: doc comment says it is translated but has no \"Edits:\" list of what changed", name)
		}
		if !regexp.MustCompile(`(?m)^\s+` + regexp.QuoteMeta(name) + `\s`).MatchString(notices) {
			t.Errorf("%s is translated from upstream but is not listed in THIRD_PARTY_NOTICES", name)
		}
	}
	for _, decl := range f.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			check(d.Name.Name, d.Doc)
		case *ast.GenDecl:
			for _, spec := range d.Specs {
				vs, ok := spec.(*ast.ValueSpec)
				if !ok {
					continue
				}
				doc := vs.Doc
				if doc == nil {
					doc = d.Doc
				}
				for _, n := range vs.Names {
					check(n.Name, doc)
				}
			}
		}
	}
	if len(translated) == 0 {
		t.Fatal("found no translated declarations in statcompat.go; the scan is broken")
	}
}

func TestUpstreamClaimPattern(t *testing.T) {
	for _, s := range []string{
		"pyExpandUser ports posixpath.expanduser",
		"is translated from CPython 3.14.3's",
		"Copied from ansible-core's basic.py",
		"a port of ansible.module_utils",
	} {
		if !upstreamClaim.MatchString(s) {
			t.Errorf("upstreamClaim does not match %q", s)
		}
	}
}
