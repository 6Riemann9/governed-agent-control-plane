package controllers

import (
	"context"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentsv1alpha1 "github.com/6Riemann9/governed-agent-control-plane/api/v1alpha1"
)

type memoryRunGateway struct {
	submissions []RunSubmission
	getCalls    int
	run         ControlPlaneRun
}

func (gateway *memoryRunGateway) Submit(_ context.Context, submission RunSubmission) (ControlPlaneRun, error) {
	gateway.submissions = append(gateway.submissions, submission)
	return gateway.run, nil
}

func (gateway *memoryRunGateway) Get(_ context.Context, _, _ string) (ControlPlaneRun, error) {
	gateway.getCalls++
	return gateway.run, nil
}

func (gateway *memoryRunGateway) Cancel(_ context.Context, _, _ string) (ControlPlaneRun, error) {
	gateway.run.Status = "cancelled"
	return gateway.run, nil
}

func TestAgentRunReconcileSubmitsOnceAcrossRepeatedReconcile(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := agentsv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	tenantID := "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
	projectID := "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
	namespace := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name: "tenant-a", Labels: map[string]string{TenantIDLabel: tenantID},
	}}
	agent := &agentsv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "research", Namespace: "tenant-a", Generation: 1, Labels: map[string]string{TenantIDLabel: tenantID}},
		Spec:       agentsv1alpha1.AgentSpec{ProjectID: projectID},
		Status:     agentsv1alpha1.AgentStatus{Ready: true, ObservedGeneration: 1},
	}
	run := &agentsv1alpha1.AgentRun{
		ObjectMeta: metav1.ObjectMeta{Name: "research-1", Namespace: "tenant-a", UID: types.UID("uid-1"), Generation: 1, Labels: map[string]string{TenantIDLabel: tenantID}},
		Spec: agentsv1alpha1.AgentRunSpec{
			AgentRef: "research",
			Input:    agentsv1alpha1.AgentRunInput{Task: "compare retrieval"},
			DAG: &agentsv1alpha1.DAGSpec{Nodes: []agentsv1alpha1.DAGNode{
				{Name: "retrieve", Role: "researcher"},
				{Name: "review", Role: "analyst", DependsOn: []string{"retrieve"}},
			}},
		},
	}
	client := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&agentsv1alpha1.Agent{}, &agentsv1alpha1.AgentRun{}).
		WithObjects(namespace, agent, run).
		Build()
	gateway := &memoryRunGateway{run: ControlPlaneRun{ID: "backend-run-1", Status: "queued"}}
	reconciler := &AgentRunReconciler{Client: client, Scheme: scheme, Gateway: gateway}
	request := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: run.Namespace, Name: run.Name}}

	// First pass persists the finalizer; second pass performs the idempotent submit.
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatalf("add finalizer: %v", err)
	}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatalf("submit: %v", err)
	}
	if len(gateway.submissions) != 1 {
		t.Fatalf("submissions = %d, want 1", len(gateway.submissions))
	}
	if gateway.submissions[0].IdempotencyKey != "uid-1" || gateway.submissions[0].ProjectID != projectID {
		t.Fatalf("unexpected submission: %#v", gateway.submissions[0])
	}
	if len(gateway.submissions[0].Nodes) != 2 || gateway.submissions[0].Nodes[0].Prompt != "compare retrieval" {
		t.Fatalf("unexpected projected DAG: %#v", gateway.submissions[0].Nodes)
	}

	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatalf("poll: %v", err)
	}
	if len(gateway.submissions) != 1 || gateway.getCalls != 1 {
		t.Fatalf("submit calls=%d get calls=%d", len(gateway.submissions), gateway.getCalls)
	}
	var stored agentsv1alpha1.AgentRun
	if err := client.Get(context.Background(), request.NamespacedName, &stored); err != nil {
		t.Fatal(err)
	}
	if stored.Status.BackendRunID != "backend-run-1" || stored.Status.Phase != agentsv1alpha1.AgentRunQueued {
		t.Fatalf("unexpected status: %#v", stored.Status)
	}
	ready := meta.FindStatusCondition(stored.Status.Conditions, "Ready")
	if ready == nil || ready.Status != metav1.ConditionTrue || ready.Reason != "ReferencesResolved" {
		t.Fatalf("run references did not recover to Ready=True: %#v", ready)
	}
	controlPlane := meta.FindStatusCondition(stored.Status.Conditions, "ControlPlaneReady")
	if controlPlane == nil || controlPlane.Status != metav1.ConditionTrue || controlPlane.Reason != "RunGatewayReachable" {
		t.Fatalf("successful submission did not mark the control plane ready: %#v", controlPlane)
	}
}

func TestTenantIDForRejectsResourceNamespaceMismatch(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	namespace := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name: "tenant-a", Labels: map[string]string{TenantIDLabel: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
	}}
	run := &agentsv1alpha1.AgentRun{ObjectMeta: metav1.ObjectMeta{
		Name: "bad", Namespace: namespace.Name,
		Labels: map[string]string{TenantIDLabel: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
	}}
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithObjects(namespace).Build()
	if _, err := tenantIDFor(context.Background(), kubernetes, run); err == nil {
		t.Fatal("expected mismatched CR and Namespace tenant labels to be rejected")
	}
}

func TestValidateDAGRejectsCyclesAndUnknownDependencies(t *testing.T) {
	cycle := &agentsv1alpha1.DAGSpec{Nodes: []agentsv1alpha1.DAGNode{
		{Name: "a", DependsOn: []string{"b"}},
		{Name: "b", DependsOn: []string{"a"}},
	}}
	if err := validateDAG(cycle); err == nil {
		t.Fatal("expected cycle to be rejected")
	}
	unknown := &agentsv1alpha1.DAGSpec{Nodes: []agentsv1alpha1.DAGNode{
		{Name: "a", DependsOn: []string{"missing"}},
	}}
	if err := validateDAG(unknown); err == nil {
		t.Fatal("expected unknown dependency to be rejected")
	}
}

func TestControlPlaneStatusKeepsBackendOutputOutOfCRStatus(t *testing.T) {
	run := &agentsv1alpha1.AgentRun{ObjectMeta: metav1.ObjectMeta{Generation: 3}}
	applyControlPlaneStatus(run, ControlPlaneRun{
		ID:      "backend-run-1",
		Status:  "succeeded",
		Summary: strings.Repeat("private model output ", 200),
		Steps:   []ControlPlaneStep{{Name: "retrieve", Status: "succeeded"}},
	})

	const want = "run succeeded; inspect resultRef for result metadata"
	if run.Status.Message != want {
		t.Fatalf("status message = %q, want compact lifecycle message", run.Status.Message)
	}
	completed := meta.FindStatusCondition(run.Status.Conditions, "Completed")
	if completed == nil || completed.Message != want {
		t.Fatalf("completed condition leaked control-plane summary: %#v", completed)
	}
	if got := run.Status.NodeStates[0].ResultRef; got != "governed://agent-runs/backend-run-1/nodes/retrieve" {
		t.Fatalf("node result reference = %q", got)
	}
}

func TestSanitizeTerminalStatusRemovesLegacySummary(t *testing.T) {
	run := &agentsv1alpha1.AgentRun{
		ObjectMeta: metav1.ObjectMeta{Generation: 2},
		Status: agentsv1alpha1.AgentRunStatus{
			Phase:        agentsv1alpha1.AgentRunSucceeded,
			BackendRunID: "backend-run-1",
			Message:      strings.Repeat("legacy model output ", 200),
			Conditions: []metav1.Condition{{
				Type: "Completed", Status: metav1.ConditionTrue, Reason: "RunSucceeded",
				Message: strings.Repeat("legacy model output ", 200), ObservedGeneration: 2,
			}, {
				Type: "ControlPlaneReady", Status: metav1.ConditionFalse, Reason: "SubmitFailed",
				Message: "legacy transient error", ObservedGeneration: 2,
			}},
		},
	}
	if !sanitizeTerminalStatus(run) {
		t.Fatal("expected legacy status to be sanitized")
	}
	if strings.Contains(run.Status.Message, "legacy") {
		t.Fatalf("legacy output remains in status: %q", run.Status.Message)
	}
	controlPlane := meta.FindStatusCondition(run.Status.Conditions, "ControlPlaneReady")
	if controlPlane == nil || controlPlane.Status != metav1.ConditionTrue {
		t.Fatalf("legacy gateway failure was not cleared: %#v", controlPlane)
	}
	if sanitizeTerminalStatus(run) {
		t.Fatal("sanitizing an already compact status should not write again")
	}
}
