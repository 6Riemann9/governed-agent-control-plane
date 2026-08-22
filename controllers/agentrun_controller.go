package controllers

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentsv1alpha1 "github.com/6Riemann9/governed-agent-control-plane/api/v1alpha1"
)

const (
	TenantIDLabel = "agents.governed.io/tenant-id"
	runFinalizer  = "agents.governed.io/agent-run-finalizer"
	pollInterval  = 2 * time.Second
)

// AgentRunReconciler projects one AgentRun CR into the durable Governed Agent
// control-plane ledger. It never starts runtime subprocesses in the Operator.
type AgentRunReconciler struct {
	client.Client
	Scheme  *runtime.Scheme
	Gateway RunGateway
}

// +kubebuilder:rbac:groups=agents.governed.io,resources=agentruns,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agents.governed.io,resources=agentruns/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=agents.governed.io,resources=agentruns/finalizers,verbs=update
// +kubebuilder:rbac:groups=agents.governed.io,resources=agents;tenantpolicies,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch

func (r *AgentRunReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	var run agentsv1alpha1.AgentRun
	if err := r.Get(ctx, request.NamespacedName, &run); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if !run.DeletionTimestamp.IsZero() {
		return r.reconcileDelete(ctx, &run)
	}
	if !controllerutil.ContainsFinalizer(&run, runFinalizer) {
		controllerutil.AddFinalizer(&run, runFinalizer)
		if err := r.Update(ctx, &run); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	tenantID, agent, err := r.validateReferences(ctx, &run)
	if err != nil {
		logger.Info("AgentRun references are not ready", "reason", err.Error())
		return r.reject(ctx, &run, "ReferencesNotReady", err.Error())
	}

	if run.Spec.Cancel {
		return r.reconcileCancel(ctx, &run, tenantID)
	}
	if run.Status.BackendRunID == "" {
		return r.reconcileSubmit(ctx, &run, tenantID, agent)
	}
	if isTerminal(run.Status.Phase) {
		if sanitizeTerminalStatus(&run) {
			if err := r.Status().Update(ctx, &run); err != nil {
				return ctrl.Result{}, err
			}
		}
		return r.reconcileTTL(ctx, &run)
	}
	return r.reconcilePoll(ctx, &run, tenantID)
}

func (r *AgentRunReconciler) validateReferences(
	ctx context.Context,
	run *agentsv1alpha1.AgentRun,
) (string, *agentsv1alpha1.Agent, error) {
	tenantID, err := tenantIDFor(ctx, r.Client, run)
	if err != nil {
		return "", nil, err
	}
	var agent agentsv1alpha1.Agent
	if err := r.Get(ctx, client.ObjectKey{Namespace: run.Namespace, Name: run.Spec.AgentRef}, &agent); err != nil {
		if apierrors.IsNotFound(err) {
			return "", nil, fmt.Errorf("Agent %q does not exist", run.Spec.AgentRef)
		}
		return "", nil, err
	}
	if agent.Labels[TenantIDLabel] != tenantID {
		return "", nil, fmt.Errorf("Agent and AgentRun tenant labels must match")
	}
	if !agent.Status.Ready || agent.Status.ObservedGeneration != agent.Generation {
		return "", nil, fmt.Errorf("Agent %q is not ready for its current generation", agent.Name)
	}
	if err := validateDAG(run.Spec.DAG); err != nil {
		return "", nil, err
	}
	return tenantID, &agent, nil
}

func validateDAG(dag *agentsv1alpha1.DAGSpec) error {
	if dag == nil || len(dag.Nodes) == 0 {
		return nil
	}
	dependencies := make(map[string][]string, len(dag.Nodes))
	for _, node := range dag.Nodes {
		if _, exists := dependencies[node.Name]; exists {
			return fmt.Errorf("DAG node %q is duplicated", node.Name)
		}
		dependencies[node.Name] = node.DependsOn
	}
	for node, deps := range dependencies {
		for _, dependency := range deps {
			if _, exists := dependencies[dependency]; !exists {
				return fmt.Errorf("DAG node %q depends on unknown node %q", node, dependency)
			}
		}
	}
	visiting := make(map[string]bool, len(dependencies))
	visited := make(map[string]bool, len(dependencies))
	var visit func(string) error
	visit = func(node string) error {
		if visiting[node] {
			return fmt.Errorf("DAG contains a cycle at node %q", node)
		}
		if visited[node] {
			return nil
		}
		visiting[node] = true
		for _, dependency := range dependencies[node] {
			if err := visit(dependency); err != nil {
				return err
			}
		}
		visiting[node] = false
		visited[node] = true
		return nil
	}
	for node := range dependencies {
		if err := visit(node); err != nil {
			return err
		}
	}
	return nil
}

func (r *AgentRunReconciler) reconcileSubmit(
	ctx context.Context,
	run *agentsv1alpha1.AgentRun,
	tenantID string,
	agent *agentsv1alpha1.Agent,
) (ctrl.Result, error) {
	idempotencyKey := run.Spec.IdempotencyKey
	if idempotencyKey == "" {
		idempotencyKey = string(run.UID)
	}
	nodes := make([]RunNode, 0)
	if run.Spec.DAG != nil {
		for _, node := range run.Spec.DAG.Nodes {
			prompt := node.Prompt
			if prompt == "" {
				prompt = run.Spec.Input.Task
			}
			projected := RunNode{
				Name:      node.Name,
				Prompt:    prompt,
				Role:      node.Role,
				Model:     agent.Spec.Model.Model,
				MaxTokens: agent.Spec.Model.MaxTokens,
				DependsOn: node.DependsOn,
			}
			if projected.Role == "" {
				projected.Role = agent.Spec.Runtime.Role
			}
			if run.Spec.RetryPolicy.MaxAttempts > 1 {
				maxRetries := run.Spec.RetryPolicy.MaxAttempts - 1
				projected.MaxRetries = &maxRetries
			}
			nodes = append(nodes, projected)
		}
	}
	if len(nodes) == 0 {
		nodes = append(nodes, RunNode{
			Name: "main", Prompt: run.Spec.Input.Task,
			Role: agent.Spec.Runtime.Role, Model: agent.Spec.Model.Model,
			MaxTokens: agent.Spec.Model.MaxTokens,
		})
	}
	controlRun, err := r.Gateway.Submit(ctx, RunSubmission{
		TenantID:       tenantID,
		ProjectID:      agent.Spec.ProjectID,
		Task:           run.Spec.Input.Task,
		Nodes:          nodes,
		IdempotencyKey: idempotencyKey,
	})
	if err != nil {
		return r.gatewayFailure(ctx, run, "SubmitFailed", err)
	}
	run.Status.BackendRunID = controlRun.ID
	run.Status.Attempts = 1
	run.Status.ObservedGeneration = run.Generation
	applyControlPlaneStatus(run, controlRun)
	markReferencesReady(run)
	markControlPlaneReady(run)
	meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
		Type:               "Accepted",
		Status:             metav1.ConditionTrue,
		Reason:             "ControlPlaneAccepted",
		Message:            "run is durably recorded by the Governed Agent control plane",
		ObservedGeneration: run.Generation,
	})
	if err := r.Status().Update(ctx, run); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: pollInterval}, nil
}

func (r *AgentRunReconciler) reconcilePoll(
	ctx context.Context,
	run *agentsv1alpha1.AgentRun,
	tenantID string,
) (ctrl.Result, error) {
	controlRun, err := r.Gateway.Get(ctx, tenantID, run.Status.BackendRunID)
	if err != nil {
		return r.gatewayFailure(ctx, run, "PollFailed", err)
	}
	before := run.Status.DeepCopy()
	applyControlPlaneStatus(run, controlRun)
	run.Status.ObservedGeneration = run.Generation
	markControlPlaneReady(run)
	if !reflect.DeepEqual(before, &run.Status) {
		if err := r.Status().Update(ctx, run); err != nil {
			return ctrl.Result{}, err
		}
	}
	if isTerminal(run.Status.Phase) {
		return r.reconcileTTL(ctx, run)
	}
	return ctrl.Result{RequeueAfter: pollInterval}, nil
}

func (r *AgentRunReconciler) reconcileCancel(
	ctx context.Context,
	run *agentsv1alpha1.AgentRun,
	tenantID string,
) (ctrl.Result, error) {
	if run.Status.BackendRunID == "" {
		run.Status.Phase = agentsv1alpha1.AgentRunCancelled
		run.Status.FinishedAt = now()
		run.Status.ObservedGeneration = run.Generation
		markReferencesReady(run)
		meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
			Type: "Cancelled", Status: metav1.ConditionTrue, Reason: "CancelledBeforeSubmit",
			Message: "run was cancelled before control-plane submission", ObservedGeneration: run.Generation,
		})
		return ctrl.Result{}, r.Status().Update(ctx, run)
	}
	controlRun, err := r.Gateway.Cancel(ctx, tenantID, run.Status.BackendRunID)
	if err != nil {
		return r.gatewayFailure(ctx, run, "CancelFailed", err)
	}
	applyControlPlaneStatus(run, controlRun)
	run.Status.ObservedGeneration = run.Generation
	markReferencesReady(run)
	markControlPlaneReady(run)
	meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
		Type: "CancelRequested", Status: metav1.ConditionTrue, Reason: "ControlPlaneNotified",
		Message: "cancellation was accepted by the Governed Agent control plane", ObservedGeneration: run.Generation,
	})
	if err := r.Status().Update(ctx, run); err != nil {
		return ctrl.Result{}, err
	}
	if isTerminal(run.Status.Phase) {
		return r.reconcileTTL(ctx, run)
	}
	return ctrl.Result{RequeueAfter: pollInterval}, nil
}

func (r *AgentRunReconciler) reconcileTTL(ctx context.Context, run *agentsv1alpha1.AgentRun) (ctrl.Result, error) {
	if run.Spec.TTLSecondsAfterFinished == 0 || run.Status.FinishedAt == nil {
		return ctrl.Result{}, nil
	}
	expiresAt := run.Status.FinishedAt.Add(time.Duration(run.Spec.TTLSecondsAfterFinished) * time.Second)
	remaining := time.Until(expiresAt)
	if remaining <= 0 {
		return ctrl.Result{}, client.IgnoreNotFound(r.Delete(ctx, run))
	}
	return ctrl.Result{RequeueAfter: remaining}, nil
}

func (r *AgentRunReconciler) reconcileDelete(ctx context.Context, run *agentsv1alpha1.AgentRun) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(run, runFinalizer) {
		return ctrl.Result{}, nil
	}
	tenantID := run.Labels[TenantIDLabel]
	if run.Status.BackendRunID != "" && !isTerminal(run.Status.Phase) {
		controlRun, err := r.Gateway.Cancel(ctx, tenantID, run.Status.BackendRunID)
		if err != nil {
			var httpErr *GatewayHTTPError
			if !errors.As(err, &httpErr) || httpErr.StatusCode != http.StatusNotFound {
				return ctrl.Result{}, err
			}
		} else {
			applyControlPlaneStatus(run, controlRun)
			if err := r.Status().Update(ctx, run); err != nil {
				return ctrl.Result{}, err
			}
			if !isTerminal(run.Status.Phase) {
				return ctrl.Result{RequeueAfter: pollInterval}, nil
			}
		}
	}
	controllerutil.RemoveFinalizer(run, runFinalizer)
	return ctrl.Result{}, r.Update(ctx, run)
}

func (r *AgentRunReconciler) reject(
	ctx context.Context,
	run *agentsv1alpha1.AgentRun,
	reason, message string,
) (ctrl.Result, error) {
	run.Status.ObservedGeneration = run.Generation
	meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
		Type: "Ready", Status: metav1.ConditionFalse, Reason: reason,
		Message: message, ObservedGeneration: run.Generation,
	})
	if err := r.Status().Update(ctx, run); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: 15 * time.Second}, nil
}

func (r *AgentRunReconciler) gatewayFailure(
	ctx context.Context,
	run *agentsv1alpha1.AgentRun,
	reason string,
	err error,
) (ctrl.Result, error) {
	message := err.Error()
	if len(message) > 1024 {
		message = message[:1024]
	}
	meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
		Type: "ControlPlaneReady", Status: metav1.ConditionFalse, Reason: reason,
		Message: message, ObservedGeneration: run.Generation,
	})
	if statusErr := r.Status().Update(ctx, run); statusErr != nil {
		return ctrl.Result{}, statusErr
	}
	return ctrl.Result{RequeueAfter: 10 * time.Second}, nil
}

func applyControlPlaneStatus(run *agentsv1alpha1.AgentRun, control ControlPlaneRun) {
	if control.ID != "" {
		run.Status.BackendRunID = control.ID
	}
	run.Status.Phase = projectPhase(control.Status)
	if control.StartedAt != nil {
		t := metav1.NewTime(*control.StartedAt)
		run.Status.StartedAt = &t
	} else if run.Status.Phase == agentsv1alpha1.AgentRunRunning && run.Status.StartedAt == nil {
		run.Status.StartedAt = now()
	}
	if control.FinishedAt != nil {
		t := metav1.NewTime(*control.FinishedAt)
		run.Status.FinishedAt = &t
	} else if isTerminal(run.Status.Phase) && run.Status.FinishedAt == nil {
		run.Status.FinishedAt = now()
	}
	if run.Status.Phase == agentsv1alpha1.AgentRunSucceeded {
		run.Status.ResultRef = "governed://agent-runs/" + run.Status.BackendRunID
	}
	if run.Status.Phase == agentsv1alpha1.AgentRunFailed {
		run.Status.ErrorCode = control.ErrorCode
		if run.Status.ErrorCode == "" {
			run.Status.ErrorCode = "RUN_FAILED"
		}
	}
	// A Kubernetes status is intentionally a compact execution ledger.  The
	// control-plane summary can include model output, so keep it in the backend
	// and expose only a stable lifecycle message and result references here.
	run.Status.Message = controlPlaneStatusMessage(run.Status.Phase)
	run.Status.NodeStates = make([]agentsv1alpha1.NodeState, 0, len(control.Steps))
	for _, step := range control.Steps {
		state := agentsv1alpha1.NodeState{Name: step.Name, Phase: projectPhase(step.Status)}
		if state.Phase == agentsv1alpha1.AgentRunSucceeded {
			state.ResultRef = fmt.Sprintf("governed://agent-runs/%s/nodes/%s", run.Status.BackendRunID, step.Name)
		}
		run.Status.NodeStates = append(run.Status.NodeStates, state)
	}
	conditionStatus := metav1.ConditionUnknown
	conditionReason := "RunInProgress"
	if run.Status.Phase == agentsv1alpha1.AgentRunSucceeded {
		conditionStatus, conditionReason = metav1.ConditionTrue, "RunSucceeded"
	} else if run.Status.Phase == agentsv1alpha1.AgentRunFailed || run.Status.Phase == agentsv1alpha1.AgentRunCancelled {
		conditionStatus, conditionReason = metav1.ConditionFalse, "Run"+string(run.Status.Phase)
	}
	meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
		Type: "Completed", Status: conditionStatus, Reason: conditionReason,
		Message: run.Status.Message, ObservedGeneration: run.Generation,
	})
}

func controlPlaneStatusMessage(phase agentsv1alpha1.AgentRunPhase) string {
	switch phase {
	case agentsv1alpha1.AgentRunSucceeded:
		return "run succeeded; inspect resultRef for result metadata"
	case agentsv1alpha1.AgentRunFailed:
		return "run failed; inspect the Governed Agent control-plane run ledger"
	case agentsv1alpha1.AgentRunCancelled:
		return "run cancelled by the Governed Agent control plane"
	case agentsv1alpha1.AgentRunRunning:
		return "run is executing in the Governed Agent control plane"
	default:
		return "run is queued in the Governed Agent control plane"
	}
}

func markReferencesReady(run *agentsv1alpha1.AgentRun) {
	meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
		Type:               "Ready",
		Status:             metav1.ConditionTrue,
		Reason:             "ReferencesResolved",
		Message:            "tenant binding, Agent, and DAG references are valid",
		ObservedGeneration: run.Generation,
	})
}

func markControlPlaneReady(run *agentsv1alpha1.AgentRun) {
	meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
		Type:               "ControlPlaneReady",
		Status:             metav1.ConditionTrue,
		Reason:             "RunGatewayReachable",
		Message:            "Governed Agent control-plane run gateway is reachable",
		ObservedGeneration: run.Generation,
	})
}

// sanitizeTerminalStatus removes output that an earlier operator version may
// have copied into CR status. It also makes upgraded controllers self-healing
// without requiring a one-off migration of AgentRun resources.
func sanitizeTerminalStatus(run *agentsv1alpha1.AgentRun) bool {
	if !isTerminal(run.Status.Phase) {
		return false
	}
	before := run.Status.DeepCopy()
	run.Status.Message = controlPlaneStatusMessage(run.Status.Phase)
	if run.Status.BackendRunID != "" {
		// A terminal status with a durable backend ID was necessarily received
		// from RunGateway. Clear a stale transient gateway failure after an
		// Operator restart without polling and rewriting terminal history.
		markControlPlaneReady(run)
	}
	conditionStatus := metav1.ConditionTrue
	conditionReason := "RunSucceeded"
	if run.Status.Phase == agentsv1alpha1.AgentRunFailed || run.Status.Phase == agentsv1alpha1.AgentRunCancelled {
		conditionStatus = metav1.ConditionFalse
		conditionReason = "Run" + string(run.Status.Phase)
	}
	meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
		Type: "Completed", Status: conditionStatus, Reason: conditionReason,
		Message: run.Status.Message, ObservedGeneration: run.Generation,
	})
	return !reflect.DeepEqual(before, &run.Status)
}

func projectPhase(status string) agentsv1alpha1.AgentRunPhase {
	switch strings.ToLower(status) {
	case "pending", "queued":
		return agentsv1alpha1.AgentRunQueued
	case "dispatching", "running":
		return agentsv1alpha1.AgentRunRunning
	case "done", "succeeded", "completed":
		return agentsv1alpha1.AgentRunSucceeded
	case "failed", "error":
		return agentsv1alpha1.AgentRunFailed
	case "cancelled", "canceled":
		return agentsv1alpha1.AgentRunCancelled
	default:
		return agentsv1alpha1.AgentRunQueued
	}
}

func isTerminal(phase agentsv1alpha1.AgentRunPhase) bool {
	return phase == agentsv1alpha1.AgentRunSucceeded ||
		phase == agentsv1alpha1.AgentRunFailed ||
		phase == agentsv1alpha1.AgentRunCancelled
}

func now() *metav1.Time {
	timestamp := metav1.Now()
	return &timestamp
}

func (r *AgentRunReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&agentsv1alpha1.AgentRun{}).
		Watches(
			&agentsv1alpha1.Agent{},
			handler.EnqueueRequestsFromMapFunc(r.runsForAgent),
		).
		Complete(r)
}

func (r *AgentRunReconciler) runsForAgent(ctx context.Context, object client.Object) []reconcile.Request {
	var runs agentsv1alpha1.AgentRunList
	if err := r.List(ctx, &runs, client.InNamespace(object.GetNamespace())); err != nil {
		log.FromContext(ctx).Error(err, "list AgentRuns for Agent watch")
		return nil
	}
	requests := make([]reconcile.Request, 0)
	for _, run := range runs.Items {
		if run.Spec.AgentRef == object.GetName() && !isTerminal(run.Status.Phase) {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&run)})
		}
	}
	return requests
}
