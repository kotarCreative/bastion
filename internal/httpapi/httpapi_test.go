package httpapi_test

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/veil/veil/internal/httpapi"
	"github.com/veil/veil/internal/seed"
)

func TestTraversalRejected(t *testing.T) {
	svc, _ := seed.Development()
	h := httpapi.New(svc)
	create := httptest.NewRequest(http.MethodPost, "/v1/projections", nil)
	create.Header.Set("X-Veil-Principal", "alice")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, create)
	if rec.Code != 201 {
		t.Fatal(rec.Code)
	} /* unknown id traversal must never reveal data */
	req := httptest.NewRequest(http.MethodGet, "/v1/projections/not-a-session/files/%2e%2e/internal/plan.txt", nil)
	req.Header.Set("X-Veil-Principal", "alice")
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("got %d", rec.Code)
	}
}
