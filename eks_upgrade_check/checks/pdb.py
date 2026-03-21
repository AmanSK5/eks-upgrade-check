"""PodDisruptionBudget blocker detection."""

from eks_upgrade_check.checks import BaseCheck
from eks_upgrade_check.reporter import CheckResult


class PDBCheck(BaseCheck):
    """Check for PDBs that could block node drains during upgrade."""

    title = "PodDisruptionBudgets"

    def run(self, context: dict) -> list[CheckResult]:
        results = []

        try:
            pdbs = self.k8s.get_pdbs()
        except Exception as e:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Could not list PodDisruptionBudgets: {e}",
            ))
            return results

        if not pdbs:
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message="No PodDisruptionBudgets found",
            ))
            return results

        blocking_pdbs = []
        exhausted_pdbs = []
        tight_pdbs = []
        healthy_pdbs = []

        for pdb in pdbs:
            name = pdb.metadata.name
            namespace = pdb.metadata.namespace
            spec = pdb.spec
            status = pdb.status

            min_available = spec.min_available if spec.min_available is not None else None
            max_unavailable = spec.max_unavailable if spec.max_unavailable is not None else None

            current_healthy = status.current_healthy if status else 0
            desired_healthy = status.desired_healthy if status else 0
            expected_pods = status.expected_pods if status else 0
            disruptions_allowed = status.disruptions_allowed if status else 0

            pdb_info = {
                "name": name,
                "namespace": namespace,
                "min_available": min_available,
                "max_unavailable": max_unavailable,
                "current_healthy": current_healthy,
                "desired_healthy": desired_healthy,
                "expected_pods": expected_pods,
                "disruptions_allowed": disruptions_allowed,
            }

            # Check for PDBs configured to block all disruptions
            if max_unavailable is not None and _is_zero(max_unavailable):
                blocking_pdbs.append(pdb_info)
            elif min_available is not None and _equals_or_exceeds(min_available, expected_pods):
                # minAvailable >= expected pods means 0 disruptions allowed by config
                blocking_pdbs.append(pdb_info)
            elif disruptions_allowed == 0 and expected_pods > 0:
                # Config looks fine but budget is currently exhausted
                # (likely due to unhealthy/missing pods)
                exhausted_pdbs.append(pdb_info)
            elif disruptions_allowed == 1 and expected_pods <= 2:
                tight_pdbs.append(pdb_info)
            else:
                healthy_pdbs.append(pdb_info)

        # Report blocking PDBs
        for pdb in blocking_pdbs:
            detail = (
                f"maxUnavailable: {pdb['max_unavailable']}, "
                f"minAvailable: {pdb['min_available']}, "
                f"expected pods: {pdb['expected_pods']}, "
                f"disruptions allowed: {pdb['disruptions_allowed']}"
            )
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"PDB '{pdb['namespace']}/{pdb['name']}' blocks all disruptions",
                detail=detail,
                resource=f"{pdb['namespace']}/{pdb['name']}",
                recommendation=(
                    f"Update PDB '{pdb['name']}' in {pdb['namespace']} to allow at least "
                    f"1 disruption (set maxUnavailable: 1 or adjust minAvailable)."
                ),
                fix=f"kubectl patch pdb {pdb['name']} -n {pdb['namespace']} --type merge -p '{{\"spec\":{{\"maxUnavailable\":1}}}}'",
            ))

        # Report PDBs with exhausted disruption budget (config is fine but pods are unhealthy)
        for pdb in exhausted_pdbs:
            healthy_deficit = pdb['expected_pods'] - pdb['current_healthy']
            detail = (
                f"maxUnavailable: {pdb['max_unavailable']}, "
                f"minAvailable: {pdb['min_available']}, "
                f"healthy: {pdb['current_healthy']}/{pdb['expected_pods']} pods, "
                f"disruptions allowed: {pdb['disruptions_allowed']}"
            )
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"PDB '{pdb['namespace']}/{pdb['name']}' disruption budget exhausted — {healthy_deficit} pod(s) unhealthy",
                detail=detail + "\nThe PDB config itself is fine, but unhealthy pods have "
                       "consumed the disruption budget. Node drains will be blocked until "
                       "pod health is restored.",
                resource=f"{pdb['namespace']}/{pdb['name']}",
                recommendation=(
                    f"Investigate unhealthy pods in '{pdb['name']}' ({pdb['namespace']}). "
                    f"Restore pod health before upgrading so the disruption budget is available."
                ),
                fix=f"kubectl get pods -n {pdb['namespace']} -l app={pdb['name']} | grep -v Running",
            ))

        # Report tight PDBs
        for pdb in tight_pdbs:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"PDB '{pdb['namespace']}/{pdb['name']}' allows only {pdb['disruptions_allowed']} disruption(s)",
                detail=f"With {pdb['expected_pods']} pod(s), drains may stall if a pod is unhealthy.",
                resource=f"{pdb['namespace']}/{pdb['name']}",
                recommendation=(
                    f"Consider scaling up the workload behind '{pdb['name']}' "
                    f"before upgrading to allow smoother node drains."
                ),
            ))

        # Summary of healthy PDBs
        if healthy_pdbs:
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message=f"{len(healthy_pdbs)} PDB(s) configured with safe disruption budgets",
            ))

        return results


def _is_zero(value) -> bool:
    """Check if a PDB value is zero (can be int or string like '0' or '0%')."""
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        return value.strip().rstrip("%") == "0"
    return False


def _equals_or_exceeds(min_available, expected_pods) -> bool:
    """Check if minAvailable >= expected pods (blocking all disruptions)."""
    try:
        if isinstance(min_available, int) and isinstance(expected_pods, int):
            return min_available >= expected_pods
        if isinstance(min_available, str) and min_available.endswith("%"):
            pct = int(min_available.rstrip("%"))
            return pct >= 100
    except (ValueError, TypeError):
        pass
    return False
