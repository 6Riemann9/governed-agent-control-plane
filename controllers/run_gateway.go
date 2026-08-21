package controllers

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

// RunGateway is the single seam between Kubernetes reconciliation and the
// Governed Agent control plane. Kubernetes objects never cross this interface.
type RunGateway interface {
	Submit(context.Context, RunSubmission) (ControlPlaneRun, error)
	Get(context.Context, string, string) (ControlPlaneRun, error)
	Cancel(context.Context, string, string) (ControlPlaneRun, error)
}

// RunSubmission is the stable projection of an AgentRun CR into Governed Agent.
type RunSubmission struct {
	TenantID       string
	ProjectID      string
	Task           string
	Nodes          []RunNode
	IdempotencyKey string
}

type RunNode struct {
	Name       string   `json:"name"`
	Prompt     string   `json:"prompt"`
	Role       string   `json:"role,omitempty"`
	Model      string   `json:"model,omitempty"`
	DependsOn  []string `json:"dependsOn,omitempty"`
	MaxRetries *int     `json:"maxRetries,omitempty"`
}

// ControlPlaneRun contains only status needed for reconciliation. Full output
// remains in PostgreSQL/object storage and is referenced from CR status.
type ControlPlaneRun struct {
	ID         string
	Status     string
	Summary    string
	ErrorCode  string
	StartedAt  *time.Time
	FinishedAt *time.Time
	Steps      []ControlPlaneStep
}

type ControlPlaneStep struct {
	Name      string
	Status    string
	Output    string
	Error     string
	LatencyMS int
}

// HTTPRunGateway is the production adapter for the FastAPI control plane.
type HTTPRunGateway struct {
	baseURL string
	token   string
	client  *http.Client
}

func NewHTTPRunGateway(baseURL, token string, client *http.Client) *HTTPRunGateway {
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	return &HTTPRunGateway{
		baseURL: strings.TrimRight(baseURL, "/"),
		token:   token,
		client:  client,
	}
}

func NewHTTPRunGatewayFromEnv() *HTTPRunGateway {
	return NewHTTPRunGateway(
		os.Getenv("GOVERNED_API_URL"),
		os.Getenv("GOVERNED_API_TOKEN"),
		nil,
	)
}

type submitRunBody struct {
	Task           string    `json:"task"`
	Nodes          []RunNode `json:"nodes,omitempty"`
	ProjectID      string    `json:"project_id,omitempty"`
	IdempotencyKey string    `json:"idempotency_key"`
}

type runEnvelope struct {
	Run backendRun `json:"run"`
}

type backendRun struct {
	ID         string        `json:"id"`
	Status     string        `json:"status"`
	Summary    string        `json:"summary"`
	CreatedAt  *time.Time    `json:"created_at"`
	FinishedAt *time.Time    `json:"finished_at"`
	Steps      []backendStep `json:"steps"`
}

type backendStep struct {
	NodeID    string `json:"node_id"`
	Status    string `json:"status"`
	Output    string `json:"output"`
	Error     string `json:"error"`
	LatencyMS int    `json:"latency_ms"`
}

func (g *HTTPRunGateway) Submit(ctx context.Context, submission RunSubmission) (ControlPlaneRun, error) {
	body, err := json.Marshal(submitRunBody{
		Task:           submission.Task,
		Nodes:          submission.Nodes,
		ProjectID:      submission.ProjectID,
		IdempotencyKey: submission.IdempotencyKey,
	})
	if err != nil {
		return ControlPlaneRun{}, fmt.Errorf("marshal run submission: %w", err)
	}
	var envelope runEnvelope
	if err := g.do(ctx, http.MethodPost, "/api/agent-runs", submission.TenantID, submission.ProjectID, submission.IdempotencyKey, body, &envelope); err != nil {
		return ControlPlaneRun{}, err
	}
	if envelope.Run.ID == "" {
		return ControlPlaneRun{}, fmt.Errorf("submit run: control plane returned no run id")
	}
	return projectRun(envelope.Run), nil
}

func (g *HTTPRunGateway) Get(ctx context.Context, tenantID, runID string) (ControlPlaneRun, error) {
	var envelope runEnvelope
	if err := g.do(ctx, http.MethodGet, "/api/agent-runs/"+runID, tenantID, "", "", nil, &envelope); err != nil {
		return ControlPlaneRun{}, err
	}
	return projectRun(envelope.Run), nil
}

func (g *HTTPRunGateway) Cancel(ctx context.Context, tenantID, runID string) (ControlPlaneRun, error) {
	var envelope runEnvelope
	if err := g.do(ctx, http.MethodPost, "/api/agent-runs/"+runID+"/cancel", tenantID, "", "", nil, &envelope); err != nil {
		return ControlPlaneRun{}, err
	}
	return projectRun(envelope.Run), nil
}

func (g *HTTPRunGateway) do(
	ctx context.Context,
	method, path, tenantID, projectID, idempotencyKey string,
	body []byte,
	result any,
) error {
	if g.baseURL == "" {
		return fmt.Errorf("GOVERNED_API_URL is required")
	}
	request, err := http.NewRequestWithContext(ctx, method, g.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create control plane request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	request.Header.Set("X-Tenant-Id", tenantID)
	if projectID != "" {
		request.Header.Set("X-Project-Id", projectID)
	}
	if idempotencyKey != "" {
		request.Header.Set("X-Idempotency-Key", idempotencyKey)
	}
	if g.token != "" {
		request.Header.Set("Authorization", "Bearer "+g.token)
	}

	response, err := g.client.Do(request)
	if err != nil {
		return fmt.Errorf("control plane request: %w", err)
	}
	defer response.Body.Close()
	responseBody, readErr := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if readErr != nil {
		return fmt.Errorf("read control plane response: %w", readErr)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return &GatewayHTTPError{StatusCode: response.StatusCode, Body: string(responseBody)}
	}
	if err := json.Unmarshal(responseBody, result); err != nil {
		return fmt.Errorf("decode control plane response: %w", err)
	}
	return nil
}

type GatewayHTTPError struct {
	StatusCode int
	Body       string
}

func (e *GatewayHTTPError) Error() string {
	return fmt.Sprintf("control plane returned %d: %s", e.StatusCode, e.Body)
}

func projectRun(run backendRun) ControlPlaneRun {
	steps := make([]ControlPlaneStep, 0, len(run.Steps))
	for _, step := range run.Steps {
		steps = append(steps, ControlPlaneStep{
			Name:      step.NodeID,
			Status:    step.Status,
			Output:    step.Output,
			Error:     step.Error,
			LatencyMS: step.LatencyMS,
		})
	}
	return ControlPlaneRun{
		ID:         run.ID,
		Status:     run.Status,
		Summary:    run.Summary,
		StartedAt:  run.CreatedAt,
		FinishedAt: run.FinishedAt,
		Steps:      steps,
	}
}
