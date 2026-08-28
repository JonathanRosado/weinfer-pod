#!/usr/bin/env python3
"""Arm the registered 24-batch coordinator as a non-restarting launchd one-shot.

`launchctl submit` is deliberately not used: on macOS it can restart a cleanly
exited submitted process.  `launchctl kickstart` is never used either, because
this helper must not be able to start a second series under one identity.  It
writes an immutable plist with RunAtLoad=true and KeepAlive=false, bootstraps it
into the caller's GUI domain, and refuses to publish an arming receipt until
launchd reports the exact label running twice -- the second time immediately
before the receipt bytes are written.

The receipt attests exactly one fact: the one-shot STARTED.  It never attests
that the coordinator finished, that any batch was accepted, or that the series
succeeded.  The job stays loaded in the GUI domain after the coordinator exits;
teardown must bootout the exact target the receipt names.

The credentials file is named by PATH and never opened.  `launchctl print` dumps
a job's environment, so the raw proof text is digested and discarded rather than
persisted -- only the digest and the two markers derived from our own constants
reach disk.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Callable, Sequence


LAUNCHCTL = "/bin/launchctl"
PROOF_ATTEMPTS = 50
PROOF_INTERVAL_SECONDS = 0.1
PROOF_EXECUTION_MARGIN_SECONDS = 2

VARIANTS = ("anchor", "realistic")
COORDINATOR_NAME = "stacked_n24_admit.py"
LABEL_SUFFIX = "stacked-n24-admit"
ARMING_RECEIPT_ENV = "WEINFER_STACKED_N24_ARMING_RECEIPT"

# Match the coordinator's registered run-prefix domain exactly.  A dot is not
# admitted, so the run identity cannot add an extra launchd label segment;
# underscore is an ordinary, unambiguous character inside the derived segment.
RUN_PREFIX_RE = re.compile(r"[A-Za-z0-9_-]{1,72}\Z")

RUNNING_MARKER = "state = running"

ALLOWED_PLIST_KEYS = frozenset(
    {
        "Label",
        "ProgramArguments",
        "RunAtLoad",
        "KeepAlive",
        "ProcessType",
        "EnvironmentVariables",
        "StandardOutPath",
        "StandardErrorPath",
    }
)

# Named separately from the allowlist so the refusal says which restart vector
# was attempted, and so the property survives an edit that widens the allowlist.
RESTART_CAPABLE_KEYS = frozenset(
    {
        "StartInterval",
        "StartCalendarInterval",
        "StartOnMount",
        "WatchPaths",
        "QueueDirectories",
        "ThrottleInterval",
        "OnDemand",
        "inetdCompatibility",
        "Sockets",
        "MachServices",
        "LaunchEvents",
        "SuccessfulExit",
    }
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class Refusal(RuntimeError):
    """Raised for every condition that must stop arming."""


@dataclass(frozen=True)
class Config:
    label: str
    run_dir: Path
    variant: str
    public_base: str
    credentials_file: Path
    run_prefix: str
    artifact_root: Path
    pre_spend_receipt: Path
    python_executable: Path
    coordinator_source: Path
    armer_source: Path
    plist_file: Path
    receipt_file: Path
    stdout_log: Path
    stderr_log: Path
    domain: str
    coordinator_gate_seconds: int

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.label}"

    @property
    def artifacts(self) -> tuple[Path, ...]:
        return (self.plist_file, self.receipt_file, self.stdout_log, self.stderr_log)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def absolute_path(raw: str, name: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise Refusal(f"{name} must be an absolute path")
    return path


def validated_public_base(raw: str) -> str:
    if not raw or any(character.isspace() for character in raw):
        raise Refusal("public base must be nonempty and contain no whitespace")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "https":
        raise Refusal("public base must be https; the admin key crosses this URL")
    if not parsed.hostname:
        raise Refusal("public base must name a host")
    if parsed.username is not None or parsed.password is not None:
        raise Refusal("public base must not embed credentials in the URL")
    if parsed.query or parsed.fragment:
        raise Refusal("public base must not carry a query string or fragment")
    return raw.rstrip("/")


def expected_label(run_prefix: str) -> str:
    return f"com.weinfer.{run_prefix}.{LABEL_SUFFIX}"


def coordinator_gate_seconds(source: Path) -> int:
    matches = re.findall(
        r"^ARMING_RECEIPT_WAIT_SECONDS = ([0-9]+)$",
        source.read_text(),
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise Refusal("coordinator source has no unique literal arming-gate timeout")
    value = int(matches[0])
    if value <= 0:
        raise Refusal("coordinator arming-gate timeout must be positive")
    return value


def require_proof_budget(gate_seconds: int) -> None:
    nominal = PROOF_ATTEMPTS * PROOF_INTERVAL_SECONDS
    if nominal + PROOF_EXECUTION_MARGIN_SECONDS >= gate_seconds:
        raise Refusal(
            "launchd proof budget plus execution margin must stay below the "
            "coordinator arming-gate timeout"
        )


def config_from_args(args: argparse.Namespace) -> Config:
    if args.variant not in VARIANTS:
        raise Refusal("variant must be exactly anchor or realistic")
    run_prefix = args.run_prefix
    if not RUN_PREFIX_RE.fullmatch(run_prefix):
        raise Refusal(
            "run-prefix must be 1-72 characters of A-Z a-z 0-9 - _ so the launchd "
            "label derived from it is unambiguous"
        )
    wanted = expected_label(run_prefix)
    if args.label != wanted:
        raise Refusal(
            f"label must be exactly {wanted} so the launchd identity and the "
            "series identity cannot drift apart"
        )

    run_dir = absolute_path(args.run_dir, "armer run directory")
    credentials_file = absolute_path(args.credentials_file, "credentials file")
    artifact_root = absolute_path(args.artifact_root, "artifact root")
    pre_spend_receipt = absolute_path(args.pre_spend_receipt, "pre-spend receipt")

    # stat() only: the credentials file is named by path and never opened.
    if not credentials_file.is_file():
        raise Refusal("credentials file does not exist")
    if not pre_spend_receipt.is_file():
        raise Refusal("pre-spend receipt does not exist")

    if os.path.islink(artifact_root):
        raise Refusal("artifact root must not be a symlink")
    if os.path.lexists(artifact_root) and not artifact_root.is_dir():
        raise Refusal("artifact root exists and is not a directory")
    if os.path.lexists(artifact_root / "one-shot.lock"):
        raise Refusal(
            "artifact root already carries a one-shot lock; the coordinator "
            "would refuse this identity immediately after launch"
        )
    resolved_run_dir = run_dir.resolve(strict=False)
    resolved_artifact_root = artifact_root.resolve(strict=False)
    if resolved_run_dir == resolved_artifact_root or resolved_run_dir in (
        resolved_artifact_root.parents
    ) or (
        resolved_artifact_root in resolved_run_dir.parents
    ):
        raise Refusal(
            "armer run directory and coordinator artifact root must not overlap"
        )

    require_bound_pre_spend(pre_spend_receipt, variant=args.variant, run_prefix=run_prefix)

    python_executable = Path(sys.executable)
    if not python_executable.is_absolute() or not python_executable.is_file():
        raise Refusal("running interpreter is not an absolute existing file")
    armer_source = Path(__file__).resolve()
    coordinator_source = armer_source.parent / COORDINATOR_NAME
    if not coordinator_source.is_file():
        raise Refusal(f"coordinator source is missing: {coordinator_source}")
    gate_seconds = coordinator_gate_seconds(coordinator_source)
    require_proof_budget(gate_seconds)

    return Config(
        label=args.label,
        run_dir=run_dir,
        variant=args.variant,
        public_base=validated_public_base(args.public_base),
        credentials_file=credentials_file,
        run_prefix=run_prefix,
        artifact_root=artifact_root,
        pre_spend_receipt=pre_spend_receipt,
        python_executable=python_executable,
        coordinator_source=coordinator_source,
        armer_source=armer_source,
        plist_file=run_dir / "admit.plist",
        receipt_file=run_dir / "admit-launchd-arming-receipt.json",
        stdout_log=run_dir / "admit.out.log",
        stderr_log=run_dir / "admit.err.log",
        domain=f"gui/{os.getuid()}",
        coordinator_gate_seconds=gate_seconds,
    )


def require_bound_pre_spend(path: Path, *, variant: str, run_prefix: str) -> None:
    """Cheap identity check only.

    The coordinator owns the full pre-spend contract including freshness; this
    repeats none of it.  It fails only the mismatch an operator would otherwise
    discover as a dead launchd job.
    """

    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Refusal("pre-spend receipt is not readable JSON") from error
    if not isinstance(value, dict):
        raise Refusal("pre-spend receipt is not an object")
    if value.get("object") != "stacked_n24_pre_spend_receipt_v1":
        raise Refusal("pre-spend receipt is not a stacked N=24 pre-spend receipt")
    if value.get("variant") != variant:
        raise Refusal("pre-spend receipt binds a different variant")
    if value.get("run_id") != run_prefix:
        raise Refusal("pre-spend receipt binds a different run")


def coordinator_argv(config: Config) -> list[str]:
    """The coordinator's six positional arguments, in its declared order."""

    return [
        str(config.python_executable),
        str(config.coordinator_source),
        config.variant,
        config.public_base,
        str(config.credentials_file),
        config.run_prefix,
        str(config.artifact_root),
        str(config.pre_spend_receipt),
    ]


def build_plist(config: Config) -> dict[str, object]:
    return {
        "Label": config.label,
        "ProgramArguments": coordinator_argv(config),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            ARMING_RECEIPT_ENV: str(config.receipt_file),
        },
        "StandardOutPath": str(config.stdout_log),
        "StandardErrorPath": str(config.stderr_log),
    }


def assert_one_shot(value: dict[str, object], config: Config) -> None:
    """Refuse any plist that could run the coordinator more than once."""

    restart = sorted(set(value) & RESTART_CAPABLE_KEYS)
    if restart:
        raise Refusal(
            "plist carries restart-capable keys: " + ", ".join(restart)
        )
    unknown = sorted(set(value) - ALLOWED_PLIST_KEYS)
    if unknown:
        raise Refusal("plist carries unregistered keys: " + ", ".join(unknown))
    missing = sorted(ALLOWED_PLIST_KEYS - set(value))
    if missing:
        raise Refusal("plist is missing required keys: " + ", ".join(missing))
    if value["RunAtLoad"] is not True:
        raise Refusal("plist must set RunAtLoad to literal true")
    # Identity, not equality: a dict KeepAlive, True, or 1 all restart the job
    # and all compare equal to or truthy against a loose check.
    if value["KeepAlive"] is not False:
        raise Refusal("plist must set KeepAlive to literal false")
    if value["ProcessType"] != "Background":
        raise Refusal("plist must run the coordinator as a Background process")
    if value["EnvironmentVariables"] != {
        ARMING_RECEIPT_ENV: str(config.receipt_file)
    }:
        raise Refusal(
            "plist environment must carry exactly the coordinator arming-receipt gate"
        )
    if value["Label"] != config.label:
        raise Refusal("plist label does not match the armed label")
    arguments = value["ProgramArguments"]
    if not isinstance(arguments, list) or not all(
        isinstance(item, str) for item in arguments
    ):
        raise Refusal("plist program arguments must be a list of strings")
    if arguments != coordinator_argv(config):
        raise Refusal("plist program arguments are not the coordinator contract")
    if value["StandardOutPath"] == value["StandardErrorPath"]:
        raise Refusal("plist must separate the coordinator stdout and stderr logs")
    if value["StandardOutPath"] != str(config.stdout_log) or value[
        "StandardErrorPath"
    ] != str(config.stderr_log):
        raise Refusal("plist log paths are not the armed log paths")


def write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if os.path.lexists(path):
        raise Refusal(f"refusing to replace immutable arming artifact: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        # The lexists check above is an operator diagnostic; hard-linking the
        # completed temp gives the actual guarantee.  link(2) fails with EEXIST
        # on any existing destination, including a symlink, so this can never
        # follow a planted link and overwrite its target.
        os.link(temporary, path)
        os.unlink(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def prepare_run_dir(config: Config) -> None:
    if os.path.islink(config.run_dir):
        raise Refusal("armer run directory must not be a symlink")
    if os.path.lexists(config.run_dir) and not config.run_dir.is_dir():
        raise Refusal("armer run directory exists and is not a directory")
    config.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.run_dir, 0o700)
    existing = [str(path) for path in config.artifacts if os.path.lexists(path)]
    if existing:
        raise Refusal(
            "this armer run directory already carries arming state: "
            + ", ".join(existing)
        )


def run_launchctl(
    command: Sequence[str], runner: Runner = subprocess.run
) -> "subprocess.CompletedProcess[str]":
    return runner(command, capture_output=True, text=True, check=False)


def proved_running(
    printed: "subprocess.CompletedProcess[str]", config: Config
) -> bool:
    return (
        printed.returncode == 0
        and RUNNING_MARKER in printed.stdout
        and f"XPC_SERVICE_NAME => {config.label}" in printed.stdout
    )


def require_free_label(config: Config, runner: Runner) -> None:
    printed = run_launchctl([LAUNCHCTL, "print", config.target], runner)
    if printed.returncode == 0:
        raise Refusal(
            f"launchd already carries this label: {config.target}; refusing to "
            "bootstrap over a job this armer did not create"
        )


def arming_receipt(
    config: Config,
    *,
    proof_digest: str,
    recheck_digest: str,
    armed_at_epoch: int,
) -> dict[str, object]:
    return {
        "object": "stacked_n24_admit_launchd_arming_receipt_v1",
        "attests": "launchd_one_shot_started",
        "does_not_attest": "coordinator_completion_or_series_success",
        "armed_at_epoch": armed_at_epoch,
        "domain": config.domain,
        "label": config.label,
        "teardown_required_bootout_target": config.target,
        "job_remains_loaded_after_coordinator_exit": True,
        "launchctl_verbs_used": ["print", "bootstrap"],
        "launchctl_submit_or_kickstart_used": False,
        "launchctl_proof_sha256": proof_digest,
        "launchctl_recheck_proof_sha256": recheck_digest,
        "launchctl_proof_markers": {
            "state": RUNNING_MARKER,
            "xpc_service_name": config.label,
        },
        "launchctl_proof_text_persisted": False,
        "python_executable": str(config.python_executable),
        "armer_source_path": str(config.armer_source),
        "armer_source_sha256": sha256_file(config.armer_source),
        "coordinator_source_path": str(config.coordinator_source),
        "coordinator_source_sha256": sha256_file(config.coordinator_source),
        "plist_path": str(config.plist_file),
        "plist_sha256": sha256_file(config.plist_file),
        "pre_spend_receipt_path": str(config.pre_spend_receipt),
        "pre_spend_receipt_sha256": sha256_file(config.pre_spend_receipt),
        "coordinator_argv_identities": {
            "variant": config.variant,
            "public_base": config.public_base,
            "credentials_file_path": str(config.credentials_file),
            "run_prefix": config.run_prefix,
            "artifact_root": str(config.artifact_root),
            "pre_spend_receipt": str(config.pre_spend_receipt),
        },
        "credentials_file_contents_read": False,
        "credentials_file_digest_recorded": False,
        "coordinator_blocks_before_arming_receipt": True,
        "coordinator_arming_gate_seconds": config.coordinator_gate_seconds,
        "armer_nominal_proof_budget_seconds": (
            PROOF_ATTEMPTS * PROOF_INTERVAL_SECONDS
        ),
        "armer_proof_execution_margin_seconds": PROOF_EXECUTION_MARGIN_SECONDS,
        "stdout_log_path": str(config.stdout_log),
        "stderr_log_path": str(config.stderr_log),
    }


def bootstrap_and_prove(
    config: Config,
    *,
    runner: Runner = subprocess.run,
    now: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    bootstrap = run_launchctl(
        [LAUNCHCTL, "bootstrap", config.domain, str(config.plist_file)], runner
    )
    if bootstrap.returncode != 0:
        raise Refusal(
            "launchctl bootstrap refused: "
            + (bootstrap.stderr.strip() or bootstrap.stdout.strip() or "unknown error")
        )

    authorized_payload: bytes | None = None
    try:
        for _ in range(PROOF_ATTEMPTS):
            printed = run_launchctl([LAUNCHCTL, "print", config.target], runner)
            if proved_running(printed, config):
                proof_digest = hashlib.sha256(printed.stdout.encode()).hexdigest()
                # The proof loop and the receipt are separated by the digesting
                # above, so running is re-established immediately before the
                # receipt bytes are written.  A coordinator that dies in that
                # gap must not leave a receipt claiming it started cleanly.
                confirmed = run_launchctl([LAUNCHCTL, "print", config.target], runner)
                if not proved_running(confirmed, config):
                    raise Refusal(
                        "coordinator exited between the arming proof and the "
                        "arming receipt; no receipt published"
                    )
                recheck_digest = hashlib.sha256(confirmed.stdout.encode()).hexdigest()
                receipt = arming_receipt(
                    config,
                    proof_digest=proof_digest,
                    recheck_digest=recheck_digest,
                    armed_at_epoch=int(now()),
                )
                authorized_payload = (
                    json.dumps(receipt, sort_keys=True, indent=2) + "\n"
                ).encode()
                write_once(
                    config.receipt_file,
                    authorized_payload,
                )
                return receipt
            sleeper(PROOF_INTERVAL_SECONDS)
    except BaseException as error:
        published_by_this_attempt = False
        if authorized_payload is not None and not config.receipt_file.is_symlink():
            try:
                published_by_this_attempt = (
                    config.receipt_file.read_bytes() == authorized_payload
                )
            except OSError:
                published_by_this_attempt = False
        if published_by_this_attempt:
            suffix = (
                " (the arming receipt was already published; the coordinator "
                "remains authorized and was NOT booted out)"
            )
            if isinstance(error, Exception):
                raise Refusal(f"armer failed after receipt publication{suffix}") from error
            error.add_note(suffix)
            raise
        bootout = run_launchctl([LAUNCHCTL, "bootout", config.target], runner)
        if bootout.returncode != 0:
            suffix = (
                f" (bootout ALSO FAILED: the job may still be loaded at "
                f"{config.target} and must be booted out by hand)"
            )
            if isinstance(error, Exception):
                raise Refusal(f"{error}{suffix}") from error
            error.add_note(suffix)
        raise

    bootout = run_launchctl([LAUNCHCTL, "bootout", config.target], runner)
    suffix = (
        ""
        if bootout.returncode == 0
        else (
            f" (bootout ALSO FAILED: the job may still be loaded at "
            f"{config.target} and must be booted out by hand)"
        )
    )
    raise Refusal(
        "launchd never reported the coordinator running under the expected "
        f"label; no receipt published{suffix}"
    )


def arm(
    config: Config,
    *,
    runner: Runner = subprocess.run,
    now: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    prepare_run_dir(config)
    require_free_label(config, runner)
    value = build_plist(config)
    assert_one_shot(value, config)
    encoded = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)
    write_once(config.plist_file, encoded)
    # Pre-create both logs so launchd appends to files that are already 0600
    # rather than creating them under its own umask.
    write_once(config.stdout_log, b"")
    write_once(config.stderr_log, b"")
    return bootstrap_and_prove(config, runner=runner, now=now, sleeper=sleeper)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--label", required=True)
    value.add_argument("--run-dir", required=True)
    value.add_argument("--variant", required=True)
    value.add_argument("--public-base", required=True)
    value.add_argument("--credentials-file", required=True)
    value.add_argument("--run-prefix", required=True)
    value.add_argument("--artifact-root", required=True)
    value.add_argument("--pre-spend-receipt", required=True)
    return value


def main() -> int:
    config = config_from_args(parser().parse_args())
    arm(config)
    print(f"COORDINATOR ONE-SHOT STARTED: {config.target}")
    print(f"plist: {config.plist_file}")
    print(f"arming receipt: {config.receipt_file}")
    print(f"stdout log: {config.stdout_log}")
    print(f"stderr log: {config.stderr_log}")
    print(
        "this attests only that the one-shot STARTED -- it does not attest that "
        "the coordinator completed or that the series succeeded"
    )
    print(f"teardown must run: {LAUNCHCTL} bootout {config.target}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"COORDINATOR ARM REFUSED: {error}", file=sys.stderr)
        raise SystemExit(1)
