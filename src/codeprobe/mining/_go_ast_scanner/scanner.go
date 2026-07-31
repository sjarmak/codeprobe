// Tool-independent Go AST scanner used by codeprobe's AstResolver.
//
// Walks every .go file under -repo, parses it with go/parser, and emits
// JSON-encoded reference records for the requested -symbol on stdout.
//
// Reference categories produced (mechanical, no semantic judgment):
//
//	method_decl   - func (r T) Symbol(...) {} declaration
//	func_decl     - func Symbol(...) {} declaration (package-level)
//	method_call   - <expr>.Symbol(...) where <expr> is a local identifier
//	package_call  - <pkg>.Symbol(...) where <pkg> imports the package that
//	                contains -defining-file
//	bare_call     - Symbol(...) at call position with no selector
//
// Package-qualified calls are matched against the defining package's exact
// module import path. Calls into unrelated packages with the same selector
// name remain excluded. Receiver-type inference is still out of scope.
//
// The scanner is intentionally single-binary, dependency-free, and
// invoked via "go run" by the Python AstResolver.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

type Reference struct {
	Path string `json:"path"`
	Line int    `json:"line"`
	Kind string `json:"kind"`
}

type Output struct {
	Refs              []Reference `json:"refs"`
	Files             []string    `json:"files"`
	TargetImportPath  string      `json:"target_import_path,omitempty"`
	TargetPackageName string      `json:"target_package_name,omitempty"`
	TargetResolved    bool        `json:"target_resolved"`
}

type targetPackage struct {
	definingFile string
	dir          string
	importPath   string
	name         string
	resolved     bool
}

const (
	maxGoSourceBytes = 8 * 1024 * 1024
	maxGoModBytes    = 1024 * 1024
)

func main() {
	repo := flag.String("repo", "", "repository root to scan")
	symbol := flag.String("symbol", "", "symbol name to resolve")
	definingFile := flag.String(
		"defining-file",
		"",
		"repo-relative file that defines the requested symbol",
	)
	targetImportPath := flag.String(
		"target-import-path",
		"",
		"defining package import path resolved from the primary repository",
	)
	targetPackageName := flag.String(
		"target-package-name",
		"",
		"defining package name resolved from the primary repository",
	)
	targetResolved := flag.Bool(
		"target-resolved",
		false,
		"whether the primary repository target resolved successfully",
	)
	scope := flag.String("scope", "auto", "reference scope: auto, package, or repo")
	flag.Parse()

	if *repo == "" || *symbol == "" {
		fmt.Fprintln(os.Stderr, "usage: scanner -repo PATH -symbol NAME")
		os.Exit(2)
	}

	rootAbs, err := filepath.Abs(*repo)
	if err != nil {
		fmt.Fprintf(os.Stderr, "abs(%q): %v\n", *repo, err)
		os.Exit(1)
	}
	root, err := filepath.EvalSymlinks(rootAbs)
	if err != nil {
		fmt.Fprintf(os.Stderr, "resolve repo %q: %v\n", *repo, err)
		os.Exit(1)
	}
	rootFS, err := os.OpenRoot(root)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open repo root %q: %v\n", *repo, err)
		os.Exit(1)
	}
	defer rootFS.Close()
	target := targetPackage{
		importPath: *targetImportPath,
		name:       *targetPackageName,
		resolved:   *targetResolved,
	}
	if !target.resolved && *definingFile != "" {
		target = resolveTargetPackage(rootFS, *definingFile)
	}

	out := Output{
		Refs:              []Reference{},
		Files:             []string{},
		TargetImportPath:  target.importPath,
		TargetPackageName: target.name,
		TargetResolved:    target.resolved,
	}
	seen := map[string]struct{}{}

	// A requested definition that cannot be resolved is unusable evidence.
	// Emit an empty result rather than widening to repo-wide structural matches.
	if *definingFile != "" && !target.resolved {
		encodeOutput(out)
		return
	}

	walkErr := fs.WalkDir(rootFS.FS(), ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			// skip unreadable entries; do not fail the whole scan
			return nil
		}
		if d.IsDir() {
			name := d.Name()
			if name == "" {
				return nil
			}
			// Skip structural dependency/fixture trees and hidden directories.
			if path != "." && (strings.HasPrefix(name, ".") || name == "vendor" || name == "testdata") {
				return fs.SkipDir
			}
			return nil
		}
		if d.Type()&os.ModeSymlink != 0 {
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		refs := scanFile(rootFS, path, *symbol, *scope, target)
		if len(refs) == 0 {
			return nil
		}
		out.Refs = append(out.Refs, refs...)
		for _, r := range refs {
			if _, ok := seen[r.Path]; !ok {
				seen[r.Path] = struct{}{}
				out.Files = append(out.Files, r.Path)
			}
		}
		return nil
	})
	if walkErr != nil {
		fmt.Fprintf(os.Stderr, "walk: %v\n", walkErr)
		os.Exit(1)
	}

	encodeOutput(out)
}

func encodeOutput(out Output) {
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		fmt.Fprintf(os.Stderr, "encode: %v\n", err)
		os.Exit(1)
	}
}

func scanFile(
	root *os.Root, path, symbol, scope string,
	target targetPackage,
) []Reference {
	fset := token.NewFileSet()
	src, err := readRegularFile(root, path, maxGoSourceBytes)
	if err != nil {
		return nil
	}
	file, err := parser.ParseFile(fset, path, src, 0)
	if err != nil {
		// Not all .go files in a repo parse cleanly (testdata, generated
		// fixtures). Skip rather than fail the whole scan.
		return nil
	}

	imports := collectImports(file, target)

	rel := filepath.ToSlash(path)
	samePackage := target.definingFile != "" && filepath.ToSlash(filepath.Dir(rel)) == target.dir
	includeStructural := !target.resolved || scope == "repo" || samePackage

	var refs []Reference

	ast.Inspect(file, func(n ast.Node) bool {
		switch v := n.(type) {
		case *ast.FuncDecl:
			if !includeStructural || v.Name == nil || v.Name.Name != symbol {
				return true
			}
			kind := "func_decl"
			if v.Recv != nil && len(v.Recv.List) > 0 {
				kind = "method_decl"
			}
			refs = append(refs, Reference{
				Path: rel,
				Line: fset.Position(v.Name.Pos()).Line,
				Kind: kind,
			})
		case *ast.CallExpr:
			switch fn := v.Fun.(type) {
			case *ast.SelectorExpr:
				if fn.Sel == nil || fn.Sel.Name != symbol {
					return true
				}
				// An imported selector is a reference only when it resolves to
				// the exact package containing the defining file.
				if id, ok := fn.X.(*ast.Ident); ok {
					if importPath, isPkg := imports[id.Name]; isPkg && id.Obj == nil {
						if scope != "package" && importPath == target.importPath && target.importPath != "" {
							refs = append(refs, Reference{
								Path: rel,
								Line: fset.Position(fn.Sel.Pos()).Line,
								Kind: "package_call",
							})
						}
						return true
					}
				}
				if !includeStructural {
					return true
				}
				refs = append(refs, Reference{
					Path: rel,
					Line: fset.Position(fn.Sel.Pos()).Line,
					Kind: "method_call",
				})
			case *ast.Ident:
				if includeStructural && fn.Name == symbol {
					refs = append(refs, Reference{
						Path: rel,
						Line: fset.Position(fn.Pos()).Line,
						Kind: "bare_call",
					})
				}
			}
		}
		return true
	})

	return refs
}

// collectImports maps each identifier used to qualify an import to its full
// import path:
//
//	import "os"               -> "os"
//	import f "fmt"            -> "f"
//	import . "io"             -> "" (skipped; dot imports inject names)
//	import _ "side"           -> "" (skipped; blank import)
func collectImports(file *ast.File, target targetPackage) map[string]string {
	out := map[string]string{}
	for _, imp := range file.Imports {
		if imp.Path == nil {
			continue
		}
		path := strings.Trim(imp.Path.Value, `"`)
		if path == "" {
			continue
		}
		var name string
		if imp.Name != nil {
			switch imp.Name.Name {
			case "_", ".":
				continue
			default:
				name = imp.Name.Name
			}
		} else {
			if path == target.importPath && target.name != "" {
				name = target.name
			} else {
				// Most packages use the final import-path component. The target
				// package is parsed above because Go does not require that name.
				parts := strings.Split(path, "/")
				name = parts[len(parts)-1]
			}
		}
		if name == "" {
			continue
		}
		out[name] = path
	}
	return out
}

func resolveTargetPackage(root *os.Root, definingFile string) targetPackage {
	if definingFile == "" || filepath.IsAbs(definingFile) {
		return targetPackage{}
	}

	targetRel := filepath.Clean(filepath.FromSlash(definingFile))
	if targetRel == "." || targetRel == ".." || strings.HasPrefix(targetRel, ".."+string(filepath.Separator)) {
		return targetPackage{}
	}
	packageName := readPackageName(root, targetRel)
	if packageName == "" {
		return targetPackage{}
	}

	targetDir := filepath.Dir(targetRel)
	moduleDir, modulePath := nearestModule(targetDir, root)
	if modulePath == "" {
		return targetPackage{
			definingFile: filepath.ToSlash(targetRel),
			dir:          filepath.ToSlash(filepath.Dir(targetRel)),
			name:         packageName,
			resolved:     true,
		}
	}

	packageRel, err := filepath.Rel(moduleDir, targetDir)
	if err != nil {
		return targetPackage{}
	}
	importPath := modulePath
	if packageRel != "." {
		importPath += "/" + filepath.ToSlash(packageRel)
	}
	return targetPackage{
		definingFile: filepath.ToSlash(targetRel),
		dir:          filepath.ToSlash(filepath.Dir(targetRel)),
		importPath:   importPath,
		name:         packageName,
		resolved:     true,
	}
}

func readPackageName(root *os.Root, path string) string {
	src, err := readRegularFile(root, path, maxGoSourceBytes)
	if err != nil {
		return ""
	}
	file, err := parser.ParseFile(token.NewFileSet(), path, src, parser.PackageClauseOnly)
	if err != nil || file.Name == nil {
		return ""
	}
	return file.Name.Name
}

func nearestModule(start string, root *os.Root) (string, string) {
	for dir := start; ; dir = filepath.Dir(dir) {
		if modulePath := readModulePath(root, filepath.Join(dir, "go.mod")); modulePath != "" {
			return dir, modulePath
		}
		if dir == "." {
			return "", ""
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", ""
		}
	}
}

func readModulePath(root *os.Root, path string) string {
	data, err := readRegularFile(root, path, maxGoModBytes)
	if err != nil {
		return ""
	}

	scanner := bufio.NewScanner(strings.NewReader(string(data)))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) >= 2 && fields[0] == "module" {
			return strings.Trim(fields[1], `"`)
		}
	}
	return ""
}

func readRegularFile(root *os.Root, path string, maxBytes int64) ([]byte, error) {
	clean, err := cleanRepoPath(path)
	if err != nil {
		return nil, err
	}
	before, err := lstatNoSymlinks(root, clean)
	if err != nil {
		return nil, err
	}
	if !before.Mode().IsRegular() || before.Size() > maxBytes {
		return nil, fmt.Errorf("unsafe or oversized source file")
	}

	// os.Root pins the repository directory and guarantees that concurrent
	// symlink swaps cannot make this open escape it.
	file, err := root.Open(clean)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	after, err := file.Stat()
	if err != nil {
		return nil, err
	}
	if !after.Mode().IsRegular() || !os.SameFile(before, after) || after.Size() > maxBytes {
		return nil, fmt.Errorf("source file changed or is not a bounded regular file")
	}

	data, err := io.ReadAll(io.LimitReader(file, maxBytes+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > maxBytes {
		return nil, fmt.Errorf("source file exceeds size limit")
	}
	return data, nil
}

func cleanRepoPath(path string) (string, error) {
	if path == "" || filepath.IsAbs(path) {
		return "", fmt.Errorf("source path must be repository-relative")
	}
	clean := filepath.Clean(filepath.FromSlash(path))
	if clean == "." || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("source path escapes repository")
	}
	return clean, nil
}

func lstatNoSymlinks(root *os.Root, path string) (os.FileInfo, error) {
	parts := strings.Split(filepath.ToSlash(path), "/")
	current := ""
	var info os.FileInfo
	for index, part := range parts {
		current = filepath.Join(current, filepath.FromSlash(part))
		candidate, err := root.Lstat(current)
		if err != nil {
			return nil, err
		}
		if candidate.Mode()&os.ModeSymlink != 0 {
			return nil, fmt.Errorf("source path contains a symlink")
		}
		if index < len(parts)-1 && !candidate.IsDir() {
			return nil, fmt.Errorf("source path component is not a directory")
		}
		info = candidate
	}
	return info, nil
}
