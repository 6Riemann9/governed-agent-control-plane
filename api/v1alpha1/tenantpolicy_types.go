package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PolicyRule declares a coarse-grained allow rule.
type PolicyRule struct {
	// Resources lists resource kinds this rule applies to.
	Resources []string `json:"resources,omitempty"`
	// Verbs lists allowed actions.
	Verbs []string `json:"verbs,omitempty"`
}

// NetworkPolicySpec declares tenant egress restrictions.
type NetworkPolicySpec struct {
	// EgressAllowed lists allowed egress targets (gateway names).
	EgressAllowed []string `json:"egressAllowed,omitempty"`
	// DefaultDenyEgress when true denies all unlisted egress.
	DefaultDenyEgress bool `json:"defaultDenyEgress,omitempty"`
}

// Budget declares tenant-level budgets.
type Budget struct {
	// MaxTokensPerDay caps total tokens per day.
	MaxTokensPerDay int `json:"maxTokensPerDay,omitempty"`
	// MaxRunsPerDay caps total runs per day.
	MaxRunsPerDay int `json:"maxRunsPerDay,omitempty"`
	// MaxConcurrentRuns caps concurrent runs.
	MaxConcurrentRuns int `json:"maxConcurrentRuns,omitempty"`
}

// TenantPolicySpec defines the desired state of a TenantPolicy.
type TenantPolicySpec struct {
	// AllowedTools is the tool whitelist for the tenant.
	AllowedTools []string `json:"allowedTools,omitempty"`
	// AllowedModels is the model whitelist for the tenant.
	// +kubebuilder:validation:MinItems=1
	AllowedModels []string `json:"allowedModels,omitempty"`
	// Network declares egress restrictions.
	Network NetworkPolicySpec `json:"network,omitempty"`
	// Budget declares tenant budgets.
	Budget Budget `json:"budget,omitempty"`
	// ApprovalLevel: "none" | "auto" | "human".
	// +kubebuilder:validation:Enum=none;auto;human
	ApprovalLevel string `json:"approvalLevel,omitempty"`
	// Rules is a coarse-grained RBAC allow list.
	Rules []PolicyRule `json:"rules,omitempty"`
	// PolicyVersion for auditing.
	// +kubebuilder:validation:MinLength=1
	PolicyVersion string `json:"policyVersion"`
}

// TenantPolicyStatus defines the observed state of a TenantPolicy.
type TenantPolicyStatus struct {
	// ObservedGeneration tracks the last processed generation.
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// Active indicates the policy is enforced.
	Active bool `json:"active,omitempty"`
	// Conditions convey detailed status.
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=eqpol,scope=Namespaced

// TenantPolicy is the Schema for the tenantpolicies API.
type TenantPolicy struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   TenantPolicySpec   `json:"spec,omitempty"`
	Status TenantPolicyStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// TenantPolicyList contains a list of TenantPolicy.
type TenantPolicyList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []TenantPolicy `json:"items"`
}

func init() {
	SchemeBuilder.Register(&TenantPolicy{}, &TenantPolicyList{})
}
