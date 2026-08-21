# Governed Agent Control Plane

This module projects `Agent`, `AgentRun`, and `TenantPolicy` resources into the
existing AgentRun-compatible control plane. Kubernetes owns desired state and a compact
status summary; PostgreSQL remains the system of record for execution events,
results, and idempotency.

The module's external interface is the three CRDs. Internally, controllers use
one `RunGateway` interface. The production adapter calls FastAPI; tests use an
in-memory adapter. The Operator never starts Node/Python runtimes itself.

## Local checks

```bash
go test ./...
kubectl kustomize config/default > /tmp/governed-agent-operator.yaml
```

Regenerate checked-in CRDs, RBAC, and deep-copy code after changing API types:

```bash
make generate manifests
```

## PoC deployment

1. Build and load `ghcr.io/6riemann9/governed-agent-control-plane:dev` into the test cluster.
2. Set `config/manager/manager.yaml` `apiUrl` to the cluster-internal FastAPI
   endpoint. Create `governed-agent-operator-credentials` with a `token` key when auth
   is enabled; never place the token in a CR.
3. Run `kubectl apply -k config/default`.
4. Replace the sample tenant/project UUIDs with IDs created by FastAPI, then
   apply `config/samples/tenant.yaml`, `agent.yaml`, and `agentrun.yaml` in that
   order.

`agents.governed.io/tenant-id` is mandatory on the Namespace and all three resources;
the values must match. Tenant users should not have permission to relabel their
Namespace. `Agent.spec.projectId` must belong to that tenant. The HTTP adapter
sends both values as control-plane headers and uses the CR UID as the default
idempotency key; FastAPI independently verifies membership and project tenancy.

## Helm deployment

`charts/governed-agent-operator/` packages the Operator as an installable chart with
the generated CRDs, RBAC, leader-election role, metrics Service and default
deny NetworkPolicy. It accepts an **existing** API-token Secret only; it never
renders a plaintext token from values. See [the chart guide](charts/governed-agent-operator/README.md)
for installation, CRD upgrade and validation commands.

## Image release

Build a local development image with `make docker-build`. Publishing a signed,
multi-architecture image is automated by `.github/workflows/operator-image.yml`:
push a `v<version>` tag or run the workflow manually with a version
tag. The workflow publishes `ghcr.io/6riemann9/governed-agent-control-plane`, produces an
SBOM and GitHub build provenance attestation, then keyless-signs the digest
with Cosign. Deploy immutable release tags or digests, never `:dev`.

