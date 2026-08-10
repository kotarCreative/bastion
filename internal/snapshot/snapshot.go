// Package snapshot defines immutable, canonical flat filesystem snapshots.
package snapshot

import (
	"errors"
	"path"
	"strings"

	"github.com/veil/veil/internal/blob"
)

type Path string
type Snapshot struct{ entries map[Path]blob.ID }

func ParsePath(raw string) (Path, error) {
	if raw == "" || !strings.HasPrefix(raw, "/") || raw == "/" || strings.Contains(raw, "\\") || path.Clean(raw) != raw || strings.HasSuffix(raw, "/") {
		return "", errors.New("path must be an absolute, canonical file path")
	}
	return Path(raw), nil
}

func New(entries map[Path]blob.ID) (Snapshot, error) {
	copyEntries := make(map[Path]blob.ID, len(entries))
	for p, id := range entries {
		if _, err := ParsePath(string(p)); err != nil {
			return Snapshot{}, err
		}
		copyEntries[p] = id
	}
	return Snapshot{entries: copyEntries}, nil
}

func (s Snapshot) Get(p Path) (blob.ID, bool) { id, ok := s.entries[p]; return id, ok }
func (s Snapshot) Entries() map[Path]blob.ID {
	entries := make(map[Path]blob.ID, len(s.entries))
	for p, id := range s.entries {
		entries[p] = id
	}
	return entries
}
