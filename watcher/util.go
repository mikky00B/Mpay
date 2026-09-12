package main

import (
	"fmt"
	"log"
	"strconv"
	"strings"
)

// trimTopic strips 24 bytes of zero-padding from an address topic.
func trimTopic(topic string) string {
	h := strings.TrimPrefix(strings.ToLower(topic), "0x")
	if len(h) == 64 {
		return h[24:]
	}
	return h
}

// hexToUint64 parses a hex-quantity string like "0x10d4f". A malformed
// quantity is an error, never a silent 0 [D16].
func hexToUint64(s string) (uint64, error) {
	h := strings.TrimPrefix(s, "0x")
	if h == "" {
		return 0, fmt.Errorf("empty hex quantity")
	}
	v, err := strconv.ParseUint(h, 16, 64)
	if err != nil {
		return 0, fmt.Errorf("bad hex quantity %q: %w", s, err)
	}
	return v, nil
}

// mustHexToUint64 is hexToUint64 for log fields already validated by the RPC
// envelope; a malformed value logs loudly and yields 0 rather than crashing a
// scan that would otherwise make progress.
func mustHexToUint64(s string) uint64 {
	v, err := hexToUint64(s)
	if err != nil {
		log.Printf("warning: %v", err)
	}
	return v
}
