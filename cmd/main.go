package main

import (
	"flag"
	"os"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	agentsv1alpha1 "github.com/6Riemann9/governed-agent-control-plane/api/v1alpha1"
	"github.com/6Riemann9/governed-agent-control-plane/controllers"
)

var scheme = runtime.NewScheme()

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(agentsv1alpha1.AddToScheme(scheme))
}

func main() {
	var metricsAddress string
	var probeAddress string
	var leaderElection bool
	flag.StringVar(&metricsAddress, "metrics-bind-address", ":8080", "Address for Prometheus metrics.")
	flag.StringVar(&probeAddress, "health-probe-bind-address", ":8081", "Address for health probes.")
	flag.BoolVar(&leaderElection, "leader-elect", true, "Use leader election for a single active reconciler.")
	logging := zap.Options{Development: false}
	logging.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&logging)))

	manager, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsAddress},
		HealthProbeBindAddress: probeAddress,
		LeaderElection:         leaderElection,
		LeaderElectionID:       "agents.governed.io",
	})
	if err != nil {
		ctrl.Log.Error(err, "create manager")
		os.Exit(1)
	}

	if err := (&controllers.TenantPolicyReconciler{Client: manager.GetClient(), Scheme: manager.GetScheme()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "register TenantPolicy reconciler")
		os.Exit(1)
	}
	if err := (&controllers.AgentReconciler{Client: manager.GetClient(), Scheme: manager.GetScheme()}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "register Agent reconciler")
		os.Exit(1)
	}
	if err := (&controllers.AgentRunReconciler{
		Client: manager.GetClient(), Scheme: manager.GetScheme(), Gateway: controllers.NewHTTPRunGatewayFromEnv(),
	}).SetupWithManager(manager); err != nil {
		ctrl.Log.Error(err, "register AgentRun reconciler")
		os.Exit(1)
	}
	if err := manager.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		ctrl.Log.Error(err, "register health check")
		os.Exit(1)
	}
	if err := manager.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		ctrl.Log.Error(err, "register readiness check")
		os.Exit(1)
	}

	ctrl.Log.Info("starting Governed Agent Operator")
	if err := manager.Start(ctrl.SetupSignalHandler()); err != nil {
		ctrl.Log.Error(err, "run manager")
		os.Exit(1)
	}
}
