// Package httpapi exposes the small, development-only read API.
package httpapi

import (
	"encoding/json"
	"net/http"
	"net/url"
	"sort"
	"strings"

	"github.com/veil/veil/internal/policy"
	"github.com/veil/veil/internal/service"
	"github.com/veil/veil/internal/snapshot"
)

type API struct{ service *service.Service }

func New(s *service.Service) http.Handler { return API{service: s} }

func (a API) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	principal := policy.Principal(r.Header.Get("X-Veil-Principal")) // development fixture identity only
	if principal == "" {
		http.Error(w, "missing X-Veil-Principal", http.StatusUnauthorized)
		return
	}
	if r.Method == http.MethodPost && r.URL.Path == "/v1/projections" {
		a.create(w, principal)
		return
	}
	const prefix = "/v1/projections/"
	if !strings.HasPrefix(r.URL.Path, prefix) {
		http.NotFound(w, r)
		return
	}
	rest := strings.TrimPrefix(r.URL.Path, prefix)
	parts := strings.SplitN(rest, "/", 3)
	if len(parts) == 2 && parts[1] == "entries" && r.Method == http.MethodGet {
		a.entries(w, principal, parts[0])
		return
	}
	if len(parts) == 3 && parts[1] == "files" && r.Method == http.MethodGet {
		a.file(w, r, principal, parts[0], parts[2])
		return
	}
	http.NotFound(w, r)
}

func (a API) create(w http.ResponseWriter, principal policy.Principal) {
	session, err := a.service.CreateProjection(principal)
	if err != nil {
		fail(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"projection_id": session.ID, "policy_version": session.PolicyVersion})
}
func (a API) entries(w http.ResponseWriter, principal policy.Principal, id string) {
	entries, err := a.service.Entries(id, principal)
	if err != nil {
		fail(w, err)
		return
	}
	paths := make([]string, 0, len(entries))
	for p := range entries {
		paths = append(paths, string(p))
	}
	sort.Strings(paths)
	writeJSON(w, http.StatusOK, map[string]any{"entries": paths})
}
func (a API) file(w http.ResponseWriter, r *http.Request, principal policy.Principal, id, raw string) {
	// Encoded separators and traversal are rejected before decoding or lookup.
	escaped := r.URL.EscapedPath()
	if strings.Contains(strings.ToLower(escaped), "%2f") {
		http.NotFound(w, r)
		return
	}
	decoded, err := url.PathUnescape("/" + raw)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	target, err := snapshot.ParsePath(decoded)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	data, err := a.service.File(id, principal, target)
	if err != nil {
		fail(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	_, _ = w.Write(data)
}
func fail(w http.ResponseWriter, err error) {
	if err == service.ErrNotFound {
		http.NotFound(w, nil)
		return
	}
	http.Error(w, "internal server error", http.StatusInternalServerError)
}
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
