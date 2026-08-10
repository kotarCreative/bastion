// Package service contains read use cases and enforces retrieval authorization.
package service

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"sync"

	"github.com/veil/veil/internal/blob"
	"github.com/veil/veil/internal/policy"
	"github.com/veil/veil/internal/projection"
	"github.com/veil/veil/internal/snapshot"
)

var ErrNotFound = errors.New("not found")

type Session struct {
	ID, PolicyVersion string
	Principal         policy.Principal
	Canonical         snapshot.Snapshot
	Projected         snapshot.Snapshot
}

type Service struct {
	blobs     blob.Store
	canonical snapshot.Snapshot
	mu        sync.RWMutex
	current   policy.Policy
	sessions  map[string]Session
}

func New(blobs blob.Store, canonical snapshot.Snapshot, current policy.Policy) *Service {
	return &Service{blobs: blobs, canonical: canonical, current: current, sessions: make(map[string]Session)}
}

func (s *Service) SetPolicy(p policy.Policy) { s.mu.Lock(); s.current = p; s.mu.Unlock() }

func (s *Service) CreateProjection(principal policy.Principal) (Session, error) {
	s.mu.RLock()
	current := s.current
	s.mu.RUnlock()
	view, err := projection.Project(s.canonical, current, principal)
	if err != nil {
		return Session{}, err
	}
	id, err := randomID()
	if err != nil {
		return Session{}, err
	}
	session := Session{ID: id, Principal: principal, PolicyVersion: current.Version, Canonical: s.canonical, Projected: view.Snapshot}
	s.mu.Lock()
	s.sessions[id] = session
	s.mu.Unlock()
	return session, nil
}

// Entries calculates from the current policy, so revocation affects existing sessions.
func (s *Service) Entries(id string, principal policy.Principal) (map[snapshot.Path]blob.ID, error) {
	session, current, err := s.sessionAndPolicy(id, principal)
	if err != nil {
		return nil, err
	}
	view, err := projection.Project(session.Canonical, current, principal)
	if err != nil {
		return nil, err
	}
	return view.Snapshot.Entries(), nil
}

func (s *Service) File(id string, principal policy.Principal, target snapshot.Path) ([]byte, error) {
	session, current, err := s.sessionAndPolicy(id, principal)
	if err != nil {
		return nil, err
	}
	if !current.CanRead(principal, target) {
		return nil, ErrNotFound
	}
	// Reachability must be established from the canonical snapshot, never a client id.
	canonicalID, ok := session.Canonical.Get(target)
	if !ok {
		return nil, ErrNotFound
	}
	projectedID, ok := session.Projected.Get(target)
	if !ok || projectedID != canonicalID {
		return nil, ErrNotFound
	}
	data, ok := s.blobs.Get(canonicalID)
	if !ok {
		return nil, ErrNotFound
	}
	return data, nil
}

func (s *Service) sessionAndPolicy(id string, principal policy.Principal) (Session, policy.Policy, error) {
	s.mu.RLock()
	session, ok := s.sessions[id]
	current := s.current
	s.mu.RUnlock()
	if !ok || session.Principal != principal {
		return Session{}, policy.Policy{}, ErrNotFound
	}
	return session, current, nil
}

func randomID() (string, error) {
	b := make([]byte, 24)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}
