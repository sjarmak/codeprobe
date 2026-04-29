// Tool-independent Go AST scanner used by codeprobe's AstResolver.
//
// Walks every .go file under -repo, parses it with go/parser, and emits
// JSON-encoded reference records for the requested -symbol on stdout.
//
// Reference categories produced (mechanical, no semantic judgment):
//
//   method_decl   - func (r T) Symbol(...) {} declaration
//   func_decl     - func Symbol(...) {} declaration (package-level)
//   method_call   - <expr>.Symbol(...) where <expr> is a local
//                   identifier (NOT an imported package alias)
//   bare_call     - Symbol(...) at call position with no selector
//
// Excluded by design:
//
//   - <pkg>.Symbol(...) where <pkg> is in the file's import set
//     (these are resolved via the imported package; cross-package
//     type inference is out of scope for AstResolver v1)
//
// The scanner is intentionally single-binary, dependency-free, and
// invoked via "go run" by the Python AstResolver.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
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
	Refs  []Reference `json:"refs"`
	Files []string    `json:"files"`
}

func main() {
	repo := flag.String("repo", "", "repository root to scan")
	symbol := flag.String("symbol", "", "symbol name to resolve")
	flag.Parse()

	if *repo == "" || *symbol == "" {
		fmt.Fprintln(os.Stderr, "usage: scanner -repo PATH -symbol NAME")
		os.Exit(2)
	}

	root, err := filepath.Abs(*repo)
	if err != nil {
		fmt.Fprintf(os.Stderr, "abs(%q): %v\n", *repo, err)
		os.Exit(1)
	}

	out := Output{Refs: []Reference{}, Files: []string{}}
	seen := map[string]struct{}{}

	walkErr := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			// skip unreadable entries; do not fail the whole scan
			return nil
		}
		if d.IsDir() {
			name := d.Name()
			if name == "" {
				return nil
			}
			// skip hidden dirs and vendor/testdata for parity with rg defaults
			if strings.HasPrefix(name, ".") && path != root {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		refs := scanFile(path, root, *symbol)
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

	enc := json.NewEncoder(os.Stdout)
	if err := enc.Encode(out); err != nil {
		fmt.Fprintf(os.Stderr, "encode: %v\n", err)
		os.Exit(1)
	}
}

func scanFile(path, root, symbol string) []Reference {
	fset := token.NewFileSet()
	src, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	file, err := parser.ParseFile(fset, path, src, parser.SkipObjectResolution)
	if err != nil {
		// Not all .go files in a repo parse cleanly (testdata, generated
		// fixtures). Skip rather than fail the whole scan.
		return nil
	}

	imports := collectImportNames(file)

	rel, err := filepath.Rel(root, path)
	if err != nil {
		rel = path
	}
	rel = filepath.ToSlash(rel)

	var refs []Reference

	ast.Inspect(file, func(n ast.Node) bool {
		switch v := n.(type) {
		case *ast.FuncDecl:
			if v.Name == nil || v.Name.Name != symbol {
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
				// Skip <pkg>.Symbol(...) when <pkg> is a known import.
				if id, ok := fn.X.(*ast.Ident); ok {
					if _, isPkg := imports[id.Name]; isPkg {
						return true
					}
				}
				refs = append(refs, Reference{
					Path: rel,
					Line: fset.Position(fn.Sel.Pos()).Line,
					Kind: "method_call",
				})
			case *ast.Ident:
				if fn.Name == symbol {
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

// collectImportNames returns the set of identifiers a Go file uses to
// reference its imports. Each entry is the alias the file uses to qualify
// package-level selectors:
//
//   import "os"               -> "os"
//   import f "fmt"            -> "f"
//   import . "io"             -> "" (skipped; dot imports inject names)
//   import _ "side"           -> "" (skipped; blank import)
//
// We need this to filter out <pkg>.Symbol(...) calls that resolve into
// imported packages — those go through Sourcegraph/gopls in a real
// type-resolved oracle, not AST.
func collectImportNames(file *ast.File) map[string]struct{} {
	out := map[string]struct{}{}
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
			// default to last path component
			parts := strings.Split(path, "/")
			name = parts[len(parts)-1]
		}
		if name == "" {
			continue
		}
		out[name] = struct{}{}
	}
	return out
}
