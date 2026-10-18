// Assemble upstream Go tests without changing submitted functions or assertions.
package main

import (
	"fmt"
	"go/ast"
	"go/format"
	"go/parser"
	"go/token"
	"os"
	"path"
	"strconv"
)

func assemble(answerPath, testPath, outputPath string) error {
	fset := token.NewFileSet()
	answer, err := parser.ParseFile(fset, answerPath, nil, parser.ParseComments)
	if err != nil {
		return err
	}
	tests, err := parser.ParseFile(fset, testPath, nil, parser.ParseComments)
	if err != nil {
		return err
	}
	if answer.Name.Name != "main" || tests.Name.Name != "main" {
		return fmt.Errorf("package main required")
	}
	used := make(map[string]bool)
	for _, file := range []*ast.File{answer, tests} {
		ast.Inspect(file, func(node ast.Node) bool {
			if selector, ok := node.(*ast.SelectorExpr); ok {
				if name, ok := selector.X.(*ast.Ident); ok {
					used[name.Name] = true
				}
			}
			return true
		})
	}
	imports := &ast.GenDecl{Tok: token.IMPORT, Lparen: token.Pos(1)}
	seen := make(map[string]bool)
	decls := make([]ast.Decl, 0)
	for index, file := range []*ast.File{answer, tests} {
		for _, spec := range file.Imports {
			pkg, err := strconv.Unquote(spec.Path.Value)
			if err != nil {
				return err
			}
			alias := ""
			name := path.Base(pkg)
			if spec.Name != nil {
				alias, name = spec.Name.Name, spec.Name.Name
			}
			key := pkg + ":" + alias
			if seen[key] {
				if index == 0 {
					return fmt.Errorf("duplicate import in answer")
				}
				continue
			}
			// Only setup-only unused imports may be removed. Submitted imports
			// must compile, and no missing model imports are synthesized.
			if index == 1 && name != "_" && name != "." && !used[name] {
				continue
			}
			seen[key] = true
			imports.Specs = append(imports.Specs, spec)
		}
		for _, decl := range file.Decls {
			if general, ok := decl.(*ast.GenDecl); ok && general.Tok == token.IMPORT {
				continue
			}
			decls = append(decls, decl)
		}
	}
	if len(imports.Specs) > 0 {
		decls = append([]ast.Decl{imports}, decls...)
	}
	result := &ast.File{Name: ast.NewIdent("main"), Decls: decls}
	output, err := os.Create(outputPath)
	if err != nil {
		return err
	}
	if err := format.Node(output, fset, result); err != nil {
		output.Close()
		return err
	}
	return output.Close()
}

func main() {
	if len(os.Args) != 4 {
		os.Exit(2)
	}
	if err := assemble(os.Args[1], os.Args[2], os.Args[3]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
