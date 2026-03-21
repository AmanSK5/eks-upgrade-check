"""Node group and AMI readiness checks."""

import re
from datetime import datetime
from eks_upgrade_check.checks import BaseCheck
from eks_upgrade_check.reporter import CheckResult


class NodeCheck(BaseCheck):
    """Check node groups, AMI types, and node health."""

    title = "Node Groups & AMIs"

    def run(self, context: dict) -> list[CheckResult]:
        results = []
        current = context["current_version"]
        target = context["target_version"]

        # Get managed node groups from EKS API
        try:
            nodegroups = self.aws.list_nodegroups()
        except Exception as e:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Could not list managed node groups: {e}",
            ))
            nodegroups = []

        if not nodegroups:
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message="No managed node groups found (cluster may use self-managed or Karpenter nodes)",
            ))

        for ng_name in nodegroups:
            try:
                ng_info = self.aws.describe_nodegroup(ng_name)
                ng = ng_info.get("nodegroup", {})
                self._check_nodegroup(results, ng, current, target)
            except Exception as e:
                results.append(CheckResult(
                    severity="WARNING",
                    check=self.title,
                    message=f"Could not describe node group {ng_name}: {e}",
                ))

        # Check individual node status from Kubernetes API
        try:
            nodes = self.k8s.get_nodes()
            self._check_node_health(results, nodes)
            self._check_node_versions(results, nodes, current)
        except Exception as e:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Could not list K8s nodes: {e}",
            ))

        return results

    def _check_nodegroup(self, results, ng, current, target):
        """Check a single managed node group."""
        ng_name = ng.get("nodegroupName", "unknown")
        status = ng.get("status", "unknown")
        ami_type = ng.get("amiType", "unknown")
        version = ng.get("version", "unknown")
        capacity_type = ng.get("capacityType", "ON_DEMAND")

        # Node group status
        if status == "ACTIVE":
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message=f"Node group '{ng_name}' is ACTIVE",
            ))
        else:
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"Node group '{ng_name}' status: {status}",
                recommendation=f"Resolve node group '{ng_name}' issues before upgrading.",
            ))

        # AMI type check — flag AL2 (approaching/past EOL)
        if "AL2_" in ami_type and "AL2023" not in ami_type:
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"Node group '{ng_name}' uses Amazon Linux 2 ({ami_type})",
                detail="Amazon Linux 2 has reached end of standard support. "
                       "AL2023 is the recommended replacement.",
                recommendation=f"Create a new node group with AL2023 AMI type to replace '{ng_name}'.",
                fix=f"aws eks create-nodegroup --cluster-name {self.aws.cluster_name} --nodegroup-name {ng_name}-al2023 --ami-type AL2023_x86_64_STANDARD ...",
            ))
        elif "AL2023" in ami_type:
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message=f"Node group '{ng_name}' uses AL2023 ({ami_type})",
            ))
        elif "BOTTLEROCKET" in ami_type:
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message=f"Node group '{ng_name}' uses Bottlerocket ({ami_type})",
            ))
        elif ami_type == "CUSTOM":
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Node group '{ng_name}' uses a custom AMI",
                detail="Custom AMIs require manual updates for new K8s versions. "
                       "Verify the AMI is compatible with the target version.",
                recommendation=f"Build or source a custom AMI compatible with K8s {target}.",
            ))

        # Node group K8s version vs cluster version
        if version != current:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Node group '{ng_name}' version ({version}) differs from cluster ({current})",
                detail="Node groups should match the control plane version before upgrading.",
                recommendation=f"Update node group '{ng_name}' to version {current} first.",
            ))

        # Launch template check
        lt = ng.get("launchTemplate")
        if lt:
            lt_id = lt.get("id", "")
            lt_version = lt.get("version", "$Latest")
            lt_info = self.aws.get_launch_template(lt_id, lt_version)
            if lt_info:
                lt_data = lt_info.get("LaunchTemplateData", {})
                ami_id = lt_data.get("ImageId")
                if ami_id:
                    results.append(CheckResult(
                        severity="INFO",
                        check=self.title,
                        message=f"Node group '{ng_name}' launch template AMI: {ami_id}",
                        detail=f"Launch template: {lt_id} (version {lt_version})",
                    ))

        # Scaling config
        scaling = ng.get("scalingConfig", {})
        desired = scaling.get("desiredSize", 0)
        min_size = scaling.get("minSize", 0)
        max_size = scaling.get("maxSize", 0)

        if desired == 0:
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message=f"Node group '{ng_name}' has 0 desired nodes (scaled down)",
            ))

        if max_size == desired and desired > 0:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Node group '{ng_name}' max equals desired ({max_size})",
                detail="During upgrade, you may need headroom for replacement nodes. "
                       "Consider temporarily increasing max size.",
                recommendation=f"Increase max size of '{ng_name}' to allow surge during upgrade.",
            ))

    def _check_node_health(self, results, nodes):
        """Check node Ready status."""
        not_ready = []
        for node in nodes:
            ready = False
            if node.status and node.status.conditions:
                for condition in node.status.conditions:
                    if condition.type == "Ready":
                        ready = condition.status == "True"
                        break
            if not ready:
                not_ready.append(node.metadata.name)

        if not_ready:
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"{len(not_ready)} node(s) are NOT Ready",
                detail=", ".join(not_ready[:5]) + (
                    f" ... and {len(not_ready) - 5} more" if len(not_ready) > 5 else ""
                ),
                recommendation="Investigate and fix NotReady nodes before upgrading.",
            ))
        else:
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message=f"All {len(nodes)} node(s) are Ready",
            ))

    def _check_node_versions(self, results, nodes, current_version):
        """Check kubelet versions across nodes for skew."""
        versions = {}
        for node in nodes:
            if node.status and node.status.node_info:
                kv = node.status.node_info.kubelet_version
                versions.setdefault(kv, []).append(node.metadata.name)

        if len(versions) > 1:
            detail_lines = [f"{v}: {len(names)} node(s)" for v, names in versions.items()]
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Mixed kubelet versions detected across nodes ({len(versions)} versions)",
                detail="\n".join(detail_lines),
                recommendation="Align all nodes to the same kubelet version before upgrading.",
            ))
        elif len(versions) == 1:
            version = list(versions.keys())[0]
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message=f"All nodes running kubelet {version}",
            ))

        # Check for nodes with taints that might block scheduling
        tainted_nodes = []
        for node in nodes:
            if node.spec and node.spec.taints:
                for taint in node.spec.taints:
                    if taint.effect == "NoSchedule" and taint.key not in (
                        "node.kubernetes.io/not-ready",
                        "node.kubernetes.io/unreachable",
                        "node.kubernetes.io/unschedulable",
                    ):
                        tainted_nodes.append(
                            f"{node.metadata.name} ({taint.key}={taint.value}:{taint.effect})"
                        )

        if tainted_nodes:
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message=f"{len(tainted_nodes)} node(s) have custom NoSchedule taints",
                detail="\n".join(tainted_nodes[:5]),
            ))
