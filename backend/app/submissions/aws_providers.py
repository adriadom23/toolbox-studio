from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from app.submissions.build import BuildFailed
from app.submissions.providers import (
    AppendResult, BuildHandle, BuildOutputs, BuildPoll, BuildRequest, DownloadSpec, LogPage,
    NotificationEnvelope, ObjectRef, ObjectStat, PreflightResult, ProviderResult,
)
from app.submissions.snapshot import write_verified_snapshot_zip
from app.submissions.stores import sha256_file


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _not_found(exc: Exception) -> bool:
    return _error_code(exc) in {"404", "NoSuchKey", "NotFound"}


def _precondition_failed(exc: Exception) -> bool:
    return _error_code(exc) in {"409", "412", "ConditionalRequestConflict", "PreconditionFailed"}


def _checksum_sha256(response: dict) -> str | None:
    encoded = response.get("ChecksumSHA256")
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded).hex()
    except (ValueError, TypeError):
        return None


def _encode_s3_ref(key: str, version_id: str, sha256: str) -> str:
    payload = json.dumps({"key": key, "version_id": version_id, "sha256": sha256}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "s3v1:" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_s3_ref(value: str) -> tuple[str, str | None, str | None]:
    if not value.startswith("s3v1:"):
        return value, None, None
    encoded = value[5:]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    if set(payload) != {"key", "version_id", "sha256"}:
        raise ValueError("invalid immutable S3 reference")
    return str(payload["key"]), str(payload["version_id"]), str(payload["sha256"])


class S3ArtifactStore:
    provider = "s3"

    def __init__(self, bucket: str, prefix: str = "toolbox/artifacts", client=None) -> None:
        if not bucket:
            raise ValueError("S3 bucket is required")
        if client is None:
            import boto3
            client = boto3.client("s3")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, object_id: str) -> str:
        digest = hashlib.sha256(("artifacts\0" + object_id).encode()).hexdigest()
        return f"{self.prefix}/{digest[:2]}/{digest}.zip"

    def _head(self, key: str, version_id: str | None = None) -> dict:
        options = {"Bucket": self.bucket, "Key": key, "ChecksumMode": "ENABLED"}
        if version_id:
            options["VersionId"] = version_id
        return self.client.head_object(**options)

    @staticmethod
    def _verified_version(response: dict, *, size: int, sha256: str) -> str:
        version_id = response.get("VersionId")
        checksum = _checksum_sha256(response)
        if not version_id or int(response["ContentLength"]) != size or checksum != sha256:
            raise FileExistsError("S3 object version or checksum mismatch")
        return str(version_id)

    def put_if_absent(self, object_id: str, source: Path, size: int, sha256: str, metadata=None) -> ObjectRef:
        actual = sha256_file(Path(source))
        if actual != (size, sha256):
            raise ValueError("artifact source integrity mismatch")
        key = self._key(object_id)
        response = None
        try:
            existing = self._head(key)
        except Exception as exc:
            if not _not_found(exc):
                raise
            try:
                with Path(source).open("rb") as handle:
                    response = self.client.put_object(
                        Bucket=self.bucket, Key=key, Body=handle, ContentLength=size,
                        ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
                        Metadata={**(metadata or {}), "sha256": sha256}, IfNoneMatch="*",
                    )
            except Exception as put_error:
                if not _precondition_failed(put_error):
                    raise
                existing = self._head(key)
        if response is not None:
            version_id = response.get("VersionId")
            if not version_id:
                raise ValueError("S3 versioned write did not return VersionId")
            existing = self._head(key, str(version_id))
        version_id = self._verified_version(existing, size=size, sha256=sha256)
        ref = ObjectRef(self.provider, _encode_s3_ref(key, version_id, sha256))
        stat_result = self.stat(ref)
        if (stat_result.size, stat_result.sha256) != (size, sha256):
            raise FileExistsError("artifact object collision")
        return ref

    def resolve_existing(self, object_id: str, size: int, sha256: str) -> ObjectRef:
        key = self._key(object_id)
        response = self._head(key)
        version_id = self._verified_version(response, size=size, sha256=sha256)
        ref = ObjectRef(self.provider, _encode_s3_ref(key, version_id, sha256))
        current = self.stat(ref)
        if (current.size, current.sha256) != (size, sha256):
            raise FileExistsError("artifact object collision")
        return ref

    def stat(self, ref: ObjectRef) -> ObjectStat:
        if ref.provider != self.provider:
            raise ValueError("wrong artifact provider")
        key_or_id, version_id, expected_sha = _decode_s3_ref(ref.key)
        key = key_or_id if version_id else self._key(key_or_id)
        response = self._head(key, version_id)
        digest = _checksum_sha256(response)
        if not digest or (expected_sha and digest != expected_sha):
            raise ValueError("S3 object lacks the expected verified checksum")
        return ObjectStat(int(response["ContentLength"]), digest)

    @contextmanager
    def open_stream(self, ref: ObjectRef) -> Iterator[object]:
        key_or_id, version_id, expected_sha = _decode_s3_ref(ref.key)
        key = key_or_id if version_id else self._key(key_or_id)
        options = {"Bucket": self.bucket, "Key": key, "ChecksumMode": "ENABLED"}
        if version_id:
            options["VersionId"] = version_id
        response = self.client.get_object(**options)
        if expected_sha and _checksum_sha256(response) != expected_sha:
            response["Body"].close()
            raise ValueError("S3 download checksum mismatch")
        body = response["Body"]
        try:
            yield body
        finally:
            body.close()

    def create_download(self, ref: ObjectRef, filename: str, ttl_seconds: int = 900) -> DownloadSpec:
        ttl = min(max(ttl_seconds, 1), 900)
        key_or_id, version_id, _expected_sha = _decode_s3_ref(ref.key)
        params = {"Bucket": self.bucket, "Key": key_or_id if version_id else self._key(key_or_id), "ResponseContentDisposition": f'attachment; filename="{filename}"'}
        if version_id:
            params["VersionId"] = version_id
        url = self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)
        return DownloadSpec("redirect", url, ttl)


_CODEBUILD_ATTESTATION_CONTRACT = "toolbox-codebuild-attestation-v1"
_CODEBUILD_REQUIRED_PHASES = ("input", "frontend", "backend", "package", "credentials_canary", "output_canary")
_CODEBUILD_BUILDSPEC = """version: 0.2
phases:
  build:
    commands:
      - /opt/toolbox/bin/build-submission --contract \"$TOOLBOX_ATTESTATION_CONTRACT\" --wrapper-sha256 \"$TOOLBOX_WRAPPER_SHA256\" --input-sha256 \"$TOOLBOX_INPUT_SHA256\" --project \"$TOOLBOX_PROJECT_SLUG\"
"""


class AwsVpcEndpointPreflight:
    """Fail-closed verifier for the effective CodeBuild VPC endpoint boundary."""

    REQUIRED_SERVICE_SUFFIXES = (".s3", ".logs", ".ecr.api", ".ecr.dkr")
    EGRESS_TARGET_KEYS = (
        "NatGatewayId", "TransitGatewayId", "VpcPeeringConnectionId", "EgressOnlyInternetGatewayId",
        "InstanceId", "NetworkInterfaceId", "CarrierGatewayId", "LocalGatewayId", "CoreNetworkArn",
    )

    def __init__(self, ec2_client) -> None:
        self.ec2 = ec2_client

    @staticmethod
    def _policy_restricted(endpoint: dict, bucket: str, project: dict) -> bool:
        policy = endpoint.get("PolicyDocument")
        if isinstance(policy, str):
            try:
                policy = json.loads(policy)
            except ValueError:
                return False
        if not isinstance(policy, dict):
            return False
        statements = policy.get("Statement")
        if isinstance(statements, dict):
            statements = [statements]
        if not isinstance(statements, list) or not statements:
            return False

        service_name = str(endpoint.get("ServiceName", ""))
        region = service_name.split(".")[2] if service_name.startswith("com.amazonaws.") and len(service_name.split(".")) >= 4 else ""
        role = str(project.get("serviceRole", ""))
        role_parts = role.split(":", 5)
        if len(role_parts) != 6 or role_parts[2] != "iam" or not role_parts[4]:
            return False
        account = role_parts[4]
        project_name = str(project.get("name", ""))
        image = str(project.get("environment", {}).get("image", ""))
        image_host, _, image_path = image.partition("/")
        image_account = image_host.split(".", 1)[0]
        repository = image_path.split("@", 1)[0]

        if service_name.endswith(".s3"):
            allowed_actions = {
                "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:ListBucket",
                "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration",
            }
            allowed_resources = {f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"}
        elif service_name.endswith(".logs"):
            allowed_actions = {"logs:CreateLogStream", "logs:PutLogEvents", "logs:GetLogEvents"}
            log_group = f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/{project_name}"
            allowed_resources = {log_group, log_group + ":*"}
        elif service_name.endswith((".ecr.api", ".ecr.dkr")):
            if image_account != account or not repository:
                return False
            allowed_actions = {"ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"}
            allowed_resources = {f"arn:aws:ecr:{region}:{account}:repository/{repository}"}
        else:
            return False

        found_allow = False
        for statement in statements:
            if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
                continue
            found_allow = True
            principal = statement.get("Principal")
            principal_values = principal.get("AWS") if isinstance(principal, dict) else principal
            principal_values = [principal_values] if isinstance(principal_values, str) else principal_values
            if not isinstance(principal_values, list) or principal_values != [role]:
                return False
            actions = statement.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            resources = statement.get("Resource", [])
            resources = [resources] if isinstance(resources, str) else resources
            if (
                not actions or not resources
                or any(not isinstance(action, str) or "*" in action or action not in allowed_actions for action in actions)
                or any(not isinstance(resource, str) or resource not in allowed_resources for resource in resources)
            ):
                return False
        return found_allow

    def __call__(self, project: dict, bucket: str) -> PreflightResult:
        vpc = project.get("vpcConfig", {})
        vpc_id = vpc.get("vpcId")
        subnets = set(vpc.get("subnets", []))
        if not vpc_id or not subnets:
            return PreflightResult(False, "codebuild VPC or subnets are missing")
        route_tables = self.ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("RouteTables", [])
        main_tables = {str(table.get("RouteTableId")) for table in route_tables if any(association.get("Main") for association in table.get("Associations", []))}
        effective_tables: set[str] = set()
        for subnet in subnets:
            explicit = {
                str(table.get("RouteTableId")) for table in route_tables
                if any(association.get("SubnetId") == subnet for association in table.get("Associations", []))
            }
            effective_tables.update(explicit or main_tables)
        if not effective_tables:
            return PreflightResult(False, "codebuild subnet route-table associations are incomplete")
        for table in route_tables:
            if str(table.get("RouteTableId")) not in effective_tables:
                continue
            for route in table.get("Routes", []):
                if route.get("State", "active") != "active":
                    continue
                gateway = str(route.get("GatewayId", ""))
                if gateway and gateway != "local" and not gateway.startswith("vpce-"):
                    return PreflightResult(False, "codebuild VPC has a non-endpoint gateway route")
                if any(route.get(key) for key in self.EGRESS_TARGET_KEYS):
                    return PreflightResult(False, "codebuild VPC has an external egress route")
        endpoints = self.ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "vpc-endpoint-state", "Values": ["available"]}]).get("VpcEndpoints", [])
        selected: dict[str, dict] = {}
        for suffix in self.REQUIRED_SERVICE_SUFFIXES:
            matches = [item for item in endpoints if str(item.get("ServiceName", "")).endswith(suffix)]
            if len(matches) != 1:
                return PreflightResult(False, "missing or ambiguous required VPC endpoint: " + suffix)
            selected[suffix] = matches[0]
        for suffix, endpoint in selected.items():
            if not self._policy_restricted(endpoint, bucket, project):
                return PreflightResult(False, "VPC endpoint policy is not least-privilege: " + suffix)
            if suffix == ".s3":
                if endpoint.get("VpcEndpointType") != "Gateway" or not effective_tables.issubset(set(endpoint.get("RouteTableIds", []))):
                    return PreflightResult(False, "S3 endpoint is not associated with every effective route table")
            elif (
                endpoint.get("VpcEndpointType") != "Interface"
                or endpoint.get("PrivateDnsEnabled") is not True
                or not subnets.issubset(set(endpoint.get("SubnetIds", [])))
                or not endpoint.get("Groups")
            ):
                return PreflightResult(False, "interface endpoint associations or private DNS are incomplete: " + suffix)
        return PreflightResult(True, "")


class CodeBuildProvider:
    isolated = True
    provider = "codebuild"

    def __init__(
        self,
        project_name: str,
        input_bucket: str,
        prefix: str = "toolbox/artifacts",
        client=None,
        s3_client=None,
        network_preflight=None,
        *,
        approved_image_digest: str | None = None,
        approved_wrapper_sha256: str | None = None,
        probe_version: str | None = None,
        account_id: str | None = None,
        region: str | None = None,
        attestation_verifier=None,
    ) -> None:
        if client is None:
            import boto3
            client = boto3.client("codebuild")
        if s3_client is None:
            import boto3
            s3_client = boto3.client("s3")
        self.client = client
        self.s3 = s3_client
        self.project_name = project_name
        self.input_bucket = input_bucket
        self.input_prefix = prefix.strip("/") + "/build-inputs"
        self.network_preflight = network_preflight
        self.approved_image_digest = approved_image_digest
        self.approved_wrapper_sha256 = approved_wrapper_sha256
        self.probe_version = probe_version
        self.account_id = account_id
        self.region = region
        self.attestation_verifier = attestation_verifier
        self.harness_sha256 = hashlib.sha256(_CODEBUILD_BUILDSPEC.encode("utf-8")).hexdigest()
        self._expected: dict[str, dict[str, str]] = {}
        self._accepted_attestations: dict[str, str] = {}
        self._attestation_lock = threading.Lock()

    def _scope(self) -> dict[str, str]:
        return {
            "contract": _CODEBUILD_ATTESTATION_CONTRACT,
            "account_id": str(self.account_id or ""),
            "region": str(self.region or ""),
            "project_name": self.project_name,
            "image_digest": str(self.approved_image_digest or ""),
            "wrapper_sha256": str(self.approved_wrapper_sha256 or ""),
            "probe_version": str(self.probe_version or ""),
        }

    def _probe_available(self, project: dict) -> PreflightResult:
        verifier = self.attestation_verifier
        if verifier is None or not callable(getattr(verifier, "preflight", None)):
            return PreflightResult(False, "codebuild attestation verifier is unavailable")
        try:
            result = verifier.preflight(project, self._scope())
        except Exception:
            return PreflightResult(False, "codebuild attestation probe failed")
        expected = {**self._scope(), "status": "passed", "credentials_canary": "passed", "output_canary": "passed"}
        if not isinstance(result, dict) or set(result) != set(expected) or any(result.get(key) != value for key, value in expected.items()):
            return PreflightResult(False, "codebuild attestation probe mismatch")
        return PreflightResult(True, "")

    def preflight(self) -> PreflightResult:
        if not self.project_name or not self.input_bucket:
            return PreflightResult(False, "codebuild project and immutable input bucket are required")
        projects = self.client.batch_get_projects(names=[self.project_name]).get("projects", [])
        if len(projects) != 1:
            return PreflightResult(False, "codebuild project is unavailable")
        project = projects[0]
        environment = project.get("environment", {})
        image = str(environment.get("image", ""))
        vpc = project.get("vpcConfig", {})
        source = project.get("source", {})
        artifacts = project.get("artifacts", {})
        if not callable(self.network_preflight):
            return PreflightResult(False, "codebuild external network and endpoint preflight is required")
        try:
            network_result = self.network_preflight(project, self.input_bucket)
        except Exception:
            return PreflightResult(False, "codebuild external network and endpoint preflight failed")
        if not isinstance(network_result, PreflightResult) or not network_result.available:
            detail = network_result.detail if isinstance(network_result, PreflightResult) else "invalid external preflight result"
            return PreflightResult(False, "codebuild external network and endpoint preflight failed: " + detail)
        try:
            public_block = self.s3.get_public_access_block(Bucket=self.input_bucket)["PublicAccessBlockConfiguration"]
            versioning = self.s3.get_bucket_versioning(Bucket=self.input_bucket).get("Status") == "Enabled"
            encryption = self.s3.get_bucket_encryption(Bucket=self.input_bucket).get("ServerSideEncryptionConfiguration", {}).get("Rules", [{}])[0].get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") == "aws:kms"
            policy_private = self.s3.get_bucket_policy_status(Bucket=self.input_bucket).get("PolicyStatus", {}).get("IsPublic") is False
        except Exception:
            public_block, versioning, encryption, policy_private = {}, False, False, False
        scope = self._scope()
        configured_digests = all(
            len(scope[name]) == 64 and all(character in "0123456789abcdef" for character in scope[name])
            for name in ("image_digest", "wrapper_sha256")
        )
        image_prefix = f"{scope['account_id']}.dkr.ecr.{scope['region']}.amazonaws.com/"
        role_parts = str(project.get("serviceRole", "")).split(":", 5)
        probe_result = self._probe_available(project)
        checks = {
            "approved_digests": configured_digests,
            "image_digest": configured_digests and image.endswith("@sha256:" + scope["image_digest"]),
            "image_scope": bool(scope["account_id"] and scope["region"] and image.startswith(image_prefix)),
            "role_account": len(role_parts) == 6 and role_parts[4] == scope["account_id"],
            "probe_attested": probe_result.available,
            "privileged_disabled": environment.get("privilegedMode", False) is False,
            "service_role": str(project.get("serviceRole", "")).startswith("arn:"),
            "private_network": bool(vpc.get("vpcId") and vpc.get("subnets") and vpc.get("securityGroupIds")),
            "external_network_preflight": network_result.available,
            "source_locked": source.get("type") == "NO_SOURCE" and not source.get("location"),
            "artifacts_locked": artifacts.get("type") == "NO_ARTIFACTS",
            "input_bucket_private": bool(public_block) and all(public_block.get(name) is True for name in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")) and policy_private,
            "input_bucket_immutable": versioning and encryption,
            "public_builds_disabled": project.get("projectVisibility", "PRIVATE") == "PRIVATE",
        }
        failed = [name for name, ok in checks.items() if not ok]
        return PreflightResult(not failed, "" if not failed else "codebuild isolation preflight failed: " + ", ".join(failed))

    @staticmethod
    def _zip_snapshot(
        root: Path,
        destination: Path,
        project_slug: str,
        input_sha256: str,
    ) -> tuple[int, str]:
        return write_verified_snapshot_zip(root, destination, project_slug, input_sha256)

    def _put_input(self, key: str, source: Path, size: int, digest: str, input_sha256: str) -> str:
        response = None
        try:
            current = self.s3.head_object(Bucket=self.input_bucket, Key=key, ChecksumMode="ENABLED")
        except Exception as exc:
            if not _not_found(exc):
                raise
            try:
                with source.open("rb") as handle:
                    response = self.s3.put_object(
                        Bucket=self.input_bucket, Key=key, Body=handle, ContentLength=size,
                        ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
                        Metadata={"sha256": digest, "input_sha256": input_sha256}, IfNoneMatch="*",
                    )
            except Exception as put_error:
                if not _precondition_failed(put_error):
                    raise
        if response is not None:
            version_id = response.get("VersionId")
            if not version_id:
                raise RuntimeError("codebuild input write did not return VersionId")
            current = self.s3.head_object(Bucket=self.input_bucket, Key=key, VersionId=str(version_id), ChecksumMode="ENABLED")
        else:
            current = self.s3.head_object(Bucket=self.input_bucket, Key=key, ChecksumMode="ENABLED")
        if int(current["ContentLength"]) != size or _checksum_sha256(current) != digest or current.get("Metadata", {}).get("input_sha256") != input_sha256 or not current.get("VersionId"):
            raise FileExistsError("codebuild input collision")
        return str(current["VersionId"])

    def _prepare_input(self, request: BuildRequest) -> dict[str, str]:
        archive = request.output_root / "codebuild-input.zip"
        size, archive_sha = self._zip_snapshot(
            request.snapshot_root,
            archive,
            request.project_slug,
            str(request.input_sha256),
        )
        key = f"{self.input_prefix}/{request.input_sha256}.zip"
        version_id = self._put_input(key, archive, size, archive_sha, str(request.input_sha256))
        return {"input_version_id": version_id, "input_archive_sha256": archive_sha}

    def prepare_input(self, request: BuildRequest) -> dict[str, str]:
        preflight = self.preflight()
        if not preflight.available:
            raise RuntimeError(preflight.detail)
        if not request.input_sha256 or not request.marker:
            raise RuntimeError("codebuild_input_marker_required")
        return self._prepare_input(request)

    def _validate_prepared_input(self, request: BuildRequest, expected: dict[str, str]) -> dict[str, str]:
        version_id = str(expected.get("input_version_id") or "")
        archive_sha = str(expected.get("input_archive_sha256") or "")
        if (
            not version_id
            or len(version_id) > 1024
            or any(ord(character) < 32 or ord(character) == 127 for character in version_id)
            or len(archive_sha) != 64
            or any(character not in "0123456789abcdef" for character in archive_sha)
        ):
            raise RuntimeError("codebuild_input_anchor_missing")
        key = f"{self.input_prefix}/{request.input_sha256}.zip"
        current = self.s3.head_object(
            Bucket=self.input_bucket,
            Key=key,
            VersionId=version_id,
            ChecksumMode="ENABLED",
        )
        if (
            str(current.get("VersionId") or "") != version_id
            or _checksum_sha256(current) != archive_sha
            or current.get("Metadata", {}).get("input_sha256") != request.input_sha256
        ):
            raise RuntimeError("codebuild_input_anchor_mismatch")
        return {"input_version_id": version_id, "input_archive_sha256": archive_sha}

    def start(self, request: BuildRequest, expected: dict[str, str] | None = None) -> BuildHandle:
        preflight = self.preflight()
        if not preflight.available:
            raise RuntimeError(preflight.detail)
        if not request.input_sha256 or not request.marker:
            raise RuntimeError("codebuild_input_marker_required")
        anchors = self._prepare_input(request) if expected is None else self._validate_prepared_input(request, expected)
        version_id = anchors["input_version_id"]
        archive_sha = anchors["input_archive_sha256"]
        key = f"{self.input_prefix}/{request.input_sha256}.zip"
        token = hashlib.sha256(request.marker.encode("utf-8")).hexdigest()
        response = self.client.start_build(
            projectName=self.project_name,
            idempotencyToken=token,
            sourceTypeOverride="S3",
            sourceLocationOverride=f"{self.input_bucket}/{key}",
            sourceVersionOverride=version_id,
            buildspecOverride=_CODEBUILD_BUILDSPEC,
            environmentVariablesOverride=[
                {"name": "TOOLBOX_PROJECT_SLUG", "value": request.project_slug, "type": "PLAINTEXT"},
                {"name": "TOOLBOX_INPUT_SHA256", "value": request.input_sha256, "type": "PLAINTEXT"},
                {"name": "TOOLBOX_INPUT_ARCHIVE_SHA256", "value": archive_sha, "type": "PLAINTEXT"},
                {"name": "TOOLBOX_INPUT_VERSION_ID", "value": version_id, "type": "PLAINTEXT"},
                {"name": "TOOLBOX_HARNESS_SHA256", "value": self.harness_sha256, "type": "PLAINTEXT"},
                {"name": "TOOLBOX_ATTESTATION_CONTRACT", "value": _CODEBUILD_ATTESTATION_CONTRACT, "type": "PLAINTEXT"},
                {"name": "TOOLBOX_WRAPPER_SHA256", "value": str(self.approved_wrapper_sha256), "type": "PLAINTEXT"},
                {"name": "TOOLBOX_PROBE_VERSION", "value": str(self.probe_version), "type": "PLAINTEXT"},
                {"name": "TOOLBOX_START_MARKER", "value": request.marker, "type": "PLAINTEXT"},
            ],
        )
        build_id = response.get("build", {}).get("id")
        if not build_id:
            raise RuntimeError("codebuild_start_unknown")
        self._expected[build_id] = {
            "input_sha256": request.input_sha256,
            "harness_sha256": self.harness_sha256,
            "input_version_id": version_id,
            "input_archive_sha256": archive_sha,
            "marker": request.marker,
            **self._scope(),
        }
        return BuildHandle(build_id, request.marker)

    @staticmethod
    def _environment(build: dict) -> dict[str, str]:
        return {item.get("name"): item.get("value") for item in build.get("environment", {}).get("environmentVariables", [])}

    def reconcile(self, marker: str, window_seconds: int) -> list[BuildHandle]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(int(window_seconds), 1))
        next_token = None
        seen_tokens: set[str] = set()
        matches: dict[str, BuildHandle] = {}
        pages = 0
        while True:
            options = {"projectName": self.project_name, "sortOrder": "DESCENDING"}
            if next_token:
                options["nextToken"] = next_token
            try:
                page = self.client.list_builds_for_project(**options)
                ids = page.get("ids", [])
                if not isinstance(ids, list):
                    raise ValueError("invalid CodeBuild listing")
                builds: list[dict] = []
                for offset in range(0, len(ids), 100):
                    response = self.client.batch_get_builds(ids=ids[offset:offset + 100])
                    batch = response.get("builds", [])
                    if not isinstance(batch, list):
                        raise ValueError("invalid CodeBuild batch")
                    builds.extend(batch)
            except Exception as exc:
                raise BuildFailed("remote_start_unknown") from exc

            page_is_older = bool(builds)
            for build in builds:
                started = build.get("startTime")
                if isinstance(started, str):
                    try:
                        started = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    except ValueError:
                        started = None
                if isinstance(started, datetime) and started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if not isinstance(started, datetime):
                    if self._environment(build).get("TOOLBOX_START_MARKER") != marker:
                        raise BuildFailed("remote_start_unknown")
                    page_is_older = False
                elif started >= cutoff:
                    page_is_older = False
                if self._environment(build).get("TOOLBOX_START_MARKER") == marker:
                    if isinstance(started, datetime) and started < cutoff:
                        continue
                    build_id = str(build.get("id", ""))
                    if not build_id:
                        raise BuildFailed("remote_start_unknown")
                    matches[build_id] = BuildHandle(build_id, marker)

            next_token = page.get("nextToken")
            if not next_token or page_is_older:
                return list(matches.values())
            token = str(next_token)
            pages += 1
            if token in seen_tokens or pages >= 1000:
                raise BuildFailed("remote_start_unknown")
            seen_tokens.add(token)
            next_token = token

    def poll(self, handle: BuildHandle) -> BuildPoll:
        builds = self.client.batch_get_builds(ids=[handle.id]).get("builds", [])
        if len(builds) != 1:
            return BuildPoll("unknown", "codebuild_unknown")
        status = builds[0].get("buildStatus")
        if status == "SUCCEEDED":
            return BuildPoll("succeeded")
        if status in {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"}:
            return BuildPoll("failed", "codebuild_failed")
        return BuildPoll("running")

    def fetch_outputs(self, handle: BuildHandle, expected: dict[str, str] | None = None) -> BuildOutputs:
        builds = self.client.batch_get_builds(ids=[handle.id]).get("builds", [])
        if len(builds) != 1 or builds[0].get("buildStatus") != "SUCCEEDED":
            raise RuntimeError("codebuild_not_complete")
        build = builds[0]
        source_version = str(build.get("sourceVersion") or "")
        if not source_version:
            raise RuntimeError("codebuild_input_version_mismatch")
        known = {**self._scope(), **self._expected.get(handle.id, {}), **(expected or {})}
        version_anchor = str(known.get("input_version_id") or "")
        archive_anchor = str(known.get("input_archive_sha256") or "")
        if not version_anchor or len(archive_anchor) != 64 or any(character not in "0123456789abcdef" for character in archive_anchor):
            raise RuntimeError("codebuild_input_anchor_missing")
        if source_version != version_anchor:
            raise RuntimeError("codebuild_input_version_mismatch")
        marker = str(known.get("marker") or handle.marker)
        if not marker:
            raise RuntimeError("codebuild_start_marker_mismatch")
        verifier = self.attestation_verifier
        if verifier is None or not callable(getattr(verifier, "verify", None)):
            raise RuntimeError("codebuild_attestation_unavailable")
        attestation_expected = {
            **known,
            "build_id": handle.id,
            "input_version_id": source_version,
            "marker": marker,
        }
        try:
            attestation = verifier.verify(build, attestation_expected)
        except Exception as exc:
            raise RuntimeError("codebuild_attestation_verification_failed") from exc
        required = {
            "contract", "attestation_id", "account_id", "region", "project_name", "build_id",
            "image_digest", "wrapper_sha256", "probe_version", "input_sha256", "input_version_id",
            "input_archive_sha256", "harness_sha256", "marker", "phases",
        }
        if not isinstance(attestation, dict) or set(attestation) != required:
            raise RuntimeError("codebuild_attestation_invalid")
        scalar_fields = required - {"attestation_id", "phases", "input_archive_sha256"}
        if any(
            attestation_expected.get(name) not in {None, ""}
            and str(attestation.get(name, "")) != str(attestation_expected[name])
            for name in scalar_fields
        ):
            raise RuntimeError("codebuild_attestation_mismatch")
        archive_digest = str(attestation.get("input_archive_sha256", ""))
        if (
            len(archive_digest) != 64
            or any(character not in "0123456789abcdef" for character in archive_digest)
            or (known.get("input_archive_sha256") and archive_digest != known["input_archive_sha256"])
        ):
            raise RuntimeError("codebuild_attestation_mismatch")
        attestation_id = str(attestation["attestation_id"])
        if len(attestation_id) != 64 or any(character not in "0123456789abcdef" for character in attestation_id):
            raise RuntimeError("codebuild_attestation_invalid")
        phases = attestation["phases"]
        if not isinstance(phases, list) or [item.get("name") for item in phases if isinstance(item, dict)] != list(_CODEBUILD_REQUIRED_PHASES):
            raise RuntimeError("codebuild_attestation_phases_invalid")
        for item in phases:
            if set(item) != {"name", "status", "evidence_sha256"} or item["status"] != "passed":
                raise RuntimeError("codebuild_attestation_phases_invalid")
            evidence = str(item["evidence_sha256"])
            if len(evidence) != 64 or any(character not in "0123456789abcdef" for character in evidence):
                raise RuntimeError("codebuild_attestation_phases_invalid")
        with self._attestation_lock:
            owner = next((build_id for build_id, value in self._accepted_attestations.items() if value == attestation_id), None)
            if owner is not None and owner != handle.id:
                raise RuntimeError("codebuild_attestation_replay")
            self._accepted_attestations[handle.id] = attestation_id
        return BuildOutputs(
            True,
            str(known["harness_sha256"]),
            ({"phase": "remote", "stdout": f"CodeBuild {handle.id} verified signed phase attestation", "stderr": ""},),
        )

    def execute(self, request: BuildRequest) -> BuildOutputs:
        marker = request.marker or "manual-" + hashlib.sha256(f"{request.project_slug}:{request.input_sha256}".encode()).hexdigest()
        request = BuildRequest(request.project_slug, request.snapshot_root, request.output_root, request.timeout_seconds, request.input_sha256, marker)
        handle = self.start(request)
        deadline = time.monotonic() + request.timeout_seconds
        while time.monotonic() < deadline:
            poll = self.poll(handle)
            if poll.status == "succeeded":
                return self.fetch_outputs(handle, self._expected[handle.id])
            if poll.status in {"failed", "unknown"}:
                raise RuntimeError(poll.error_code or "codebuild_failed")
            time.sleep(1)
        raise RuntimeError("codebuild_timeout")


class CloudWatchLogStore:
    _PHASE_MAX_BYTES = 5 * 1024 * 1024
    _EVENT_MAX_BYTES = 1024 * 1024 - 26

    def __init__(self, log_group: str, client=None) -> None:
        if not log_group:
            raise ValueError("CloudWatch log group is required")
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "logs",
                config=Config(
                    connect_timeout=5,
                    read_timeout=5,
                    retries={"max_attempts": 0},
                ),
            )
        self.client = client
        self.log_group = log_group

    @classmethod
    def _message_chunks(cls, payload: bytes):
        start = 0
        while start < len(payload):
            end = min(start + cls._EVENT_MAX_BYTES, len(payload))
            while end < len(payload) and payload[end] & 0xC0 == 0x80:
                end -= 1
            yield payload[start:end].decode("utf-8")
            start = end

    def append(self, ref: str, phase: str, safe_chunk: str) -> AppendResult:
        stream = f"{ref}/{phase}"
        try:
            self.client.create_log_stream(logGroupName=self.log_group, logStreamName=stream)
        except Exception:
            pass
        encoded = safe_chunk.encode("utf-8")
        payload = encoded[:self._PHASE_MAX_BYTES].decode("utf-8", errors="ignore").encode("utf-8")
        timestamp = int(time.time() * 1000)
        sequence_token = None
        for index, message in enumerate(self._message_chunks(payload)):
            request = {
                "logGroupName": self.log_group,
                "logStreamName": stream,
                "logEvents": [{"timestamp": timestamp + index, "message": message}],
            }
            if sequence_token:
                request["sequenceToken"] = sequence_token
            response = self.client.put_log_events(**request) or {}
            sequence_token = response.get("nextSequenceToken")
        return AppendResult(len(payload), len(payload) != len(encoded))

    def read_page(self, ref: str, phase: str, cursor: str | None, line_limit: int, byte_limit: int) -> LogPage:
        response = self.client.get_log_events(logGroupName=self.log_group, logStreamName=f"{ref}/{phase}", nextToken=cursor, limit=min(line_limit, 500), startFromHead=True)
        lines: list[str] = []
        used = 0
        for event in response.get("events", []):
            for line in str(event.get("message", "")).splitlines():
                encoded = line.encode("utf-8")
                if used + len(encoded) > byte_limit or len(lines) >= line_limit:
                    return LogPage(tuple(lines), response.get("nextForwardToken"), has_more=True)
                lines.append(line)
                used += len(encoded)
        next_cursor = response.get("nextForwardToken")
        return LogPage(tuple(lines), next_cursor, has_more=bool(next_cursor))


class SesNotificationProvider:
    def __init__(self, source: str | None, recipients: tuple[str, ...], client=None) -> None:
        if not source or not recipients:
            raise ValueError("SES source and recipients are required")
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "ses",
                config=Config(
                    connect_timeout=5,
                    read_timeout=5,
                    retries={"max_attempts": 0},
                ),
            )
        self.client, self.source, self.recipients = client, source, recipients

    def send(self, effect_id: str, generation: int, envelope: NotificationEnvelope) -> ProviderResult:
        body = json.dumps(envelope.__dict__, sort_keys=True)
        response = self.client.send_email(Source=self.source, Destination={"ToAddresses": list(self.recipients)}, Message={"Subject": {"Data": f"Toolbox {envelope.project} {envelope.version}"}, "Body": {"Text": {"Data": body}}}, Tags=[{"Name": "effect", "Value": f"{effect_id}-{generation}"}])
        return ProviderResult(True, response.get("MessageId"))


class TeamsNotificationProvider:
    def __init__(self, secret_env: str | None, client=None, allowed_hosts: tuple[str, ...] = ()) -> None:
        from urllib.parse import urlparse
        if not secret_env or not os.environ.get(secret_env):
            raise ValueError("Teams webhook secret environment variable is required")
        self.url = os.environ[secret_env]
        parsed = urlparse(self.url)
        normalized_hosts = {host.casefold() for host in allowed_hosts}
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.hostname.casefold() not in normalized_hosts:
            raise ValueError("Teams webhook must use HTTPS and an explicitly allowlisted host")
        self.client = client

    def send(self, effect_id: str, generation: int, envelope: NotificationEnvelope) -> ProviderResult:
        payload = {**envelope.__dict__, "effect": f"{effect_id}:{generation}"}
        if self.client is None:
            from urllib.request import HTTPRedirectHandler, Request, build_opener

            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            request = Request(self.url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            with build_opener(NoRedirect()).open(request, timeout=5) as response:
                response_body = response.read(65537)
                if len(response_body) > 65536:
                    raise ValueError("Teams response exceeds 64 KiB")
                ok = 200 <= response.status < 300
        else:
            ok = bool(self.client.send(self.url, payload, timeout=5, allow_redirects=False, max_response_bytes=65536))
        return ProviderResult(ok, f"{effect_id}:{generation}" if ok else None, "delivered" if ok else "failed")


class CompositeNotificationProvider:
    def __init__(self, providers: tuple[object, ...]) -> None:
        self.providers = providers

    def send(self, effect_id: str, generation: int, envelope: NotificationEnvelope) -> ProviderResult:
        if not self.providers:
            return ProviderResult(True, "disabled")
        results = [provider.send(effect_id, generation, envelope) for provider in self.providers]
        return ProviderResult(all(result.ok for result in results), ",".join(filter(None, (result.provider_id for result in results))))
