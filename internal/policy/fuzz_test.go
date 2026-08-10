package policy

import (
	"testing"

	"github.com/veil/veil/internal/snapshot"
)

func FuzzCanRead(f *testing.F) {
	f.Add("/", true, "/visible/file.txt")
	f.Add("/private", false, "/private/secret.txt")
	f.Fuzz(func(t *testing.T, rulePath string, allow bool, target string) {
		p := Policy{Rules: []Rule{{Principal: "fuzz", Path: rulePath, Read: allow}}}
		path, err := snapshot.ParsePath(target)
		if err != nil {
			return
		}
		_ = p.CanRead("fuzz", path)
	})
}
