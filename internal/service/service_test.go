package service_test

import (
	"testing"

	"github.com/veil/veil/internal/blob"
	"github.com/veil/veil/internal/policy"
	"github.com/veil/veil/internal/service"
	"github.com/veil/veil/internal/snapshot"
)

func fixture(t *testing.T) (*service.Service, policy.Policy) {
	t.Helper()
	store := blob.NewMemoryStore()
	pub := store.Put([]byte("public"))
	private := store.Put([]byte("secret"))
	s, err := snapshot.New(map[snapshot.Path]blob.ID{"/public.txt": pub, "/private/secret.txt": private})
	if err != nil {
		t.Fatal(err)
	}
	p := policy.Policy{Version: "1", Rules: []policy.Rule{{Principal: "bob", Path: "/", Read: true}, {Principal: "bob", Path: "/private", Read: false}}}
	return service.New(store, s, p), p
}
func TestProjectionAndRevocation(t *testing.T) {
	svc, p := fixture(t)
	session, err := svc.CreateProjection("bob")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := svc.File(session.ID, "bob", "/private/secret.txt"); err != service.ErrNotFound {
		t.Fatalf("private retrieval = %v", err)
	}
	got, err := svc.File(session.ID, "bob", "/public.txt")
	if err != nil || string(got) != "public" {
		t.Fatalf("public retrieval = %q, %v", got, err)
	}
	p.Version = "2"
	p.Rules = []policy.Rule{{Principal: "bob", Path: "/", Read: false}}
	svc.SetPolicy(p)
	if _, err := svc.File(session.ID, "bob", "/public.txt"); err != service.ErrNotFound {
		t.Fatalf("revoked retrieval = %v", err)
	}
}
func TestSnapshotAndBlobAreImmutable(t *testing.T) {
	store := blob.NewMemoryStore()
	bytes := []byte("x")
	id := store.Put(bytes)
	bytes[0] = 'y'
	got, _ := store.Get(id)
	if string(got) != "x" {
		t.Fatal("blob was mutated")
	}
	entries := map[snapshot.Path]blob.ID{"/x": id}
	s, _ := snapshot.New(entries)
	delete(entries, "/x")
	if _, ok := s.Get("/x"); !ok {
		t.Fatal("snapshot was mutated")
	}
}
