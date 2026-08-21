package controllers

import (
	"context"
	"fmt"
	"reflect"

	"github.com/google/uuid"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentsv1alpha1 "github.com/6Riemann9/governed-agent-control-plane/api/v1alpha1"
)

// AgentReconciler validates the declarative Agent contract. Runtime policy is
// still enforced independently by src/policy.mjs.
type AgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=agents.governed.io,resources=agents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agents.governed.io,resources=agents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=agents.governed.io,resources=agents/finalizers,verbs=update
// +kubebuilder:rbac:groups=agents.governed.io,resources=tenantpolicies,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch

func (r *AgentReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	var agent agentsv1alpha1.Agent
	if err := r.Get(ctx, request.NamespacedName, &agent); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	ready, reason, message, err := r.validate(ctx, &agent)
	if err != nil {
		return ctrl.Result{}, err
	}
	before := agent.Status.DeepCopy()
	agent.Status.Ready = ready
	agent.Status.ObservedGeneration = agent.Generation
	conditionStatus := metav1.ConditionFalse
	if ready {
		conditionStatus = metav1.ConditionTrue
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type: "Ready", Status: conditionStatus, Reason: reason, Message: message,
		ObservedGeneration: agent.Generation,
	})
	if reflect.DeepEqual(before, &agent.Status) {
		return ctrl.Result{}, nil
	}
	return ctrl.Result{}, r.Status().Update(ctx, &agent)
}

func (r *AgentReconciler) validate(
	ctx context.Context,
	agent *agentsv1alpha1.Agent,
) (bool, string, string, error) {
	tenantID, err := tenantIDFor(ctx, r.Client, agent)
	if err != nil {
		return false, "InvalidTenantBinding", err.Error(), nil
	}
	if _, err := uuid.Parse(agent.Spec.ProjectID); err != nil {
		return false, "InvalidProjectID", "spec.projectId must be an Governed Agent project UUID", nil
	}
	if agent.Spec.Sandbox.Mode == "required" && agent.Spec.Sandbox.RuntimeClassName == "" {
		return false, "RuntimeClassRequired", "required sandbox mode needs spec.sandbox.runtimeClassName", nil
	}
	if agent.Spec.PolicyRef == "" {
		return false, "PolicyRequired", "spec.policyRef is required", nil
	}

	var policy agentsv1alpha1.TenantPolicy
	err = r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: agent.Spec.PolicyRef}, &policy)
	if apierrors.IsNotFound(err) {
		return false, "PolicyNotFound", fmt.Sprintf("TenantPolicy %q does not exist", agent.Spec.PolicyRef), nil
	}
	if err != nil {
		return false, "PolicyLookupFailed", err.Error(), err
	}
	if policy.Labels[TenantIDLabel] != tenantID {
		return false, "TenantMismatch", "Agent and TenantPolicy tenant labels must match", nil
	}
	if !policy.Status.Active || policy.Status.ObservedGeneration != policy.Generation {
		return false, "PolicyNotActive", "referenced TenantPolicy is not active for its current generation", nil
	}
	modelRef := agent.Spec.Model.ProviderRef + "/" + agent.Spec.Model.Model
	if !allowed(policy.Spec.AllowedModels, modelRef) {
		return false, "ModelDenied", fmt.Sprintf("model %q is not allowed by TenantPolicy", modelRef), nil
	}
	for _, tool := range agent.Spec.Tools {
		if !allowed(policy.Spec.AllowedTools, tool.Ref) {
			return false, "ToolDenied", fmt.Sprintf("tool %q is not allowed by TenantPolicy", tool.Ref), nil
		}
	}
	return true, "AgentReady", "agent references and tenant policy are valid", nil
}

func allowed(values []string, candidate string) bool {
	for _, value := range values {
		if value == candidate {
			return true
		}
	}
	return false
}

func (r *AgentReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&agentsv1alpha1.Agent{}).
		Watches(
			&agentsv1alpha1.TenantPolicy{},
			handler.EnqueueRequestsFromMapFunc(r.agentsForPolicy),
		).
		Complete(r)
}

func (r *AgentReconciler) agentsForPolicy(ctx context.Context, object client.Object) []reconcile.Request {
	var agents agentsv1alpha1.AgentList
	if err := r.List(ctx, &agents, client.InNamespace(object.GetNamespace())); err != nil {
		log.FromContext(ctx).Error(err, "list Agents for TenantPolicy watch")
		return nil
	}
	requests := make([]reconcile.Request, 0)
	for _, agent := range agents.Items {
		if agent.Spec.PolicyRef == object.GetName() {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&agent)})
		}
	}
	return requests
}
