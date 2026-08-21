package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ToolRef references a tool by name, optionally overridable.
type ToolRef struct {
	// Ref is the tool identifier (e.g. "rag-search", "memory-recall").
	// +kubebuilder:validation:MinLength=1
	Ref string `json:"ref"`
}

// ModelRef specifies the provider and model.
type ModelRef struct {
	// ProviderRef references a provider config (e.g. "deepseek-prod").
	// +kubebuilder:validation:MinLength=1
	ProviderRef string `json:"providerRef"`
	// Model is the model identifier (e.g. "deepseek-chat").
	// +kubebuilder:validation:MinLength=1
	Model string `json:"model"`
	// MaxTokens caps output tokens per request.
	// +kubebuilder:validation:Minimum=1
	MaxTokens int `json:"maxTokens,omitempty"`
}

// AgentRuntime describes how the agent is executed.
type AgentRuntime struct {
	// Engine is the runtime engine, default "governed".
	// +kubebuilder:default=governed
	// +kubebuilder:validation:Enum=governed
	Engine string `json:"engine,omitempty"`
	// Role is the agent role (analyst/researcher/etc).
	Role string `json:"role,omitempty"`
	// Image is the executor image for sandboxed runs.
	Image string `json:"image,omitempty"`
}

// SandboxSpec declares execution isolation requirements.
type SandboxSpec struct {
	// Mode: "required" | "optional" | "disabled".
	// +kubebuilder:validation:Enum=required;optional;disabled
	Mode string `json:"mode,omitempty"`
	// RuntimeClassName selects gVisor/Kata etc.
	RuntimeClassName string `json:"runtimeClassName,omitempty"`
	// NetworkEgress lists allowed egress targets (gateways).
	NetworkEgress []string `json:"networkEgress,omitempty"`
	// PersistentVolumeSizeGi for durable worktrees.
	// +kubebuilder:validation:Minimum=1
	PersistentVolumeSizeGi int `json:"persistentVolumeSizeGi,omitempty"`
	// IdleTimeoutSeconds before hibernating the sandbox.
	// +kubebuilder:validation:Minimum=30
	IdleTimeoutSeconds int `json:"idleTimeoutSeconds,omitempty"`
}

// BudgetSpec declares resource budgets.
type BudgetSpec struct {
	// MaxConcurrentRuns caps concurrent executions.
	// +kubebuilder:validation:Minimum=1
	MaxConcurrentRuns int `json:"maxConcurrentRuns,omitempty"`
	// MaxTokensPerRun caps total tokens per run.
	// +kubebuilder:validation:Minimum=1
	MaxTokensPerRun int `json:"maxTokensPerRun,omitempty"`
	// MaxRunsPerDay caps runs per day.
	// +kubebuilder:validation:Minimum=1
	MaxRunsPerDay int `json:"maxRunsPerDay,omitempty"`
}

// AgentSpec defines the desired state of an Agent.
type AgentSpec struct {
	// DisplayName is the human-readable agent name.
	DisplayName string `json:"displayName,omitempty"`
	// ProjectID binds all runs for this Agent to an Governed Agent project.
	// +kubebuilder:validation:Format=uuid
	ProjectID string `json:"projectId"`
	// Runtime declares the execution runtime.
	Runtime AgentRuntime `json:"runtime,omitempty"`
	// Model declares the handler model.
	Model ModelRef `json:"model,omitempty"`
	// Tools lists allowed tool references.
	Tools []ToolRef `json:"tools,omitempty"`
	// PolicyRef references a TenantPolicy by name.
	PolicyRef string `json:"policyRef,omitempty"`
	// Sandbox declares isolation requirements.
	Sandbox SandboxSpec `json:"sandbox,omitempty"`
	// Budget declares budgets.
	Budget BudgetSpec `json:"budget,omitempty"`
}

// AgentStatus defines the observed state of an Agent.
type AgentStatus struct {
	// Ready is true when the agent can accept runs.
	Ready bool `json:"ready,omitempty"`
	// ObservedGeneration tracks the last processed generation.
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// Conditions convey detailed status.
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=eqagent,scope=Namespaced

// Agent is the Schema for the agents API.
type Agent struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentSpec   `json:"spec,omitempty"`
	Status AgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentList contains a list of Agent.
type AgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Agent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Agent{}, &AgentList{})
}
