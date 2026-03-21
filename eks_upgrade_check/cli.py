"""EKS Upgrade Readiness Checker - CLI entry point."""

import click
import sys
import json
from datetime import datetime

from eks_upgrade_check.reporter import Reporter, ReportFormat
from eks_upgrade_check.clients import AWSClient, K8sClient
from eks_upgrade_check.checks.control_plane import ControlPlaneCheck
from eks_upgrade_check.checks.eks_insights import EKSInsightsCheck
from eks_upgrade_check.checks.deprecated_apis import DeprecatedAPICheck
from eks_upgrade_check.checks.addons import AddonCheck
from eks_upgrade_check.checks.irsa import IRSACheck
from eks_upgrade_check.checks.nodes import NodeCheck
from eks_upgrade_check.checks.pdb import PDBCheck
from eks_upgrade_check.checks.helm import HelmCheck
from eks_upgrade_check.checks.drain_simulation import DrainSimulationCheck

ALL_CHECKS = {
    "control-plane": ControlPlaneCheck,
    "eks-insights": EKSInsightsCheck,
    "deprecated-apis": DeprecatedAPICheck,
    "addons": AddonCheck,
    "irsa": IRSACheck,
    "nodes": NodeCheck,
    "pdb": PDBCheck,
    "helm": HelmCheck,
    "drain-simulation": DrainSimulationCheck,
}

# Drain simulation is opt-in — not included in the default set
DEFAULT_CHECKS = set(ALL_CHECKS.keys()) - {"drain-simulation"}


@click.command()
@click.option(
    "--cluster", "-c",
    default=None,
    help="EKS cluster name.",
)
@click.option(
    "--target", "-t",
    default=None,
    help="Target Kubernetes version (e.g. 1.31).",
)
@click.option(
    "--region", "-r",
    default=None,
    help="AWS region. Defaults to AWS_DEFAULT_REGION or profile region.",
)
@click.option(
    "--profile", "-p",
    default=None,
    help="AWS CLI profile name.",
)
@click.option(
    "--output", "-o",
    type=click.Choice(["terminal", "json", "markdown"]),
    default="terminal",
    help="Output format (default: terminal).",
)
@click.option(
    "--checks",
    default=None,
    help="Comma-separated list of checks to run. Default: all (except drain-simulation).",
)
@click.option(
    "--skip",
    default=None,
    help="Comma-separated list of checks to skip.",
)
@click.option(
    "--output-file", "-f",
    default=None,
    help="Write report to file instead of stdout (for json/markdown).",
)
@click.option(
    "--simulate-drain",
    is_flag=True,
    default=False,
    help="Include node drain simulation (dry-run) in the checks.",
)
def main(cluster, target, region, profile, output, checks, skip, output_file, simulate_drain):
    """EKS Upgrade Readiness Checker.

    Scans your EKS cluster and identifies issues that could block or
    complicate an upgrade to the target Kubernetes version.

    Run with no arguments for interactive mode.

    \b
    Example:
        eks-upgrade-check
        eks-upgrade-check --cluster my-prod-eks --target 1.31
        eks-upgrade-check -c my-cluster -t 1.32 -p my-aws-profile -r eu-west-2
        eks-upgrade-check -c my-cluster -t 1.31 --simulate-drain
        eks-upgrade-check -c my-cluster -t 1.31 --checks addons,irsa,nodes
        eks-upgrade-check -c my-cluster -t 1.31 -o json -f report.json
    """
    # If no cluster/target provided, launch interactive mode
    if not cluster or not target:
        from eks_upgrade_check.interactive import run_interactive
        args = run_interactive()
        # Re-invoke with the collected args
        sys.argv = ["eks-upgrade-check"] + args
        main(standalone_mode=False)
        return

    # Determine which checks to run
    checks_to_run = _resolve_checks(checks, skip, simulate_drain)

    # Set up output format
    fmt = {
        "terminal": ReportFormat.TERMINAL,
        "json": ReportFormat.JSON,
        "markdown": ReportFormat.MARKDOWN,
    }[output]

    reporter = Reporter(fmt)

    reporter.print_banner(cluster, target)

    # Initialise AWS + K8s clients
    try:
        aws_client = AWSClient(
            cluster_name=cluster,
            region=region,
            profile=profile,
        )
        k8s_client = K8sClient(cluster_name=cluster, aws_client=aws_client)
    except Exception as e:
        reporter.print_fatal(f"Failed to initialise clients: {e}")
        sys.exit(1)

    # Validate target version against AWS-supported versions
    try:
        supported = aws_client.get_supported_versions()
        if supported and target not in supported:
            reporter.print_fatal(
                f"'{target}' is not a valid EKS version.\n"
                f"  Supported versions: {', '.join(supported)}"
            )
            sys.exit(1)
    except Exception:
        pass  # If we can't fetch versions, let it proceed and fail naturally

    # Fetch cluster info
    try:
        cluster_info = aws_client.describe_cluster()
        current_version = cluster_info["cluster"]["version"]
        reporter.print_info(f"Current version: {current_version}")
        reporter.print_info(f"Target version:  {target}")
        reporter.print_info(f"Region:          {aws_client.region}")
        reporter.print_separator()
    except Exception as e:
        reporter.print_fatal(f"Failed to describe cluster: {e}")
        sys.exit(1)

    # Validate target > current (catch downgrades early)
    current_minor = int(current_version.split(".")[1])
    target_minor = int(target.split(".")[1])

    if target_minor <= current_minor:
        reporter.print_fatal(
            f"Target version {target} must be higher than current cluster version ({current_version})."
        )
        sys.exit(1)

    # Context for all checks
    context = {
        "cluster_name": cluster,
        "current_version": current_version,
        "target_version": target,
        "cluster_info": cluster_info,
        "region": aws_client.region,
    }

    # Run checks
    all_results = []
    for name, check_cls in ALL_CHECKS.items():
        if name not in checks_to_run:
            continue

        check = check_cls(aws_client=aws_client, k8s_client=k8s_client)
        reporter.print_check_header(check.title)

        try:
            results = check.run(context)
            all_results.extend(results)
            for result in results:
                reporter.print_result(result)
        except Exception as e:
            reporter.print_error(f"Check '{name}' failed: {e}")

        reporter.print_separator()

    # Upgrade Plan (dynamic — built from findings)
    if fmt == ReportFormat.TERMINAL:
        _print_upgrade_plan(current_version, target, current_minor, target_minor, all_results)

    # Summary + recommended actions
    reporter.print_summary(all_results)
    reporter.print_recommendations(all_results)

    # Write to file if requested
    if output_file:
        report_data = reporter.generate_report(all_results, context)
        with open(output_file, "w") as f:
            f.write(report_data)
        click.echo(f"\nReport written to {output_file}")

    # Exit code: 1 if any ERRORs, 0 otherwise
    has_errors = any(r.severity == "ERROR" for r in all_results)
    sys.exit(1 if has_errors else 0)


def _resolve_checks(include, exclude, simulate_drain):
    """Resolve which checks to run based on --checks, --skip, and --simulate-drain flags."""
    if include:
        selected = {c.strip() for c in include.split(",")}
        invalid = selected - set(ALL_CHECKS.keys())
        if invalid:
            click.echo(f"Unknown checks: {', '.join(invalid)}", err=True)
            click.echo(f"Available: {', '.join(ALL_CHECKS.keys())}", err=True)
            sys.exit(1)
        return selected

    selected = set(DEFAULT_CHECKS)

    # Add drain simulation if requested
    if simulate_drain:
        selected.add("drain-simulation")

    if exclude:
        to_skip = {c.strip() for c in exclude.split(",")}
        selected -= to_skip

    return selected


def _print_upgrade_plan(current, target, current_minor, target_minor, results):
    """Print a dynamic upgrade plan built from check findings."""
    from eks_upgrade_check.reporter import Colours

    hops = target_minor - current_minor
    path_versions = [f"1.{v}" for v in range(current_minor, target_minor + 1)]
    path_str = " → ".join(path_versions)
    multi_hop = " (multi-hop)" if hops > 1 else ""

    # Collect blocking issues (ERRORs that prevent upgrade)
    blocking = []
    recommended = []

    # Drain blockers
    drain_blocked = [
        r for r in results
        if r.check == "Drain Impact Analysis" and r.severity == "ERROR" and "BLOCKED" in r.message
    ]
    if drain_blocked:
        blocking.append("Resolve drain blockers (PDB / pod health issues)")

    # Exhausted PDB budgets
    pdb_exhausted = [
        r for r in results
        if r.check == "PodDisruptionBudgets" and r.severity == "ERROR" and "exhausted" in r.message
    ]
    if pdb_exhausted and not drain_blocked:
        blocking.append("Restore unhealthy pods to free PDB disruption budgets")

    # Blocking PDB configs
    pdb_blocking = [
        r for r in results
        if r.check == "PodDisruptionBudgets" and r.severity == "ERROR" and "blocks all" in r.message
    ]
    if pdb_blocking:
        blocking.append("Fix PDBs that block all disruptions (maxUnavailable: 0)")

    # Deprecated APIs removed in target version
    deprecated_errors = [
        r for r in results
        if r.severity == "ERROR" and "Deprecated API" in r.message
    ]
    if deprecated_errors:
        blocking.append("Update deprecated API manifests before upgrading")

    # AL2 nodes
    al2_nodes = [
        r for r in results
        if r.check == "Node Groups & AMIs" and r.severity == "ERROR" and "Amazon Linux 2" in r.message
    ]
    if al2_nodes:
        blocking.append("Migrate AL2 node groups to AL2023")

    # Self-managed add-ons (WARNING but should fix before)
    self_managed = [
        r for r in results
        if r.check == "EKS Add-ons" and "SELF-MANAGED" in r.message
    ]
    if self_managed:
        recommended.append("Convert self-managed add-ons to EKS-managed")

    # Outdated Helm charts
    outdated_helm = [
        r for r in results
        if r.check == "Helm Releases" and r.severity == "WARNING" and "chart version" in r.message
    ]
    if outdated_helm:
        chart_names = []
        for r in outdated_helm:
            # Extract chart name from message like "'vault' chart version..."
            name = r.message.split("'")[1] if "'" in r.message else "unknown"
            chart_names.append(name)
        recommended.append(f"Upgrade outdated Helm charts ({', '.join(chart_names)})")

    # Missing IRSA
    missing_irsa = [
        r for r in results
        if r.check == "IRSA (IAM Roles for Service Accounts)" and "missing IRSA" in r.message
    ]
    if missing_irsa:
        recommended.append("Configure IRSA for service accounts using node role fallback")

    # Deprecated charts
    deprecated_charts = [
        r for r in results
        if r.check == "Helm Releases" and "deprecated" in r.message.lower()
    ]
    if deprecated_charts:
        recommended.append("Remove or replace deprecated Helm charts")

    # Print the plan
    print(f"  {Colours.BOLD}Upgrade Plan{Colours.RESET}")
    print(f"  {'─' * 48}")
    print(f"  {path_str}{multi_hop}")

    has_prereqs = blocking or recommended

    if has_prereqs:
        print()
        print(f"  {Colours.BOLD}Phase 1 — Pre-upgrade remediation{Colours.RESET}")
        print(f"  {'─' * 48}")

        step = 1
        if blocking:
            print()
            print(f"  {Colours.RED}{Colours.BOLD}  Must fix (blocking):{Colours.RESET}")
            for item in blocking:
                print(f"    {Colours.RED}{step}. {item}{Colours.RESET}")
                step += 1

        if recommended:
            print()
            print(f"  {Colours.YELLOW}{Colours.BOLD}  Should fix (recommended):{Colours.RESET}")
            for item in recommended:
                print(f"    {Colours.YELLOW}{step}. {item}{Colours.RESET}")
                step += 1

    print()
    phase_label = "Phase 2" if has_prereqs else "Steps"
    print(f"  {Colours.BOLD}{phase_label} — Upgrade execution{Colours.RESET}")
    print(f"  {'─' * 48}")

    if hops == 1:
        print(f"    1. Upgrade control plane ({current} → {target})")
        print(f"    2. Upgrade EKS add-ons")
        print(f"    3. Upgrade node groups")
        print(f"    4. Validate workloads")
    else:
        step = 1
        for v in range(current_minor + 1, target_minor + 1):
            from_v = f"1.{v - 1}"
            to_v = f"1.{v}"
            print()
            print(f"    {Colours.GREY}{'─' * 20} {from_v} → {to_v} {'─' * 20}{Colours.RESET}")
            print(f"    {step}. Upgrade control plane ({from_v} → {to_v})")
            print(f"    {step + 1}. Upgrade EKS add-ons")
            print(f"    {step + 2}. Upgrade node groups")
            print(f"    {step + 3}. Validate workloads")
            step += 4

    print()

    print()


def cli():
    """Entry point wrapper."""
    main(standalone_mode=False)


if __name__ == "__main__":
    cli()
