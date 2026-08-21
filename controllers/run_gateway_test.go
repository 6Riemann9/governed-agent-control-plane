package controllers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHTTPRunGatewayProjectsExactControlPlaneContract(t *testing.T) {
	t.Helper()
	const tenantID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
	const projectID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
	const idempotencyKey = "crd-uid"

	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Header.Get("X-Tenant-Id") != tenantID {
			t.Errorf("tenant header = %q", request.Header.Get("X-Tenant-Id"))
		}
		if request.Header.Get("Authorization") != "Bearer test-token" {
			t.Errorf("authorization header = %q", request.Header.Get("Authorization"))
		}
		switch {
		case request.Method == http.MethodPost && request.URL.Path == "/api/agent-runs":
			if request.Header.Get("X-Project-Id") != projectID {
				t.Errorf("project header = %q", request.Header.Get("X-Project-Id"))
			}
			if request.Header.Get("X-Idempotency-Key") != idempotencyKey {
				t.Errorf("idempotency header = %q", request.Header.Get("X-Idempotency-Key"))
			}
			var body submitRunBody
			if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
				t.Fatalf("decode request: %v", err)
			}
			if body.Task != "research" || body.ProjectID != projectID || len(body.Nodes) != 1 {
				t.Fatalf("unexpected submit body: %#v", body)
			}
			response.WriteHeader(http.StatusAccepted)
			_, _ = response.Write([]byte(`{"created":true,"run":{"id":"run-1","status":"queued","steps":[]}}`))
		case request.Method == http.MethodGet && request.URL.Path == "/api/agent-runs/run-1":
			_, _ = response.Write([]byte(`{"run":{"id":"run-1","status":"running","steps":[{"node_id":"research","status":"running","output":null,"error":null,"latency_ms":0}]}}`))
		case request.Method == http.MethodPost && request.URL.Path == "/api/agent-runs/run-1/cancel":
			_, _ = response.Write([]byte(`{"run":{"id":"run-1","status":"cancelled","steps":[]}}`))
		default:
			http.NotFound(response, request)
		}
	}))
	defer server.Close()

	gateway := NewHTTPRunGateway(server.URL, "test-token", server.Client())
	submitted, err := gateway.Submit(context.Background(), RunSubmission{
		TenantID: tenantID, ProjectID: projectID, Task: "research", IdempotencyKey: idempotencyKey,
		Nodes: []RunNode{{Name: "research", Prompt: "research"}},
	})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if submitted.ID != "run-1" || submitted.Status != "queued" {
		t.Fatalf("unexpected submitted run: %#v", submitted)
	}

	polled, err := gateway.Get(context.Background(), tenantID, submitted.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if polled.Status != "running" || len(polled.Steps) != 1 || polled.Steps[0].Name != "research" {
		t.Fatalf("unexpected polled run: %#v", polled)
	}

	cancelled, err := gateway.Cancel(context.Background(), tenantID, submitted.ID)
	if err != nil {
		t.Fatalf("cancel: %v", err)
	}
	if cancelled.Status != "cancelled" {
		t.Fatalf("unexpected cancelled run: %#v", cancelled)
	}
}

func TestHTTPRunGatewayReturnsTypedHTTPError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		http.Error(response, "missing", http.StatusNotFound)
	}))
	defer server.Close()

	_, err := NewHTTPRunGateway(server.URL, "", server.Client()).Get(
		context.Background(), "tenant", "missing",
	)
	httpErr, ok := err.(*GatewayHTTPError)
	if !ok || httpErr.StatusCode != http.StatusNotFound {
		t.Fatalf("expected typed 404, got %T %v", err, err)
	}
}
