package fastagent

import "sync"

// onceTokensCap bounds how many Once tokens OnceTokens remembers. The
// controller sets a token on one request per task, and a second delivery of
// that request arrives within seconds to minutes of the first, so this
// covers far more than any window in which a duplicate can arrive.
const onceTokensCap = 4096

// OnceTokens records the Once tokens of requests the agent has received, so
// that a request sent twice runs at most once. One daemon shares a single
// OnceTokens across all of its connections: the controller sends the second
// copy on a new connection.
type OnceTokens struct {
	mu    sync.Mutex
	seen  map[string]struct{}
	order []string // ring buffer of tokens in seen, oldest at next
	next  int
}

// NewOnceTokens returns an empty OnceTokens.
func NewOnceTokens() *OnceTokens {
	return &OnceTokens{seen: make(map[string]struct{}, onceTokensCap)}
}

// Claim records token and reports whether it was new. Once it returns true
// for a token, it returns false for that token until onceTokensCap later
// tokens have been claimed.
func (o *OnceTokens) Claim(token string) bool {
	o.mu.Lock()
	defer o.mu.Unlock()
	if _, ok := o.seen[token]; ok {
		return false
	}
	if len(o.order) < onceTokensCap {
		o.order = append(o.order, token)
	} else {
		delete(o.seen, o.order[o.next])
		o.order[o.next] = token
		o.next = (o.next + 1) % onceTokensCap
	}
	o.seen[token] = struct{}{}
	return true
}
