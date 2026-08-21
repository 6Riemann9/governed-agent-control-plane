package controllers

import "testing"

func TestHasDuplicates(t *testing.T) {
	if !hasDuplicates([]string{"rag-search", "memory-recall", "rag-search"}) {
		t.Fatal("expected duplicate allowlist entry to be detected")
	}
	if hasDuplicates([]string{"rag-search", "memory-recall"}) {
		t.Fatal("did not expect distinct allowlist entries to be rejected")
	}
}
