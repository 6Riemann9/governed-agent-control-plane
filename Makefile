CONTROLLER_GEN ?= go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.17.2
IMAGE ?= ghcr.io/6riemann9/governed-agent-control-plane:dev

.PHONY: generate manifests test build docker-build install uninstall deploy undeploy helm-lint helm-template

generate:
	$(CONTROLLER_GEN) object paths=./api/v1alpha1

manifests:
	$(CONTROLLER_GEN) crd:allowDangerousTypes=true paths=./api/v1alpha1 output:crd:artifacts:config=config/crd/bases
	$(CONTROLLER_GEN) rbac:roleName=governed-agent-operator paths=./controllers output:rbac:artifacts:config=config/rbac

test: generate manifests
	go test ./...

build:
	go build -o bin/operator ./cmd

docker-build:
	docker build -t $(IMAGE) .

install:
	kubectl apply -k config/crd

uninstall:
	kubectl delete -k config/crd --ignore-not-found

deploy:
	kubectl apply -k config/default

undeploy:
	kubectl delete -k config/default --ignore-not-found

helm-lint:
	helm lint charts/governed-agent-operator

helm-template:
	helm template governed-agent-operator charts/governed-agent-operator --namespace agent-control-system

