package service_test

import (
	"testing"

	"github.com/veil/veil/internal/service"
	"github.com/veil/veil/internal/snapshot"
)

// FuzzFile verifies arbitrary path-like input cannot panic or bypass the session view.
func FuzzFile(f *testing.F) {
	f.Add("/public.txt")
	f.Add("/private/secret.txt")
	f.Add("../../private/secret.txt")
	f.Fuzz(func(t *testing.T, raw string) {
		svc, _ := fixture(t)
		session, err := svc.CreateProjection("bob")
		if err != nil {
			t.Fatal(err)
		}
		path, err := snapshot.ParsePath(raw)
		if err != nil {
			return
		}
		data, err := svc.File(session.ID, "bob", path)
		if path == "/private/secret.txt" && (err != service.ErrNotFound || data != nil) {
			t.Fatalf("private path leaked: %q, %v", data, err)
		}
	})
}
