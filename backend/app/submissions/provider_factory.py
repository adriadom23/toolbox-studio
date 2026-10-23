from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.projects.runtime import IsolatedGatewayRuntimeProvider, LocalProjectRuntimeProvider
from app.submissions.build import LocalSubprocessBuildProvider
from app.submissions.git_metadata import LocalGitMetadataProvider
from app.submissions.local_providers import LocalLogStore, LocalNotificationProvider, LocalPublicationProvider
from app.submissions.stores import FilesystemArtifactStore


@dataclass(frozen=True)
class ProviderSet:
    artifacts: object
    build: object
    logs: object
    notifications: dict[str, object]
    publication: object
    git: object
    runtime: object
    identities: dict[str, object]
    log_exporter: object | None = None
    review_base_url: str = "http://127.0.0.1:5173"


def _validate_provider_selection(config) -> None:
    selected = {
        "artifact": config.artifact_provider,
        "build": config.build_provider,
        "logs": config.log_provider,
        "notifications": tuple(config.notification_providers),
        "publication": config.publication_provider,
        "runtime": config.runtime_provider,
    }
    if config.is_local:
        expected = {
            "artifact": "local",
            "build": "local-subprocess",
            "logs": "local",
            "notifications": ("local",),
            "publication": "local",
            "runtime": "local-subprocess",
        }
        if selected != expected:
            raise ValueError("config_invalid: local profile only supports local providers")
        return
    if (
        selected["artifact"] != "s3"
        or selected["build"] != "codebuild"
        or selected["logs"] != "cloudwatch"
        or selected["publication"] != "local"
        or selected["runtime"] != "isolated-gateway"
        or not selected["notifications"]
        or any(channel not in {"ses", "teams"} for channel in selected["notifications"])
        or len(set(selected["notifications"])) != len(selected["notifications"])
    ):
        raise ValueError(
            "config_invalid: shared profile requires s3, codebuild, cloudwatch, "
            "one or more unique ses/teams notification channels, local publication, and isolated-gateway"
        )


def create_local_providers(
    runtime_root: Path,
    frontend_root: Path | None = None,
    backend_root: Path | None = None,
    *,
    review_base_url: str = "http://127.0.0.1:5173",
    max_log_bytes: int = 5 * 1024 * 1024,
    max_builds_global: int = 2,
    max_builds_per_project: int = 1,
) -> ProviderSet:
    root = Path(runtime_root)
    checkout = Path(__file__).resolve().parents[3]
    frontend = Path(frontend_root or checkout / "frontend")
    backend = Path(backend_root or checkout / "backend")
    return ProviderSet(
        artifacts=FilesystemArtifactStore(root / "objects"),
        build=LocalSubprocessBuildProvider(
            frontend,
            max_log_bytes=max_log_bytes,
            max_builds_global=max_builds_global,
            max_builds_per_project=max_builds_per_project,
        ),
        logs=LocalLogStore(root / "logs", max_phase_bytes=max_log_bytes),
        notifications={"local": LocalNotificationProvider(root / "notifications" / "audit.jsonl")},
        publication=LocalPublicationProvider(root / "publications"),
        git=LocalGitMetadataProvider(),
        runtime=LocalProjectRuntimeProvider(backend),
        identities={
            "artifact": "local",
            "build": "local-subprocess",
            "logs": "local",
            "notifications": ["local"],
            "publication": "local",
            "runtime": "local-subprocess",
        },
        review_base_url=review_base_url,
    )


def create_providers(config, runtime_root: Path, *, frontend_root: Path | None = None, backend_root: Path | None = None, clients: dict[str, object] | None = None) -> ProviderSet:
    config.validate_runtime_safety()
    _validate_provider_selection(config)
    if config.is_local:
        return create_local_providers(
            runtime_root,
            frontend_root,
            backend_root,
            review_base_url=config.review_base_url,
            max_log_bytes=config.log_phase_max_bytes,
            max_builds_global=config.max_builds_global,
            max_builds_per_project=config.max_builds_per_project,
        )
    clients = clients or {}
    from app.submissions.aws_providers import (
        AwsVpcEndpointPreflight,
        CloudWatchLogStore,
        CodeBuildProvider,
        S3ArtifactStore,
        SesNotificationProvider,
        TeamsNotificationProvider,
    )
    notifications: dict[str, object] = {}
    if "ses" in config.notification_providers:
        notifications["ses"] = SesNotificationProvider(config.ses_source, config.ses_recipients, clients.get("ses"))
    if "teams" in config.notification_providers:
        notifications["teams"] = TeamsNotificationProvider(config.teams_webhook_secret_env, clients.get("teams"), config.teams_allowed_hosts)
    network_preflight = clients.get("codebuild_network_preflight")
    if network_preflight is None and config.codebuild_network_preflight == "aws-vpc-endpoints-v1":
        ec2_client = clients.get("ec2")
        if ec2_client is None:
            import boto3
            ec2_client = boto3.client("ec2")
        network_preflight = AwsVpcEndpointPreflight(ec2_client)
    runtime = clients.get("runtime")
    if runtime is None:
        if not config.runtime_gateway_url or not config.runtime_gateway_token_env or not config.runtime_gateway_allowed_hosts:
            raise ValueError("config_invalid: shared profile requires a configured allowlisted isolated runtime gateway")
        runtime = IsolatedGatewayRuntimeProvider(
            config.runtime_gateway_url,
            config.runtime_gateway_token_env,
            config.runtime_gateway_allowed_hosts,
        )
    if not getattr(runtime, "isolated", False):
        raise ValueError("config_invalid: shared runtime provider must attest isolation")
    if network_preflight is None:
        raise ValueError("config_invalid: shared CodeBuild requires an allowlisted network preflight")
    return ProviderSet(
        artifacts=S3ArtifactStore(config.s3_bucket or "", config.s3_prefix, clients.get("s3")),
        build=CodeBuildProvider(
            config.codebuild_project or "",
            config.s3_bucket or "",
            config.s3_prefix,
            clients.get("codebuild"),
            clients.get("s3"),
            network_preflight,
            approved_image_digest=config.codebuild_image_digest,
            approved_wrapper_sha256=config.codebuild_wrapper_sha256,
            probe_version=config.codebuild_probe_version,
            account_id=config.codebuild_account_id,
            region=config.codebuild_region,
            attestation_verifier=clients.get("codebuild_attestation_verifier"),
        ),
        logs=LocalLogStore(Path(runtime_root) / "logs", max_phase_bytes=config.log_phase_max_bytes),
        log_exporter=CloudWatchLogStore(config.cloudwatch_log_group or "", clients.get("logs")),
        notifications=notifications,
        publication=LocalPublicationProvider(Path(runtime_root) / "publications"),
        git=LocalGitMetadataProvider(),
        runtime=runtime,
        identities={
            "artifact": "s3",
            "build": "codebuild",
            "logs": "cloudwatch",
            "notifications": list(notifications),
            "publication": "local",
            "runtime": "isolated-gateway",
        },
        review_base_url=config.review_base_url,
    )


def create_artifact_provider(kind: str, *, runtime_root: Path, **options):
    if kind == "local":
        return FilesystemArtifactStore(Path(runtime_root) / "objects")
    if kind == "s3":
        from app.submissions.aws_providers import S3ArtifactStore
        return S3ArtifactStore(options.get("bucket", ""), options.get("prefix", "toolbox/artifacts"), options.get("client"))
    raise ValueError(f"unknown artifact provider: {kind}")
