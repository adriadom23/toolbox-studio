from __future__ import annotations

import base64
import hashlib
import io
import os
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.studio_config import StudioConfig
from app.projects.safe_fs import StableRegularFile
from app.submissions.aws_providers import AwsVpcEndpointPreflight, CloudWatchLogStore, CodeBuildProvider, S3ArtifactStore, TeamsNotificationProvider
from app.submissions.provider_factory import create_providers
from app.submissions.providers import BuildRequest, NotificationEnvelope, PreflightResult
from app.submissions.snapshot import SafeSnapshotBuilder


class S3Error(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.head_error = None
        self.version = 0
        self.presigned = None

    def _find(self, key, version_id=None):
        versions = self.objects.get(key, [])
        if not versions:
            raise S3Error("NoSuchKey")
        if version_id is None:
            return versions[-1]
        return next((item for item in versions if item[0] == version_id), None) or (_ for _ in ()).throw(S3Error("NoSuchVersion"))

    def head_object(self, Bucket, Key, VersionId=None, ChecksumMode=None):
        if self.head_error:
            raise S3Error(self.head_error)
        version_id, body, metadata, checksum = self._find(Key, VersionId)
        return {"ContentLength": len(body), "Metadata": metadata, "VersionId": version_id, "ChecksumSHA256": checksum}

    def put_object(self, Bucket, Key, Body, ContentLength, ChecksumSHA256, Metadata, IfNoneMatch):
        assert IfNoneMatch == "*"
        if Key in self.objects:
            raise S3Error("PreconditionFailed")
        body = Body.read()
        assert len(body) == ContentLength
        assert base64.b64decode(ChecksumSHA256) == bytes.fromhex(Metadata["sha256"])
        self.version += 1
        version_id = f"version-{self.version}"
        self.objects.setdefault(Key, []).append((version_id, body, Metadata, ChecksumSHA256))
        return {"VersionId": version_id, "ChecksumSHA256": ChecksumSHA256}

    def overwrite(self, Key, body, metadata):
        self.version += 1
        version_id = f"version-{self.version}"
        checksum = base64.b64encode(hashlib.sha256(body).digest()).decode()
        self.objects.setdefault(Key, []).append((version_id, body, metadata, checksum))
        return version_id

    def get_object(self, Bucket, Key, VersionId=None, ChecksumMode=None):
        version_id, body, _metadata, checksum = self._find(Key, VersionId)
        return {"Body": io.BytesIO(body), "VersionId": version_id, "ChecksumSHA256": checksum}

    def generate_presigned_url(self, name, Params, ExpiresIn):
        assert ExpiresIn <= 900
        self.presigned = Params
        return "https://example.invalid/download"

    def get_public_access_block(self, Bucket):
        return {"PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True}}

    def get_bucket_versioning(self, Bucket):
        return {"Status": "Enabled"}

    def get_bucket_encryption(self, Bucket):
        return {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]}}

    def get_bucket_policy_status(self, Bucket):
        return {"PolicyStatus": {"IsPublic": False}}


def test_s3_contract_uses_conditional_create_and_propagates_operational_errors(tmp_path):
    source = tmp_path / "artifact.zip"
    source.write_bytes(b"zip")
    digest = hashlib.sha256(b"zip").hexdigest()
    client = FakeS3()
    store = S3ArtifactStore("private", client=client)
    ref = store.put_if_absent("object", source, 3, digest)
    assert ref.key.startswith("s3v1:")
    assert store.stat(ref).sha256 == digest
    assert store.create_download(ref, "safe.zip", 9999).ttl_seconds == 900
    assert client.presigned["VersionId"] == "version-1"
    key = client.presigned["Key"]
    client.overwrite(key, b"bad", {"sha256": digest})
    assert store.stat(ref).sha256 == digest
    with store.open_stream(ref) as handle:
        assert handle.read() == b"zip"
    with pytest.raises(FileExistsError):
        store.put_if_absent("object", source, 3, digest)
    client.head_error = "AccessDenied"
    with pytest.raises(S3Error, match="AccessDenied"):
        store.put_if_absent("other", source, 3, digest)


def test_recovery_persists_immutable_s3_version_after_post_put_crash(tmp_path):
    from app.submissions.database import connect, transaction
    from app.submissions.migrations import migrate
    from app.submissions.providers import ObjectRef
    from app.submissions.recovery import recover_interrupted_submissions
    from app.submissions.repository import SubmissionRepository
    from app.submissions.repository_v2 import PipelineRepository

    database = tmp_path / "recovery.sqlite3"
    migrate(database)
    submission_id = "sub_" + "7" * 32
    object_id = f"art:v1:{submission_id}"
    source = tmp_path / "artifact.zip"
    source.write_bytes(b"immutable package")
    size = source.stat().st_size
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    client = FakeS3()
    store = S3ArtifactStore("private", client=client)
    store.put_if_absent(object_id, source, size, digest)

    connection = connect(database)
    with transaction(connection):
        SubmissionRepository(connection).upsert_project(slug="demo", display_name="Demo", created_at=None, modified_at=None)
        PipelineRepository(connection).insert_building(
            submission_id=submission_id, slug="demo", version=1, actor="alice",
            title="Crash recovery", description="Post-put boundary", correlation_id="recovery-s3",
        )
        connection.execute(
            "INSERT INTO artifact_objects(object_id,expected_sha256,expected_size,state,provider,submission_id,manifest_json,input_sha256,harness_sha256,created_at,updated_at) "
            "VALUES (?,?,?,'reserved','s3',?,'{}','input','harness',datetime('now'),datetime('now'))",
            (object_id, digest, size, submission_id),
        )
    connection.close()

    assert recover_interrupted_submissions(database, store) == [submission_id]
    connection = connect(database)
    row = connection.execute(
        "SELECT state,artifact_ref FROM submission_records WHERE id=?", (submission_id,)
    ).fetchone()
    connection.close()
    assert row["state"] == "submitted"
    assert row["artifact_ref"].startswith("s3v1:")

    key = next(iter(client.objects))
    client.overwrite(key, b"new latest version", {"sha256": "different"})
    assert store.stat(ObjectRef("s3", row["artifact_ref"])).sha256 == digest


APPROVED_IMAGE_DIGEST = "a" * 64
APPROVED_WRAPPER_SHA256 = "b" * 64
APPROVED_PROBE_VERSION = "toolbox-probe-v1"
APPROVED_ACCOUNT_ID = "111122223333"
APPROVED_REGION = "eu-west-1"


class FakeCodeBuild:
    def __init__(self):
        self.environment = {}
        self.start = None
        self.build_count = 0

    def batch_get_projects(self, names):
        return {"projects": [{
            "name": names[0],
            "serviceRole": "arn:aws:iam::111122223333:role/toolbox-codebuild",
            "environment": {"image": "111122223333.dkr.ecr.eu-west-1.amazonaws.com/toolbox@sha256:" + "a" * 64, "privilegedMode": False, "environmentVariables": [{"name": "TOOLBOX_NETWORK_POLICY", "value": "isolated-vpc-endpoints"}]},
            "vpcConfig": {"vpcId": "vpc-test", "subnets": ["subnet-test"], "securityGroupIds": ["sg-test"]},
            "source": {"type": "NO_SOURCE"},
            "artifacts": {"type": "NO_ARTIFACTS"},
            "projectVisibility": "PRIVATE",
        }]}

    def start_build(self, **kwargs):
        self.start = kwargs
        self.environment = {item["name"]: item["value"] for item in kwargs["environmentVariablesOverride"]}
        self.build_count += 1
        return {"build": {"id": f"build-{self.build_count}"}}

    def list_builds_for_project(self, **kwargs):
        return {"ids": ["build-1"] if self.start else []}

    def batch_get_builds(self, ids):
        return {"builds": [{
            "id": ids[0],
            "startTime": datetime.now(timezone.utc),
            "buildStatus": "SUCCEEDED",
            "environment": {"environmentVariables": [{"name": name, "value": value} for name, value in self.environment.items()]},
            "sourceVersion": self.start.get("sourceVersionOverride") if self.start else None,
            "exportedEnvironmentVariables": [
                {"name": "TOOLBOX_RESULT_INPUT_SHA256", "value": self.environment["TOOLBOX_INPUT_SHA256"]},
                {"name": "TOOLBOX_RESULT_HARNESS_SHA256", "value": self.environment["TOOLBOX_HARNESS_SHA256"]},
            ],
        }]}


class FakeAttestationVerifier:
    def __init__(self, mutate=None, attestation_id=None):
        self.mutate = mutate
        self.attestation_id = attestation_id

    def preflight(self, _project, scope):
        result = {
            **scope,
            "status": "passed",
            "credentials_canary": "passed",
            "output_canary": "passed",
        }
        return self.mutate("probe", result) if self.mutate else result

    def verify(self, build, expected):
        environment = {
            item.get("name"): item.get("value")
            for item in build.get("environment", {}).get("environmentVariables", [])
        }
        result = {
            "contract": expected["contract"],
            "attestation_id": self.attestation_id or hashlib.sha256((expected["build_id"] + expected["marker"]).encode()).hexdigest(),
            "account_id": expected["account_id"],
            "region": expected["region"],
            "project_name": expected["project_name"],
            "build_id": expected["build_id"],
            "image_digest": expected["image_digest"],
            "wrapper_sha256": expected["wrapper_sha256"],
            "probe_version": expected["probe_version"],
            "input_sha256": expected["input_sha256"],
            "input_version_id": expected["input_version_id"],
            "input_archive_sha256": expected.get("input_archive_sha256") or environment["TOOLBOX_INPUT_ARCHIVE_SHA256"],
            "harness_sha256": expected["harness_sha256"],
            "marker": expected["marker"],
            "phases": [
                {"name": name, "status": "passed", "evidence_sha256": hashlib.sha256(name.encode()).hexdigest()}
                for name in ("input", "frontend", "backend", "package", "credentials_canary", "output_canary")
            ],
        }
        return self.mutate("attestation", result) if self.mutate else result


def codebuild_options(verifier=None):
    return {
        "approved_image_digest": APPROVED_IMAGE_DIGEST,
        "approved_wrapper_sha256": APPROVED_WRAPPER_SHA256,
        "probe_version": APPROVED_PROBE_VERSION,
        "account_id": APPROVED_ACCOUNT_ID,
        "region": APPROVED_REGION,
        "attestation_verifier": verifier or FakeAttestationVerifier(),
    }


def sealed_snapshot(tmp_path: Path, slug: str = "demo"):
    frontend = tmp_path / "snapshot-source" / "frontend" / "src" / "projects"
    backend = tmp_path / "snapshot-source" / "backend" / "app" / "projects"
    (frontend / slug).mkdir(parents=True)
    (backend / slug).mkdir(parents=True)
    (frontend / slug / "Source.vue").write_text("<template />", encoding="utf-8")
    (frontend / slug / "nested").mkdir()
    (frontend / slug / "nested" / "secret.txt").write_bytes(b"inside-good")
    (backend / slug / "routes.py").write_text("x = 1", encoding="utf-8")
    return SafeSnapshotBuilder(frontend, backend).build(slug, tmp_path / "snapshot")


def replace_directory_with_link(source: Path, original: Path, outside: Path) -> bool:
    try:
        source.rename(original)
    except OSError:
        return False
    if os.name == "nt":
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(source), str(outside)],
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr or created.stdout
    else:
        source.symlink_to(outside, target_is_directory=True)
    return True


class FakeLogs:
    def create_log_stream(self, **kwargs): return None
    def put_log_events(self, **kwargs): self.message = kwargs["logEvents"][0]["message"]
    def get_log_events(self, **kwargs): return {"events": [{"message": self.message}], "nextForwardToken": "next"}


class FakeSes:
    def send_email(self, **kwargs):
        assert "Ready" not in kwargs["Message"]["Body"]["Text"]["Data"]
        return {"MessageId": "message-1"}


class FakeRuntime:
    isolated = True


class FakeEc2:
    def describe_route_tables(self, **kwargs):
        return {"RouteTables": [{
            "RouteTableId": "rtb-test",
            "Associations": [{"SubnetId": "subnet-test", "Main": False}],
            "Routes": [{"GatewayId": "local", "State": "active"}],
        }]}

    def describe_vpc_endpoints(self, **kwargs):
        principal = {"AWS": "arn:aws:iam::111122223333:role/toolbox-codebuild"}
        def endpoint(service, endpoint_type="Interface"):
            if service == "s3":
                actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
                resources = ["arn:aws:s3:::private", "arn:aws:s3:::private/*"]
            elif service == "logs":
                actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
                resources = ["arn:aws:logs:eu-west-1:111122223333:log-group:/aws/codebuild/toolbox-build:*"]
            else:
                actions = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
                resources = ["arn:aws:ecr:eu-west-1:111122223333:repository/toolbox"]
            return {
                "ServiceName": f"com.amazonaws.eu-west-1.{service}",
                "VpcEndpointType": endpoint_type,
                "RouteTableIds": ["rtb-test"],
                "SubnetIds": ["subnet-test"],
                "PrivateDnsEnabled": endpoint_type == "Interface",
                "Groups": [{"GroupId": "sg-test"}] if endpoint_type == "Interface" else [],
                "PolicyDocument": {"Statement": [{"Effect": "Allow", "Principal": principal, "Action": actions, "Resource": resources}]},
            }
        return {"VpcEndpoints": [endpoint("s3", "Gateway"), endpoint("logs"), endpoint("ecr.api"), endpoint("ecr.dkr")]}


def test_shared_provider_factory_transfers_immutable_build_input_with_doubles(tmp_path):
    config = StudioConfig(
        profile="shared", host="0.0.0.0", database_path=tmp_path / "db.sqlite3",
        proxy_hmac_secret="secret", trusted_peer_cidrs=("127.0.0.1/32",),
        artifact_provider="s3", build_provider="codebuild", runtime_provider="isolated-gateway",
        log_provider="cloudwatch", notification_providers=("ses",), s3_bucket="private",
        codebuild_project="toolbox-build", cloudwatch_log_group="toolbox/logs",
        codebuild_image_digest=APPROVED_IMAGE_DIGEST, codebuild_wrapper_sha256=APPROVED_WRAPPER_SHA256,
        codebuild_probe_version=APPROVED_PROBE_VERSION, codebuild_account_id=APPROVED_ACCOUNT_ID,
        codebuild_region=APPROVED_REGION,
        ses_source="toolbox@example.invalid", ses_recipients=("owner@example.invalid",),
        public_base_url="https://studio.example", review_base_url="https://studio.example",
        cors_origins=("https://studio.example",), preview_origin="https://preview.example",
        preview_port=443, preview_allowed_origins=("https://preview.example",),
    )
    s3 = FakeS3()
    codebuild = FakeCodeBuild()
    providers = create_providers(config, tmp_path, clients={
        "s3": s3, "codebuild": codebuild, "logs": FakeLogs(),
        "ses": FakeSes(), "runtime": FakeRuntime(),
        "codebuild_network_preflight": lambda project, bucket: PreflightResult(
            bool(project["vpcConfig"]["vpcId"] and bucket == "private"), ""
        ),
        "codebuild_attestation_verifier": FakeAttestationVerifier(),
    })
    snapshot = sealed_snapshot(tmp_path, "demo-ping")
    assert providers.build.preflight().available
    outputs = providers.build.execute(BuildRequest("demo-ping", snapshot.root, tmp_path / "output", timeout_seconds=1, input_sha256=snapshot.input_sha256))
    assert outputs.ok
    recovery_request = BuildRequest(
        "demo-ping",
        snapshot.root,
        tmp_path / "recovery-output",
        timeout_seconds=1,
        input_sha256=snapshot.input_sha256,
        marker="recovery-marker",
    )
    recovery_anchors = providers.build.prepare_input(recovery_request)
    recovery_handle = providers.build.start(recovery_request, recovery_anchors)
    recreated = CodeBuildProvider(
        "toolbox-build",
        "private",
        client=codebuild,
        s3_client=s3,
        network_preflight=lambda project, bucket: PreflightResult(
            bool(project["vpcConfig"]["vpcId"] and bucket == "private"), ""
        ),
        **codebuild_options(),
    )
    recovered = recreated.fetch_outputs(
        recovery_handle,
        {
            "input_sha256": snapshot.input_sha256,
            "harness_sha256": recreated.harness_sha256,
            **recovery_anchors,
        },
    )
    assert recovered.ok
    original_source_version = codebuild.start["sourceVersionOverride"]
    codebuild.start["sourceVersionOverride"] = "altered-version"
    with pytest.raises(RuntimeError, match="codebuild_input_version_mismatch"):
        recreated.fetch_outputs(
            recovery_handle,
            {"input_sha256": snapshot.input_sha256, "harness_sha256": recreated.harness_sha256, **recovery_anchors},
        )
    codebuild.start["sourceVersionOverride"] = original_source_version
    recreated.attestation_verifier = FakeAttestationVerifier(
        lambda stage, result: {**result, "input_archive_sha256": "f" * 64} if stage == "attestation" else result
    )
    with pytest.raises(RuntimeError, match="codebuild_attestation_mismatch"):
        recreated.fetch_outputs(
            recovery_handle,
            {"input_sha256": snapshot.input_sha256, "harness_sha256": recreated.harness_sha256, **recovery_anchors},
        )
    assert codebuild.start["sourceTypeOverride"] == "S3"
    assert codebuild.start["sourceLocationOverride"].startswith("private/toolbox/artifacts/build-inputs/")
    assert codebuild.start["sourceVersionOverride"].startswith("version-")
    assert codebuild.start["idempotencyToken"]
    assert codebuild.environment["TOOLBOX_START_MARKER"]
    assert "/opt/toolbox/bin/build-submission" in codebuild.start["buildspecOverride"]
    assert "exported-variables" not in codebuild.start["buildspecOverride"]
    assert "TOOLBOX_RESULT_INPUT_SHA256" not in codebuild.start["buildspecOverride"]
    providers.logs.append("sub", "build", "safe")
    assert providers.logs.read_page("sub", "build", None, 10, 100).lines == ("safe",)
    result = providers.notifications["ses"].send("eff", 1, NotificationEnvelope("demo", "v1", "12345678", "alice", "submitted", "https://studio.example/submissions/1"))
    assert result.ok


def test_codebuild_input_retains_descendant_parents_and_revalidates_snapshot(tmp_path, monkeypatch):
    snapshot = sealed_snapshot(tmp_path)
    nested = snapshot.root / "frontend" / "src" / "projects" / "demo" / "nested"
    source = nested / "secret.txt"
    outside = tmp_path / "remote-outside"
    outside.mkdir()
    external = outside / "secret.txt"
    external.write_bytes(b"outside-bad")
    source_stat = source.stat()
    os.utime(external, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    original = nested.with_name("nested-original")
    real_open = StableRegularFile.open
    attempted = False
    swapped = False

    @contextmanager
    def swap_parent_then_open(stable_file):
        nonlocal attempted, swapped
        if stable_file.path == source and not attempted:
            attempted = True
            swapped = replace_directory_with_link(nested, original, outside)
        with real_open(stable_file) as opened:
            yield opened

    monkeypatch.setattr(StableRegularFile, "open", swap_parent_then_open)
    archive = tmp_path / "codebuild-input.zip"
    if os.name == "nt":
        CodeBuildProvider._zip_snapshot(snapshot.root, archive, "demo", snapshot.input_sha256)
        assert attempted and not swapped
        with zipfile.ZipFile(archive) as packaged:
            assert packaged.read("frontend/src/projects/demo/nested/secret.txt") == b"inside-good"
    else:
        with pytest.raises(ValueError, match="changed"):
            CodeBuildProvider._zip_snapshot(snapshot.root, archive, "demo", snapshot.input_sha256)
        assert attempted and swapped


def test_cloudwatch_splits_large_unicode_phases_below_the_event_byte_limit():
    class StrictLogs:
        def __init__(self):
            self.events = []
            self.tokens = []

        def create_log_stream(self, **_kwargs):
            return None

        def put_log_events(self, **kwargs):
            event = kwargs["logEvents"][0]
            encoded = event["message"].encode("utf-8")
            assert len(encoded) + 26 <= 1024 * 1024
            self.events.append(event)
            self.tokens.append(kwargs.get("sequenceToken"))
            return {"nextSequenceToken": f"token-{len(self.events)}"}

    client = StrictLogs()
    store = CloudWatchLogStore("toolbox/logs", client)
    content = "á" * (2 * 1024 * 1024) + "tail"
    result = store.append("sub", "build", content)
    assert result.bytes_written == len(content.encode("utf-8"))
    assert not result.truncated
    assert len(client.events) > 1
    assert "".join(event["message"] for event in client.events) == content
    assert [event["timestamp"] for event in client.events] == sorted(event["timestamp"] for event in client.events)
    assert client.tokens[0] is None and client.tokens[1] == "token-1"

    oversized = "🙂" * (2 * 1024 * 1024)
    truncated = store.append("sub", "oversized", oversized)
    assert truncated.bytes_written <= 5 * 1024 * 1024
    assert truncated.truncated
    assert all(len(event["message"].encode("utf-8")) + 26 <= 1024 * 1024 for event in client.events)


def test_shared_factory_builds_only_allowlisted_operational_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_GATEWAY_TOKEN", "secret")
    config = StudioConfig(
        profile="shared", host="0.0.0.0", database_path=tmp_path / "db.sqlite3",
        proxy_hmac_secret="secret", trusted_peer_cidrs=("127.0.0.1/32",),
        artifact_provider="s3", build_provider="codebuild", runtime_provider="isolated-gateway",
        log_provider="cloudwatch", notification_providers=("ses",), s3_bucket="private",
        codebuild_project="toolbox-build", cloudwatch_log_group="toolbox/logs",
        ses_source="toolbox@example.invalid", ses_recipients=("owner@example.invalid",),
        codebuild_image_digest=APPROVED_IMAGE_DIGEST, codebuild_wrapper_sha256=APPROVED_WRAPPER_SHA256,
        codebuild_probe_version=APPROVED_PROBE_VERSION, codebuild_account_id=APPROVED_ACCOUNT_ID,
        codebuild_region=APPROVED_REGION,
        public_base_url="https://studio.example", review_base_url="https://studio.example",
        cors_origins=("https://studio.example",), preview_origin="https://preview.example",
        preview_port=443, preview_allowed_origins=("https://preview.example",),
        codebuild_network_preflight="aws-vpc-endpoints-v1",
        runtime_gateway_url="https://runtime.example/v1",
        runtime_gateway_token_env="RUNTIME_GATEWAY_TOKEN",
        runtime_gateway_allowed_hosts=("runtime.example",),
    )
    providers = create_providers(config, tmp_path, clients={
        "s3": FakeS3(), "codebuild": FakeCodeBuild(), "logs": FakeLogs(), "ec2": FakeEc2(),
        "ses": FakeSes(),
        "codebuild_attestation_verifier": FakeAttestationVerifier(),
    })
    assert providers.build.preflight().available
    assert providers.runtime.isolated


def test_provider_selection_is_authoritative_and_local_never_builds_remote_adapters(tmp_path):
    local = StudioConfig(database_path=tmp_path / "local.sqlite3")
    providers = create_providers(local, tmp_path / "runtime")
    assert providers.identities == {
        "artifact": "local",
        "build": "local-subprocess",
        "logs": "local",
        "notifications": ["local"],
        "publication": "local",
        "runtime": "local-subprocess",
    }
    assert set(providers.notifications) == {"local"}

    with pytest.raises(ValueError, match="local profile only supports local providers"):
        create_providers(
            StudioConfig(database_path=tmp_path / "invalid-local.sqlite3", build_provider="codebuild"),
            tmp_path / "invalid-local-runtime",
        )

    shared_base = dict(
        profile="shared", host="0.0.0.0", database_path=tmp_path / "shared.sqlite3",
        proxy_hmac_secret="secret", trusted_peer_cidrs=("127.0.0.1/32",),
        artifact_provider="s3", build_provider="codebuild", runtime_provider="isolated-gateway",
        log_provider="cloudwatch", s3_bucket="private", codebuild_project="toolbox-build",
        cloudwatch_log_group="toolbox/logs", codebuild_image_digest=APPROVED_IMAGE_DIGEST,
        codebuild_wrapper_sha256=APPROVED_WRAPPER_SHA256, codebuild_probe_version=APPROVED_PROBE_VERSION,
        codebuild_account_id=APPROVED_ACCOUNT_ID, codebuild_region=APPROVED_REGION,
        public_base_url="https://studio.example", review_base_url="https://studio.example",
        cors_origins=("https://studio.example",), preview_origin="https://preview.example",
        preview_port=443, preview_allowed_origins=("https://preview.example",),
    )
    for invalid_notifications in ((), ("local",)):
        with pytest.raises(ValueError, match="shared profile requires"):
            create_providers(
                StudioConfig(**shared_base, notification_providers=invalid_notifications),
                tmp_path / "invalid-shared-runtime",
            )


def test_vpc_preflight_rejects_transit_gateway_and_open_endpoint_policy():
    project = FakeCodeBuild().batch_get_projects(["toolbox-build"])["projects"][0]

    class TransitEc2(FakeEc2):
        def describe_route_tables(self, **kwargs):
            response = super().describe_route_tables(**kwargs)
            response["RouteTables"][0]["Routes"].append({"TransitGatewayId": "tgw-1", "DestinationCidrBlock": "0.0.0.0/0", "State": "active"})
            return response

    assert not AwsVpcEndpointPreflight(TransitEc2())(project, "private").available

    class OpenPolicyEc2(FakeEc2):
        def describe_vpc_endpoints(self, **kwargs):
            response = super().describe_vpc_endpoints(**kwargs)
            response["VpcEndpoints"][0]["PolicyDocument"] = {"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "*", "Resource": "*"}]}
            return response

    result = AwsVpcEndpointPreflight(OpenPolicyEc2())(project, "private")
    assert not result.available
    assert "least-privilege" in result.detail


def test_vpc_preflight_rejects_partial_wildcards_prefix_buckets_and_cross_account_resources():
    project = FakeCodeBuild().batch_get_projects(["toolbox-build"])["projects"][0]

    mutations = (
        (0, "Action", ["s3:*"]),
        (0, "Resource", ["arn:aws:s3:::private-other/*"]),
        (1, "Resource", ["arn:aws:logs:eu-west-1:999999999999:log-group:/aws/codebuild/toolbox-build:*"]),
        (2, "Resource", ["arn:aws:s3:::private/*"]),
    )
    for endpoint_index, field, value in mutations:
        class UnsafeEc2(FakeEc2):
            def describe_vpc_endpoints(self, **kwargs):
                response = super().describe_vpc_endpoints(**kwargs)
                response["VpcEndpoints"][endpoint_index]["PolicyDocument"]["Statement"][0][field] = value
                return response

        assert not AwsVpcEndpointPreflight(UnsafeEc2())(project, "private").available


def test_codebuild_attestation_fails_closed_on_absence_mismatch_and_replay(tmp_path):
    network = lambda project, bucket: PreflightResult(True, "")
    missing = CodeBuildProvider(
        "toolbox-build", "private", client=FakeCodeBuild(), s3_client=FakeS3(),
        network_preflight=network,
        **{key: value for key, value in codebuild_options().items() if key != "attestation_verifier"},
    )
    result = missing.preflight()
    assert not result.available and "probe_attested" in result.detail

    wrong_image = CodeBuildProvider(
        "toolbox-build", "private", client=FakeCodeBuild(), s3_client=FakeS3(),
        network_preflight=network,
        **{**codebuild_options(), "approved_image_digest": "c" * 64},
    )
    assert not wrong_image.preflight().available

    def mismatch(kind, payload):
        if kind == "attestation":
            payload["input_sha256"] = "f" * 64
        return payload

    snapshot = sealed_snapshot(tmp_path)
    mismatched = CodeBuildProvider(
        "toolbox-build", "private", client=FakeCodeBuild(), s3_client=FakeS3(),
        network_preflight=network,
        **codebuild_options(FakeAttestationVerifier(mutate=mismatch)),
    )
    with pytest.raises(RuntimeError, match="codebuild_attestation_mismatch"):
        mismatched.execute(BuildRequest("demo", snapshot.root, tmp_path / "mismatch", 1, snapshot.input_sha256))

    replayed = CodeBuildProvider(
        "toolbox-build", "private", client=FakeCodeBuild(), s3_client=FakeS3(),
        network_preflight=network,
        **codebuild_options(FakeAttestationVerifier(attestation_id="e" * 64)),
    )
    assert replayed.execute(BuildRequest("demo", snapshot.root, tmp_path / "first", 1, snapshot.input_sha256)).ok
    with pytest.raises(RuntimeError, match="codebuild_attestation_replay"):
        replayed.execute(BuildRequest("demo", snapshot.root, tmp_path / "second", 1, snapshot.input_sha256))


def test_codebuild_reconcile_paginates_and_fails_closed_on_incomplete_listing():
    class Paginated(FakeCodeBuild):
        def __init__(self):
            super().__init__()
            self.environment = {"TOOLBOX_START_MARKER": "wanted"}

        def list_builds_for_project(self, **kwargs):
            if kwargs.get("nextToken") == "next":
                return {"ids": ["wanted-id"]}
            return {"ids": [], "nextToken": "next"}

        def batch_get_builds(self, ids):
            return {"builds": [{
                "id": ids[0],
                "startTime": datetime.now(timezone.utc),
                "environment": {"environmentVariables": [{"name": "TOOLBOX_START_MARKER", "value": "wanted"}]},
            }]}

    matches = CodeBuildProvider(
        "toolbox-build", "private", client=Paginated(), s3_client=FakeS3(),
        network_preflight=lambda project, bucket: PreflightResult(True, ""),
    ).reconcile("wanted", 300)
    assert [match.id for match in matches] == ["wanted-id"]

    class Incomplete(Paginated):
        def list_builds_for_project(self, **kwargs):
            raise RuntimeError("listing unavailable")

    with pytest.raises(Exception, match="remote_start_unknown"):
        CodeBuildProvider(
            "toolbox-build", "private", client=Incomplete(), s3_client=FakeS3(),
            network_preflight=lambda project, bucket: PreflightResult(True, ""),
        ).reconcile("wanted", 300)


def test_codebuild_preflight_fails_closed_without_external_network_verification():
    result = CodeBuildProvider("toolbox-build", "private", client=FakeCodeBuild(), s3_client=FakeS3()).preflight()
    assert not result.available
    assert "external network" in result.detail


def test_teams_requires_allowlisted_https_and_bounded_client(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK", "https://hooks.example.test/toolbox")

    class Client:
        def send(self, url, payload, **options):
            assert url == "https://hooks.example.test/toolbox"
            assert options == {"timeout": 5, "allow_redirects": False, "max_response_bytes": 65536}
            return True

    provider = TeamsNotificationProvider("TEAMS_WEBHOOK", Client(), ("hooks.example.test",))
    assert provider.send("eff", 1, NotificationEnvelope("demo", "v1", "12345678", "alice", "submitted", "https://studio.example/submissions/1")).ok
    with pytest.raises(ValueError, match="allowlisted"):
        TeamsNotificationProvider("TEAMS_WEBHOOK", Client(), ("other.example",))
    monkeypatch.setenv("TEAMS_WEBHOOK", "http://hooks.example.test/toolbox")
    with pytest.raises(ValueError, match="HTTPS"):
        TeamsNotificationProvider("TEAMS_WEBHOOK", Client(), ("hooks.example.test",))
