package controllers

import (
	"context"
	"reflect"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentsv1alpha1 "github.com/6Riemann9/governed-agent-control-plane/api/v1alpha1"
)

// TenantPolicyReconciler validates the Kubernetes-side policy projection.
// Runtime enforcement remains mandatory; an Active CR never bypasses policy.mjs.
type TenantPolicyReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=agents.governed.io,resources=tenantpolicies,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agents.governed.io,resources=tenantpolicies/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=agents.governed.io,resources=tenantpolicies/finalizers,verbs=update
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch

func (r *TenantPolicyReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	var policy agentsv1alpha1.TenantPolicy
	if err := r.Get(ctx, request.NamespacedName, &policy); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	active, reason, message := validateTenantPolicy(ctx, r.Client, &policy)
	before := policy.Status.DeepCopy()
	policy.Status.Active = active
	policy.Status.ObservedGeneration = policy.Generation
	conditionStatus := metav1.ConditionFalse
	if active {
		conditionStatus = metav1.ConditionTrue
	}
	meta.SetStatusCondition(&policy.Status.Conditions, metav1.Condition{
		Type: "Active", Status: conditionStatus, Reason: reason, Message: message,
		ObservedGeneration: policy.Generation,
	})
	if reflect.DeepEqual(before, &policy.Status) {
		return ctrl.Result{}, nil
	}
	return ctrl.Result{}, r.Status().Update(ctx, &policy)
}

func validateTenantPolicy(ctx context.Context, kubernetes client.Client, policy *agentsv1alpha1.TenantPolicy) (bool, string, string) {
	if _, err := tenantIDFor(ctx, kubernetes, policy); err != nil {
		return false, "InvalidTenantBinding", err.Error()
	}
	if policy.Spec.PolicyVersion == "" {
		return false, "PolicyVersionRequired", "spec.policyVersion is required for audit correlation"
	}
	if len(policy.Spec.AllowedModels) == 0 {
		return false, "NoAllowedModels", "spec.allowedModels must explicitly allow at least one model"
	}
	if hasDuplicates(policy.Spec.AllowedModels) || hasDuplicates(policy.Spec.AllowedTools) {
		return false, "DuplicateAllowlistEntry", "allowedModels and allowedTools must not contain duplicates"
	}
	if !policy.Spec.Network.DefaultDenyEgress {
		return false, "DefaultDenyRequired", "spec.network.defaultDenyEgress must be true"
	}
	return true, "PolicyActive", "policy is valid; runtime enforcement is still required"
}

func hasDuplicates(values []string) bool {
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		if _, exists := seen[value]; exists {
			return true
		}
		seen[value] = struct{}{}
	}
	return false
}

func (r *TenantPolicyReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&agentsv1alpha1.TenantPolicy{}).
		Complete(r)
}
