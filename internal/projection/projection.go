// Package projection constructs authorized views without any transport dependency.
package projection

import (
	"github.com/veil/veil/internal/blob"
	"github.com/veil/veil/internal/policy"
	"github.com/veil/veil/internal/snapshot"
)

type Projection struct{ Snapshot snapshot.Snapshot }

func Project(source snapshot.Snapshot, policy policy.Policy, principal policy.Principal) (Projection, error) {
	entries := make(map[snapshot.Path]blob.ID)
	for _, path := range policy.AuthorizedPaths(principal, source) {
		id, _ := source.Get(path)
		entries[path] = id
	}
	view, err := snapshot.New(entries)
	return Projection{Snapshot: view}, err
}
