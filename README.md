# Governed Agent Control Plane

This module projects `Agent`, `AgentRun`, and `TenantPolicy` resources into the
existing AgentRun-compatible control plane. Kubernetes owns desired state and a compact
status summary; PostgreSQL remains the system of record for execution events,
results, and idempotency.

For the first MVP, this repository also includes a small FastAPI data plane in
`backend/`. It implements the Operator HTTP contract, persists run and node
state to PostgreSQL, and runs deterministic mock tasks by default. An opt-in
xindu overlay enables OpenAI-compatible model calls for live demonstrations.

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

Run the data-plane tests and build both local images:

```bash
make api-test
make docker-build docker-build-api
```

## Local Kubernetes database

The Kustomize deployment requires an existing `governed-database` Secret. Create
one locally without committing its password:

```powershell
$password = [guid]::NewGuid().ToString("N")
$url = "postgresql://governed:${password}@governed-postgres:5432/governed"
kubectl -n agent-control-system create secret generic governed-database `
  --from-literal=username=governed `
  --from-literal=password=$password `
  --from-literal=url=$url
```

Then run `kubectl apply -k config/default`. PostgreSQL is an internal ClusterIP
StatefulSet with a 5Gi persistent volume. The API applies versioned SQL
migrations under `backend/migrations/` while holding a PostgreSQL advisory lock.

## xindu live model mode

The default deployment remains in deterministic `mock` mode. The xindu overlay
switches the API to OpenAI-compatible live calls using
`https://xindu.xyz/v1` and `deepseek-v4-flash`. Keep the key out of shell
history and source control:

```powershell
$apiKey = Read-Host "xindu API key"
kubectl -n agent-control-system create secret generic governed-llm `
  --from-literal=api-key=$apiKey `
  --dry-run=client -o yaml | kubectl apply -f -
Remove-Variable apiKey
kubectl apply -k config/overlays/xindu
```

The API records each node's output, latency, input tokens and output tokens in
PostgreSQL. Provider errors are stored as compact status messages and never
include the API key or raw provider response body. DAG dependencies are validated
at the API boundary and executed topologically; each node honors its retry
budget. The Agent model and max-token budget are projected into each durable
node record and sent to the provider. To return to safe mock mode, apply
`kubectl apply -k config/default`.

The default-deny operator NetworkPolicy permits DNS, the API service, and the
Kubernetes API Server (`kube-system/component=kube-apiserver` on local clusters).
Override that selector for clusters whose API Server is external to the cluster.

## PoC deployment

1. Build and load `ghcr.io/6riemann9/governed-agent-control-plane:dev` and
   `governed-agent-api:dev` into the test cluster.
2. Run `kubectl apply -k config/default`. It creates the API Service named
   `agent-control-api`; the Operator points to it by default.
3. Replace the sample tenant/project UUIDs with IDs created by FastAPI, then
   apply `config/samples/tenant.yaml`, `agent.yaml`, and `agentrun.yaml` in that
   order.

Within a few seconds, `kubectl get agentrun research-example -n tenant-acme -o yaml`
will show a `Succeeded` status and compact per-node result references.

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

