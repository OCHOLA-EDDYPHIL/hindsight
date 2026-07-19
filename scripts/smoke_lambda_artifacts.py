"""Import every Terraform-configured Lambda handler from its built artifact."""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path


def _builder_module():
    path = Path(__file__).with_name("build_lambda_artifacts.py")
    spec = importlib.util.spec_from_file_location("build_lambda_artifacts", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Lambda artifact builder: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BUILDER = _builder_module()
ARTIFACTS = _BUILDER.ARTIFACTS
BUILD_ROOT = _BUILDER.BUILD_ROOT
ROOT = _BUILDER.ROOT

TERRAFORM_STACK = ROOT / "infra" / "terraform" / "app" / "main.tf"
_LAMBDA_RESOURCE = re.compile(
    r'^resource\s+"aws_lambda_function"\s+"(?P<resource>[^"]+)"\s*\{'
    r"(?P<body>.*?)(?=^resource\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_HANDLER = re.compile(r'^\s*handler\s*=\s*"(?P<handler>[^"]+)"', re.MULTILINE)
_ARTIFACT = re.compile(
    r'^\s*s3_key\s*=\s*aws_s3_object\.lambda_artifact\["(?P<artifact>[^"]+)"\]\.key',
    re.MULTILINE,
)
_IMPORT_HANDLER = """
import importlib
import pathlib
import sys
import types

artifact_root = pathlib.Path(sys.argv[1]).resolve()
handler = sys.argv[2]
sys.path.insert(0, str(artifact_root))

if not (artifact_root / "boto3").exists():
    def unavailable(*args, **kwargs):
        raise RuntimeError("AWS client access is unavailable during artifact import")

    boto3 = types.ModuleType("boto3")
    boto3.__path__ = []
    boto3.client = unavailable
    boto3.resource = unavailable
    dynamodb = types.ModuleType("boto3.dynamodb")
    dynamodb.__path__ = []
    conditions = types.ModuleType("boto3.dynamodb.conditions")
    botocore = types.ModuleType("botocore")
    botocore.__path__ = []
    config = types.ModuleType("botocore.config")
    exceptions = types.ModuleType("botocore.exceptions")

    class Config:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class ClientError(Exception):
        pass

    class Condition:
        def __init__(self, *args):
            self.args = args

        def eq(self, value):
            return Condition(self, value)

        def lt(self, value):
            return Condition(self, value)

        def not_exists(self):
            return Condition(self)

        def __or__(self, other):
            return Condition(self, other)

    config.Config = Config
    exceptions.ClientError = ClientError
    conditions.Attr = Condition
    conditions.Key = Condition
    sys.modules.update(
        {
            "boto3": boto3,
            "boto3.dynamodb": dynamodb,
            "boto3.dynamodb.conditions": conditions,
            "botocore": botocore,
            "botocore.config": config,
            "botocore.exceptions": exceptions,
        }
    )

module_name, attribute = handler.rsplit(".", 1)
module = importlib.import_module(module_name)
module_path = pathlib.Path(module.__file__).resolve()
if module_path != artifact_root and artifact_root not in module_path.parents:
    raise RuntimeError(f"{module_name} was imported from {module_path}, not {artifact_root}")
if not callable(getattr(module, attribute)):
    raise TypeError(f"configured handler is not callable: {handler}")
"""


@dataclass(frozen=True)
class LambdaHandler:
    function: str
    artifact: str
    handler: str


def configured_handlers(stack_path: Path = TERRAFORM_STACK) -> list[LambdaHandler]:
    """Return the Lambda artifact and handler configured for every Terraform function."""

    stack = stack_path.read_text()
    handlers: list[LambdaHandler] = []
    for match in _LAMBDA_RESOURCE.finditer(stack):
        body = match.group("body")
        handler_match = _HANDLER.search(body)
        artifact_match = _ARTIFACT.search(body)
        if handler_match is None or artifact_match is None:
            raise ValueError(
                f"Lambda {match.group('resource')} must configure a literal handler and artifact"
            )
        handlers.append(
            LambdaHandler(
                function=match.group("resource"),
                artifact=artifact_match.group("artifact"),
                handler=handler_match.group("handler"),
            )
        )
    if not handlers:
        raise ValueError(f"no Lambda functions found in {stack_path}")
    configured_artifacts = {handler.artifact for handler in handlers}
    builder_artifacts = set(ARTIFACTS)
    if configured_artifacts != builder_artifacts:
        raise ValueError(
            "Terraform and the artifact builder disagree: "
            f"configured={sorted(configured_artifacts)}, built={sorted(builder_artifacts)}"
        )
    return handlers


def import_handler(handler: LambdaHandler, *, build_root: Path = BUILD_ROOT) -> None:
    """Extract one built artifact and import one handler in an isolated process."""

    zip_path = build_root / f"hindsight-{handler.artifact}.zip"
    if not zip_path.is_file():
        raise FileNotFoundError(f"Lambda artifact has not been built: {zip_path}")
    with tempfile.TemporaryDirectory(prefix=f"hindsight-{handler.function}-") as directory:
        artifact_root = Path(directory)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(artifact_root)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _IMPORT_HANDLER,
                str(artifact_root),
                handler.handler,
            ],
            check=True,
            cwd=artifact_root,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--terraform-stack",
        type=Path,
        default=TERRAFORM_STACK,
    )
    args = parser.parse_args()
    for handler in configured_handlers(args.terraform_stack):
        import_handler(handler)
        print(f"{handler.function}: {handler.handler} ({handler.artifact})")


if __name__ == "__main__":
    main()
