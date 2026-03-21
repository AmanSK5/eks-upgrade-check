"""Interactive menu for guided usage."""

import sys


def run_interactive():
    """Launch the interactive menu when no arguments are provided."""
    from eks_upgrade_check.reporter import Colours

    print()
    print(f"{Colours.BOLD}╔══════════════════════════════════════════════════╗{Colours.RESET}")
    print(f"{Colours.BOLD}║       EKS Upgrade Readiness Checker v0.1.0       ║{Colours.RESET}")
    print(f"{Colours.BOLD}╚══════════════════════════════════════════════════╝{Colours.RESET}")
    print()
    print(f"  {Colours.GREY}No arguments provided — launching interactive mode.{Colours.RESET}")
    print(f"  {Colours.GREY}You can also run directly with flags (see --help).{Colours.RESET}")
    print()

    # Cluster name
    cluster = _prompt("  EKS cluster name: ")
    if not cluster:
        print(f"\n  {Colours.RED}Cluster name is required.{Colours.RESET}")
        sys.exit(1)

    # AWS profile (optional)
    profile = _prompt("  AWS profile (press Enter to use default): ")

    # AWS region (optional)
    region = _prompt("  AWS region (press Enter to use profile default): ")

    # Connect to AWS and fetch supported versions + current cluster version
    print()
    print(f"  {Colours.GREY}Connecting to AWS...{Colours.RESET}")
    supported_versions = _fetch_supported_versions(profile, region)
    current_version = _fetch_cluster_version(cluster, profile, region)

    if current_version:
        print(f"  {Colours.GREY}Current cluster version: {current_version}{Colours.RESET}")
    if supported_versions:
        print(f"  {Colours.GREY}Supported versions: {', '.join(supported_versions)}{Colours.RESET}")
    else:
        print(f"  {Colours.YELLOW}Could not fetch versions from AWS. Version will be validated later.{Colours.RESET}")

    print()

    # Target version (validated against AWS + current version)
    while True:
        target = _prompt("  Target K8s version (e.g. 1.31): ")
        if not target:
            print(f"  {Colours.RED}Target version is required.{Colours.RESET}")
            continue

        if supported_versions:
            if target not in supported_versions:
                print(f"  {Colours.RED}'{target}' is not a valid EKS version.{Colours.RESET}")
                print(f"  {Colours.GREY}Supported: {', '.join(supported_versions)}{Colours.RESET}")
                print()
                continue
        else:
            import re
            if not re.match(r"^1\.\d{1,2}$", target):
                print(f"  {Colours.RED}Invalid format. Use 1.XX (e.g. 1.31){Colours.RESET}")
                continue

        # Check for downgrade
        if current_version:
            current_minor = int(current_version.split(".")[1])
            target_minor = int(target.split(".")[1])
            if target_minor <= current_minor:
                print(f"  {Colours.RED}Target version must be higher than current cluster version ({current_version}).{Colours.RESET}")
                print()
                continue

        break

    print()
    print(f"  {Colours.BOLD}What would you like to do?{Colours.RESET}")
    print()
    print(f"  {Colours.CYAN}1{Colours.RESET}  Run all readiness checks")
    print(f"  {Colours.CYAN}2{Colours.RESET}  Run all checks + drain impact analysis")
    print(f"  {Colours.CYAN}3{Colours.RESET}  Drain impact analysis only")
    print(f"  {Colours.CYAN}4{Colours.RESET}  Choose specific checks to run")
    print(f"  {Colours.CYAN}5{Colours.RESET}  Run all checks and save report to file")
    print(f"  {Colours.GREY}q{Colours.RESET}  Quit")
    print()

    choice = _prompt("  Select [1-5/q]: ", default="1")

    if choice.lower() == "q":
        print(f"  {Colours.GREY}Bye.{Colours.RESET}")
        print()
        sys.exit(0)

    # Build the command args
    args = ["--cluster", cluster, "--target", target]
    if profile:
        args.extend(["--profile", profile])
    if region:
        args.extend(["--region", region])

    if choice == "1":
        pass  # Default — all checks

    elif choice == "2":
        args.append("--simulate-drain")

    elif choice == "3":
        args.extend(["--checks", "drain-simulation"])

    elif choice == "4":
        print()
        print(f"  {Colours.BOLD}Available checks:{Colours.RESET}")
        checks = [
            ("control-plane", "Upgrade path, extended support, cluster health"),
            ("eks-insights", "AWS EKS upgrade insights from audit logs"),
            ("deprecated-apis", "Scan workloads for removed API versions"),
            ("addons", "Self-managed vs EKS-managed add-ons"),
            ("irsa", "IAM Roles for Service Accounts validation"),
            ("nodes", "Node groups, AMI types, kubelet versions"),
            ("pdb", "PodDisruptionBudgets that block drains"),
            ("helm", "Helm chart staleness and deprecated charts"),
            ("drain-simulation", "Drain impact analysis (PDB blocks, single-replica downtime, standalone pods)"),
        ]
        for i, (name, desc) in enumerate(checks, 1):
            print(f"    {Colours.CYAN}{i}{Colours.RESET}  {name:20s} {Colours.GREY}{desc}{Colours.RESET}")
        print()
        selected = _prompt("  Enter numbers separated by commas (e.g. 1,3,5): ")

        try:
            indices = [int(x.strip()) for x in selected.split(",")]
            check_names = [checks[i - 1][0] for i in indices if 1 <= i <= len(checks)]
            if check_names:
                args.extend(["--checks", ",".join(check_names)])
            else:
                print(f"\n  {Colours.RED}No valid checks selected.{Colours.RESET}")
                sys.exit(1)
        except (ValueError, IndexError):
            print(f"\n  {Colours.RED}Invalid selection.{Colours.RESET}")
            sys.exit(1)

    elif choice == "5":
        print()
        fmt = _prompt("  Output format — json or markdown [markdown]: ", default="markdown")
        filename = _prompt(f"  Filename [upgrade-report.{'json' if fmt == 'json' else 'md'}]: ",
                          default=f"upgrade-report.{'json' if fmt == 'json' else 'md'}")
        args.extend(["--output", fmt, "--output-file", filename])
        args.append("--simulate-drain")

    else:
        print(f"\n  {Colours.RED}Invalid choice.{Colours.RESET}")
        sys.exit(1)

    print()
    return args


def _fetch_supported_versions(profile, region):
    """Connect to AWS and fetch the list of supported EKS versions."""
    try:
        import boto3

        session_kwargs = {}
        if profile:
            session_kwargs["profile_name"] = profile
        if region:
            session_kwargs["region_name"] = region

        session = boto3.Session(**session_kwargs)
        eks = session.client("eks", region_name=session.region_name or "us-east-1")

        versions = set()
        response = eks.describe_addon_versions(addonName="kube-proxy")
        for addon in response.get("addons", []):
            for av in addon.get("addonVersions", []):
                for compat in av.get("compatibilities", []):
                    cv = compat.get("clusterVersion", "")
                    if cv:
                        versions.add(cv)

        return sorted(versions, key=lambda v: int(v.split(".")[1]))
    except Exception:
        return []


def _fetch_cluster_version(cluster_name, profile, region):
    """Fetch the current Kubernetes version of the cluster."""
    try:
        import boto3

        session_kwargs = {}
        if profile:
            session_kwargs["profile_name"] = profile
        if region:
            session_kwargs["region_name"] = region

        session = boto3.Session(**session_kwargs)
        eks = session.client("eks", region_name=session.region_name or "us-east-1")

        response = eks.describe_cluster(name=cluster_name)
        return response["cluster"]["version"]
    except Exception:
        return None


def _prompt(text: str, default: str = "") -> str:
    """Prompt the user for input."""
    from eks_upgrade_check.reporter import Colours
    try:
        value = input(text).strip()
        return value if value else default
    except (EOFError, KeyboardInterrupt):
        print("\n\n  Cancelled.")
        sys.exit(0)
