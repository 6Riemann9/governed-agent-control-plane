package controllers

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// tenantIDFor resolves tenant identity from the administrator-controlled
// Namespace and requires the namespaced resource to repeat the same value.
// A CR author cannot select another Governed Agent tenant by changing only a CR label.
func tenantIDFor(ctx context.Context, kubernetes client.Client, object client.Object) (string, error) {
	var namespace corev1.Namespace
	if err := kubernetes.Get(ctx, client.ObjectKey{Name: object.GetNamespace()}, &namespace); err != nil {
		return "", fmt.Errorf("read Namespace tenant binding: %w", err)
	}
	namespaceTenantID := namespace.Labels[TenantIDLabel]
	if _, err := uuid.Parse(namespaceTenantID); err != nil {
		return "", fmt.Errorf("Namespace label %q must be a tenant UUID", TenantIDLabel)
	}
	if object.GetLabels()[TenantIDLabel] != namespaceTenantID {
		return "", fmt.Errorf("resource tenant label must match its Namespace tenant binding")
	}
	return namespaceTenantID, nil
}
