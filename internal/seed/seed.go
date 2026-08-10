// Package seed supplies a deliberately small development-only fixture.
package seed

import (
	"github.com/veil/veil/internal/blob"
	"github.com/veil/veil/internal/policy"
	"github.com/veil/veil/internal/service"
	"github.com/veil/veil/internal/snapshot"
)

func Development() (*service.Service, error) {
	store := blob.NewMemoryStore()
	public := store.Put([]byte("welcome to veil\n"))
	secret := store.Put([]byte("internal only\n"))
	canonical, err := snapshot.New(map[snapshot.Path]blob.ID{"/README.md": public, "/internal/plan.txt": secret})
	if err != nil {
		return nil, err
	}
	return service.New(store, canonical, policy.Policy{Version: "development-v1", Rules: []policy.Rule{{Principal: "alice", Path: "/", Read: true}, {Principal: "bob", Path: "/README.md", Read: true}}}), nil
}
