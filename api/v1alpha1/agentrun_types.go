package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// DAGNode declares a node in the agent DAG.
type DAGNode struct {
	// Name is the node name.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=64
	Name string `json:"name"`
	// Role is the subagent role for this node.
	Role string `json:"role,omitempty"`
	// DependsOn lists node names this node depends on.
	DependsOn []string `json:"dependsOn,omitempty"`
	// Prompt overrides the default prompt for this node.
	// +kubebuilder:validation:MaxLength=32000
	Prompt string `json:"prompt,omitempty"`
}

// RetryPolicy declares retry semantics.
type RetryPolicy struct {
	// MaxAttempts caps execution attempts (1 = no retry).
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=5
	MaxAttempts int `json:"maxAttempts,omitempty"`
}

// AgentRunInput is the durable, auditable input for one run.
type AgentRunInput struct {
	// Task is the user-visible objective.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=8000
	Task string `json:"task"`
}

// AgentRunSpec defines the desired state of an AgentRun.
type AgentRunSpec struct {
	// AgentRef references an Agent by name in the same namespace.
	// +kubebuilder:validation:MinLength=1
	AgentRef string `json:"agentRef"`
	// Input is submitted to the referenced Agent.
	Input AgentRunInput `json:"input"`
	// DAG declares the multi-node workflow (optional).
	DAG *DAGSpec `json:"dag,omitempty"`
	// RetryPolicy declares retry semantics.
	RetryPolicy RetryPolicy `json:"retryPolicy,omitempty"`
	// TTLSecondsAfterFinished cleans up the run after completion.
	// +kubebuilder:validation:Minimum=0
	TTLSecondsAfterFinished int64 `json:"ttlSecondsAfterFinished,omitempty"`
	// IdempotencyKey prevents duplicate executions.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	IdempotencyKey string `json:"idempotencyKey,omitempty"`
	// Cancel requests cancellation of a running/queued run.
	Cancel bool `json:"cancel,omitempty"`
}

// DAGSpec declares a DAG of nodes.
type DAGSpec struct {
	// Nodes lists DAG nodes in dependency order.
	// +kubebuilder:validation:MaxItems=64
	Nodes []DAGNode `json:"nodes,omitempty"`
}

// AgentRunPhase is the run lifecycle phase.
// +kubebuilder:validation:Enum=Queued;Running;Succeeded;Failed;Cancelled
type AgentRunPhase string

const (
	AgentRunQueued    AgentRunPhase = "Queued"
	AgentRunRunning   AgentRunPhase = "Running"
	AgentRunSucceeded AgentRunPhase = "Succeeded"
	AgentRunFailed    AgentRunPhase = "Failed"
	AgentRunCancelled AgentRunPhase = "Cancelled"
)

// NodeState captures per-node status.
type NodeState struct {
	// Name of the node.
	Name string `json:"name"`
	// Phase of this node.
	Phase AgentRunPhase `json:"phase"`
	// StartedAt when the node started.
	StartedAt *metav1.Time `json:"startedAt,omitempty"`
	// FinishedAt when the node finished.
	FinishedAt *metav1.Time `json:"finishedAt,omitempty"`
	// ResultRef references the node result in object storage / Postgres.
	ResultRef string `json:"resultRef,omitempty"`
}

// AgentRunStatus defines the observed state of an AgentRun.
type AgentRunStatus struct {
	// Phase is the overall lifecycle phase.
	Phase AgentRunPhase `json:"phase,omitempty"`
	// ObservedGeneration tracks the last processed generation.
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// Conditions convey detailed status.
	Conditions []metav1.Condition `json:"conditions,omitempty"`
	// NodeStates captures per-node progress.
	NodeStates []NodeState `json:"nodeStates,omitempty"`
	// Attempts counts execution attempts.
	Attempts int `json:"attempts,omitempty"`
	// StartedAt when the run started.
	StartedAt *metav1.Time `json:"startedAt,omitempty"`
	// FinishedAt when the run finished.
	FinishedAt *metav1.Time `json:"finishedAt,omitempty"`
	// TraceId correlates with Governed Agent trace.
	TraceID string `json:"traceId,omitempty"`
	// BackendRunID is the durable AgentRun identifier in the Governed Agent control plane.
	BackendRunID string `json:"backendRunId,omitempty"`
	// ResultRef references the run result in object storage / Postgres.
	ResultRef string `json:"resultRef,omitempty"`
	// ErrorCode is a stable error code on failure.
	ErrorCode string `json:"errorCode,omitempty"`
	// Message is a human-readable status message.
	Message string `json:"message,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=eqrun,scope=Namespaced
// +kubebuilder:printcolumn:name="Phase",type="string",JSONPath=".status.phase"
// +kubebuilder:printcolumn:name="Age",type="date",JSONPath=".metadata.creationTimestamp"

// AgentRun is the Schema for the agentruns API.
type AgentRun struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentRunSpec   `json:"spec,omitempty"`
	Status AgentRunStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentRunList contains a list of AgentRun.
type AgentRunList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AgentRun `json:"items"`
}

func init() {
	SchemeBuilder.Register(&AgentRun{}, &AgentRunList{})
}
