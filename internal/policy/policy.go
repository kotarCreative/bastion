// Package policy evaluates read permissions using most-specific path rules.
package policy

import (
	"sort"
	"strings"

	"github.com/veil/veil/internal/snapshot"
)

type Principal string
type Rule struct {
	Principal Principal
	Path      string
	Read      bool
}
type Policy struct {
	Version string
	Rules   []Rule
}

func (p Policy) CanRead(principal Principal, target snapshot.Path) bool {
	best, allowed := -1, false
	for _, rule := range p.Rules {
		if rule.Principal != principal || !matches(rule.Path, string(target)) {
			continue
		}
		if len(rule.Path) >= best {
			best, allowed = len(rule.Path), rule.Read
		}
	}
	return allowed
}

func matches(rule, target string) bool {
	if rule == "/" {
		return strings.HasPrefix(target, "/")
	}
	return target == rule || strings.HasPrefix(target, rule+"/")
}

// AuthorizedPaths is sorted so projections have deterministic ordering at every boundary.
func (p Policy) AuthorizedPaths(principal Principal, s snapshot.Snapshot) []snapshot.Path {
	var paths []snapshot.Path
	for path := range s.Entries() {
		if p.CanRead(principal, path) {
			paths = append(paths, path)
		}
	}
	sort.Slice(paths, func(i, j int) bool { return paths[i] < paths[j] })
	return paths
}
