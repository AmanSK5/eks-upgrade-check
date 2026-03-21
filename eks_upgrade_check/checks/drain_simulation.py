"""Predicts what would happen if each node were drained during an upgrade.

Doesn't use 'kubectl drain --dry-run' — that only validates syntax.
Instead, this checks pods against PDBs, replica counts, and controllers
to work out which nodes would actually block and which workloads would
go down. More useful than anything AWS surfaces natively.
"""

from eks_upgrade_check.checks import BaseCheck
from eks_upgrade_check.reporter import CheckResult


class DrainSimulationCheck(BaseCheck):
    """Per-node drain impact analysis.
    
    Checks each pod for: PDB violations, single-replica downtime,
    standalone pods (no controller), and emptyDir data loss.
    DaemonSet pods are skipped since they survive drains.
    """

    title = "Drain Impact Analysis"

    def run(self, context: dict) -> list[CheckResult]:
        results = []

        # Gather cluster-wide data
        try:
            nodes = self.k8s.get_nodes()
        except Exception as e:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Could not list nodes: {e}",
            ))
            return results

        if not nodes:
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message="No nodes found to analyse",
            ))
            return results

        try:
            all_pods = self.k8s.get_pods()
        except Exception as e:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Could not list pods: {e}",
            ))
            return results

        try:
            all_pdbs = self.k8s.get_pdbs()
        except Exception:
            all_pdbs = []

        # Build PDB lookup: namespace -> list of PDB info
        pdb_lookup = {}
        for pdb in all_pdbs:
            ns = pdb.metadata.namespace
            pdb_lookup.setdefault(ns, []).append({
                "name": pdb.metadata.name,
                "selector": pdb.spec.selector,
                "disruptions_allowed": pdb.status.disruptions_allowed if pdb.status else 0,
                "expected_pods": pdb.status.expected_pods if pdb.status else 0,
                "current_healthy": pdb.status.current_healthy if pdb.status else 0,
            })

        # Get replica counts for deployments and statefulsets
        replica_lookup = {}
        try:
            for dep in self.k8s.get_deployments():
                key = f"{dep.metadata.namespace}/{dep.metadata.name}"
                replica_lookup[key] = {
                    "kind": "Deployment",
                    "replicas": dep.spec.replicas or 0,
                    "ready": dep.status.ready_replicas or 0,
                }
        except Exception:
            pass

        try:
            for sts in self.k8s.get_statefulsets():
                key = f"{sts.metadata.namespace}/{sts.metadata.name}"
                replica_lookup[key] = {
                    "kind": "StatefulSet",
                    "replicas": sts.spec.replicas or 0,
                    "ready": sts.status.ready_replicas or 0,
                }
        except Exception:
            pass

        results.append(CheckResult(
            severity="INFO",
            check=self.title,
            message=f"Analysing drain impact on {len(nodes)} node(s)...",
        ))

        # Analyse each node
        blocked_nodes = 0
        risky_nodes = 0
        safe_nodes = 0

        for node in nodes:
            node_name = node.metadata.name
            node_pods = [
                p for p in all_pods
                if p.spec and p.spec.node_name == node_name
            ]

            analysis = self._analyse_node(
                node_name, node_pods, pdb_lookup, replica_lookup
            )

            # Classify this node
            if analysis["hard_blockers"]:
                blocked_nodes += 1
                blocker_lines = [f"✗ {b}" for b in analysis["hard_blockers"]]
                risk_lines = [f"⚠ {r}" for r in analysis["soft_risks"]]
                safe_lines = [f"✓ {len(analysis['safe_pods'])} pod(s) safe to evict"]

                detail = "\n".join(blocker_lines + risk_lines + safe_lines)

                results.append(CheckResult(
                    severity="ERROR",
                    check=self.title,
                    message=f"Node '{node_name}' — BLOCKED",
                    detail=detail,
                    recommendation=f"Resolve drain blockers on '{node_name}' before upgrading.",
                ))

            elif analysis["soft_risks"]:
                risky_nodes += 1
                risk_lines = [f"⚠ {r}" for r in analysis["soft_risks"]]
                safe_lines = [f"✓ {len(analysis['safe_pods'])} pod(s) safe to evict"]

                detail = "\n".join(risk_lines + safe_lines)

                results.append(CheckResult(
                    severity="WARNING",
                    check=self.title,
                    message=f"Node '{node_name}' — drainable with RISKS ({len(analysis['soft_risks'])})",
                    detail=detail,
                ))

            else:
                safe_nodes += 1
                results.append(CheckResult(
                    severity="PASS",
                    check=self.title,
                    message=f"Node '{node_name}' — safe to drain ({len(analysis['safe_pods'])} pod(s) would be evicted)",
                ))

        # Overall summary
        results.append(CheckResult(
            severity="INFO",
            check=self.title,
            message=f"Drain impact: {safe_nodes} safe, {risky_nodes} risky, {blocked_nodes} blocked out of {len(nodes)} node(s)",
        ))

        if blocked_nodes > 0:
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"{blocked_nodes} node(s) would be BLOCKED during drain",
                recommendation="Fix drain blockers before upgrading. PDB violations are the most common cause.",
            ))
        elif risky_nodes > 0:
            # Calculate safe node capacity
            max_safe_simultaneous = safe_nodes
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Cluster can safely lose {max_safe_simultaneous} of {len(nodes)} node(s) at a time",
                detail="Risky nodes have single-replica workloads or standalone pods that "
                       "will cause brief downtime or data loss during drain.",
            ))
        else:
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message=f"All {len(nodes)} node(s) can be drained safely",
            ))

        return results

    def _analyse_node(self, node_name, pods, pdb_lookup, replica_lookup):
        """Analyse the drain impact for a single node."""
        analysis = {
            "hard_blockers": [],    # Will prevent drain
            "soft_risks": [],       # Won't block but cause issues
            "safe_pods": [],        # Fine to evict
        }

        for pod in pods:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            pod_id = f"{ns}/{name}"

            # Skip completed/succeeded pods
            if pod.status and pod.status.phase in ("Succeeded", "Failed"):
                continue

            # Determine owner
            owners = pod.metadata.owner_references or []
            owner_kinds = [o.kind for o in owners]
            owner_names = [o.name for o in owners]

            # DaemonSet pods are skipped during drain — always safe
            if "DaemonSet" in owner_kinds:
                continue

            # Check 1: PDB blocking
            pdb_blocked = self._check_pdb_blocking(pod, pdb_lookup.get(ns, []))
            if pdb_blocked:
                analysis["hard_blockers"].append(
                    f"{pod_id} — PDB blocks eviction ({pdb_blocked})"
                )
                continue

            # Check 2: Standalone pods (no controller)
            if not owners:
                analysis["soft_risks"].append(
                    f"{pod_id} — standalone pod (no controller, will be permanently deleted)"
                )
                continue

            has_known_controller = any(
                o in ("ReplicaSet", "StatefulSet", "Job") for o in owner_kinds
            )
            if not has_known_controller:
                analysis["soft_risks"].append(
                    f"{pod_id} — unmanaged pod (owner: {owner_kinds[0]}, may not be recreated)"
                )
                continue

            # Check 3: Single-replica workloads (eviction = downtime)
            # Trace ReplicaSet back to Deployment
            controller_key = None
            if "ReplicaSet" in owner_kinds:
                rs_name = owner_names[owner_kinds.index("ReplicaSet")]
                # ReplicaSet name is typically <deployment-name>-<hash>
                dep_name = "-".join(rs_name.rsplit("-", 1)[:-1]) if "-" in rs_name else rs_name
                controller_key = f"{ns}/{dep_name}"
            elif "StatefulSet" in owner_kinds:
                sts_name = owner_names[owner_kinds.index("StatefulSet")]
                controller_key = f"{ns}/{sts_name}"

            if controller_key and controller_key in replica_lookup:
                info = replica_lookup[controller_key]
                if info["replicas"] == 1:
                    analysis["soft_risks"].append(
                        f"{pod_id} — single replica {info['kind']} (eviction = downtime)"
                    )
                    continue

            # Check 4: emptyDir volumes (data loss)
            has_emptydir = False
            if pod.spec.volumes:
                for vol in pod.spec.volumes:
                    if vol.empty_dir:
                        # Skip default service account token volumes
                        if vol.name and "token" in vol.name:
                            continue
                        has_emptydir = True
                        break

            if has_emptydir:
                analysis["soft_risks"].append(
                    f"{pod_id} — uses emptyDir (data will be lost on eviction)"
                )
                continue

            # Pod is safe to evict
            analysis["safe_pods"].append(pod_id)

        return analysis

    def _check_pdb_blocking(self, pod, pdbs):
        """Check if any PDB would block this pod's eviction.

        Returns a description string if blocked, None if not.
        """
        if not pdbs:
            return None

        pod_labels = pod.metadata.labels or {}

        for pdb in pdbs:
            selector = pdb.get("selector")
            if not selector or not selector.match_labels:
                continue

            # Check if pod matches the PDB selector
            match = all(
                pod_labels.get(k) == v
                for k, v in selector.match_labels.items()
            )

            if match and pdb["disruptions_allowed"] == 0:
                return (
                    f"PDB '{pdb['name']}' allows 0 disruptions, "
                    f"healthy: {pdb['current_healthy']}/{pdb['expected_pods']}"
                )

        return None
