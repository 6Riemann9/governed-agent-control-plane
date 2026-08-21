# Governed Agent Operator Helm Chart

This chart deploys the Governed Agent Operator, its CRDs, RBAC, metrics Service and a
default-deny NetworkPolicy. The controller calls the existing FastAPI AgentRun
contract; it does not run Node or Python workloads in the controller Pod.

## Install

Create an API token Secret only when FastAPI authentication is enabled:

```bash
kubectl -n agent-control-system create secret generic governed-agent-operator-credentials \
  --from-literal=token="$GOVERNED_OPERATOR_TOKEN"
helm upgrade --install governed-agent-operator ./charts/governed-agent-operator \
  --namespace agent-control-system --create-namespace \
  --set api.url=http://agent-control-api.agent-control-system.svc:8000 \
  --set credentials.existingSecret=governed-agent-operator-credentials
```

Do not pass a token with `--set`; values can be recorded in shell history and
Helm release metadata. For local unauthenticated development, omit
`credentials.existingSecret`.

The CRDs in `crds/` are installed on a fresh install. Helm deliberately does
not upgrade CRDs, so apply generated CRDs explicitly during an Operator API
upgrade before `helm upgrade`:

```bash
kubectl apply -k config/crd
helm upgrade governed-agent-operator ./charts/governed-agent-operator \
  --namespace agent-control-system
```

## Important values

| Value | Purpose |
|---|---|
| `image.repository`, `image.tag` | Signed Operator image to deploy |
| `api.url` | Cluster-internal FastAPI control-plane endpoint |
| `credentials.existingSecret` | Existing Secret with the API token; never plaintext values |
| `metrics.serviceMonitor.enabled` | Render a Prometheus Operator `ServiceMonitor`; requires its CRD |
| `networkPolicy.metricsNamespace` | Only Namespace allowed to scrape the metrics Service |
| `networkPolicy.backend.namespace` | Backend namespace; blank means release namespace |
| `networkPolicy.backend.selector` | Labels selecting only backend Pods |

Validate without contacting a cluster:

```bash
helm lint charts/governed-agent-operator
helm template governed-agent-operator charts/governed-agent-operator --namespace agent-control-system
```

