// Package blob provides content-addressed immutable byte storage.
package blob

import (
	"crypto/sha256"
	"sync"
)

type ID [sha256.Size]byte

func Sum(data []byte) ID { return sha256.Sum256(data) }

// Store is deliberately narrow so another persistence mechanism can replace it.
type Store interface {
	Put([]byte) ID
	Get(ID) ([]byte, bool)
}

type MemoryStore struct {
	mu   sync.RWMutex
	data map[ID][]byte
}

func NewMemoryStore() *MemoryStore { return &MemoryStore{data: make(map[ID][]byte)} }

func (s *MemoryStore) Put(data []byte) ID {
	id := Sum(data)
	copyData := append([]byte(nil), data...)
	s.mu.Lock()
	if _, exists := s.data[id]; !exists {
		s.data[id] = copyData
	}
	s.mu.Unlock()
	return id
}

func (s *MemoryStore) Get(id ID) ([]byte, bool) {
	s.mu.RLock()
	data, ok := s.data[id]
	s.mu.RUnlock()
	return append([]byte(nil), data...), ok
}
