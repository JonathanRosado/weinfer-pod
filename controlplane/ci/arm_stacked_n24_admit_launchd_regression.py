#!/usr/bin/env python3
"""Zero-network red cases for the N=24 coordinator launchd one-shot armer.

Three properties carry the weight and each is proven by breaking it:

* the plist can never restart the coordinator, so one armed identity can spend
  at most one series;
* the credentials file is named by path and never opened, so no artifact can
  carry the admin key -- proven by arming successfully against a file the
  process is not permitted to read;
* the arming receipt is published only while launchd still reports the job
  running, re-established immediately before the receipt bytes are written.

A fake launchctl drives every path.  The optional final case bootstraps a real
throwaway job to prove on this machine that the plist shape actually runs once.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
import types


SCRIPTS = Path(__file__).resolve().parent
MODULE_PATH = SCRIPTS / "arm_stacked_n24_admit_launchd.py"
COORDINATOR_PATH = SCRIPTS / "stacked_n24_admit.py"
RUN = "n24-anchor-armer1"
LABEL = f"com.weinfer.{RUN}.stacked-n24-admit"
BASE = "https://fake-tunnel.trycloudflare.com"
SECRET = "rpa_FAKE_ADMIN_KEY_MUST_NEVER_BE_PERSISTED_0123456789"
PASSES: list[str] = []
SKIPS: list[str] = []


def module(path: Path = MODULE_PATH, name: str = "armer") -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules under
    # `from __future__ import annotations`; register before executing.
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


AR = module()


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)
    PASSES.append(label)


def refuses(label: str, call, expect: str) -> None:
    try:
        call()
    except AR.Refusal as error:
        check(label, expect in str(error), f"expected {expect!r} in {error!r}")
    else:
        raise AssertionError(f"{label}: accepted what it must refuse")


def completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeLaunchctl:
    """Records every verb and answers print from a scripted liveness sequence."""

    def __init__(
        self,
        *,
        running_after_bootstrap: list[bool],
        bootstrap_rc: int = 0,
        pre_bootstrap_rc: int = 1,
        bootout_rc: int = 0,
        print_noise: str = "",
        label: str = LABEL,
    ) -> None:
        self.running = list(running_after_bootstrap)
        self.bootstrap_rc = bootstrap_rc
        self.pre_bootstrap_rc = pre_bootstrap_rc
        self.bootout_rc = bootout_rc
        self.print_noise = print_noise
        self.label = label
        self.bootstrapped = False
        self.calls: list[list[str]] = []

    @property
    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]

    def running_body(self) -> str:
        return (
            f"gui/501/{self.label} = {{\n"
            "\tstate = running\n"
            "\tenvironment = {\n"
            f"\t\tXPC_SERVICE_NAME => {self.label}\n"
            f"\t\t{self.print_noise}\n"
            "\t}\n"
            "}\n"
        )

    def __call__(self, command, capture_output=False, text=False, check=False):
        self.calls.append(list(command))
        verb = command[1]
        if verb == "bootstrap":
            self.bootstrapped = True
            if self.bootstrap_rc == 0:
                return completed(0)
            return completed(self.bootstrap_rc, "", "Bootstrap failed: 5: Input/output error")
        if verb == "bootout":
            return completed(self.bootout_rc, "", "" if self.bootout_rc == 0 else "No such process")
        if verb == "print":
            if not self.bootstrapped:
                if self.pre_bootstrap_rc == 0:
                    return completed(0, self.running_body())
                return completed(self.pre_bootstrap_rc, "", "Could not find service")
            alive = self.running.pop(0) if self.running else False
            if alive:
                return completed(0, self.running_body())
            return completed(1, "", "Could not find service")
        raise AssertionError(f"unexpected launchctl verb: {verb}")


def namespace(root: Path, **overrides) -> argparse.Namespace:
    value = {
        "label": LABEL,
        "run_dir": str(root / "armer"),
        "variant": "anchor",
        "public_base": BASE,
        "credentials_file": str(root / "credentials.txt"),
        "run_prefix": RUN,
        "artifact_root": str(root / "series"),
        "pre_spend_receipt": str(root / "pre-spend.json"),
    }
    value.update(overrides)
    return argparse.Namespace(**value)


def seed(root: Path, *, variant: str = "anchor", run_id: str = RUN) -> None:
    (root / "credentials.txt").write_text(SECRET + "\n")
    (root / "pre-spend.json").write_text(
        json.dumps(
            {
                "object": "stacked_n24_pre_spend_receipt_v1",
                "variant": variant,
                "run_id": run_id,
            }
        )
    )


def config(root: Path, module_ref=AR, **overrides):
    return module_ref.config_from_args(namespace(root, **overrides))


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        base_root = Path(raw)

        # --- plist shape: one armed identity can spend at most one series ----
        root = base_root / "shape"
        root.mkdir()
        seed(root)
        shape = config(root)
        built = AR.build_plist(shape)
        check(
            "built plist carries exactly the eight registered keys",
            set(built) == AR.ALLOWED_PLIST_KEYS,
            f"got {sorted(built)}",
        )
        check(
            "built plist is RunAtLoad=true, KeepAlive=false, Background",
            built["RunAtLoad"] is True
            and built["KeepAlive"] is False
            and built["ProcessType"] == "Background",
        )
        check(
            "built plist separates the coordinator stdout and stderr logs",
            built["StandardOutPath"] != built["StandardErrorPath"],
        )
        check(
            "built plist carries only the immutable arming-receipt demand gate",
            built["EnvironmentVariables"]
            == {AR.ARMING_RECEIPT_ENV: str(shape.receipt_file)},
        )
        AR.assert_one_shot(built, shape)
        PASSES.append("the built plist passes its own one-shot assertion")
        check(
            "the plist round-trips through plistlib unchanged",
            plistlib.loads(plistlib.dumps(built, fmt=plistlib.FMT_XML, sort_keys=True))
            == built,
        )

        for key in sorted(AR.RESTART_CAPABLE_KEYS):
            mutant = dict(built)
            mutant[key] = 60 if key == "StartInterval" else True
            refuses(
                f"a plist carrying {key} is refused as restart-capable",
                lambda m=mutant: AR.assert_one_shot(m, shape),
                "restart-capable keys",
            )

        for bad, why in (
            (True, "KeepAlive=true"),
            ({"SuccessfulExit": False}, "a dict KeepAlive"),
            (0, "KeepAlive=0"),
            ("false", "a string KeepAlive"),
        ):
            mutant = dict(built)
            mutant["KeepAlive"] = bad
            refuses(
                f"{why} is refused: only literal false is a one-shot",
                lambda m=mutant: AR.assert_one_shot(m, shape),
                "KeepAlive to literal false",
            )

        for bad, why in ((False, "RunAtLoad=false"), (1, "RunAtLoad=1")):
            mutant = dict(built)
            mutant["RunAtLoad"] = bad
            refuses(
                f"{why} is refused: the one-shot must start on load",
                lambda m=mutant: AR.assert_one_shot(m, shape),
                "RunAtLoad to literal true",
            )

        refuses(
            "an unregistered plist key is refused",
            lambda: AR.assert_one_shot({**built, "Nice": -5}, shape),
            "unregistered keys",
        )
        refuses(
            "a plist missing a required key is refused",
            lambda: AR.assert_one_shot(
                {k: v for k, v in built.items() if k != "ProcessType"}, shape
            ),
            "missing required keys",
        )
        refuses(
            "a plist whose label is not the armed label is refused",
            lambda: AR.assert_one_shot({**built, "Label": "com.other.job"}, shape),
            "label does not match",
        )
        refuses(
            "a plist whose program arguments drift is refused",
            lambda: AR.assert_one_shot(
                {**built, "ProgramArguments": built["ProgramArguments"][:-1]}, shape
            ),
            "not the coordinator contract",
        )
        refuses(
            "a plist with non-string program arguments is refused",
            lambda: AR.assert_one_shot({**built, "ProgramArguments": [1, 2]}, shape),
            "list of strings",
        )
        refuses(
            "a plist that merges stdout and stderr is refused",
            lambda: AR.assert_one_shot(
                {**built, "StandardErrorPath": built["StandardOutPath"]}, shape
            ),
            "separate the coordinator stdout and stderr",
        )
        refuses(
            "a plist pointing at an unarmed log path is refused",
            lambda: AR.assert_one_shot(
                {**built, "StandardOutPath": "/tmp/elsewhere.log"}, shape
            ),
            "not the armed log paths",
        )
        refuses(
            "a plist without the exact arming-receipt demand gate is refused",
            lambda: AR.assert_one_shot(
                {**built, "EnvironmentVariables": {}}, shape
            ),
            "arming-receipt gate",
        )

        # --- exact coordinator argv -------------------------------------------
        argv = AR.coordinator_argv(shape)
        check(
            "coordinator argv is the interpreter, the source, and six positionals",
            len(argv) == 8,
            f"got {len(argv)}",
        )
        check(
            "coordinator argv is exactly the declared order",
            argv
            == [
                sys.executable,
                str(SCRIPTS / "stacked_n24_admit.py"),
                "anchor",
                BASE,
                str(root / "credentials.txt"),
                RUN,
                str(root / "series"),
                str(root / "pre-spend.json"),
            ],
            f"got {argv}",
        )
        check(
            "the armed coordinator source is the sibling coordinator file",
            shape.coordinator_source == COORDINATOR_PATH.resolve(),
        )
        coordinator_text = COORDINATOR_PATH.read_text()
        check(
            "the coordinator still declares exactly six positional arguments",
            "if len(sys.argv) != 7:" in coordinator_text
            and "variant, base, credentials_text, run_prefix, root_text, "
            "pre_spend_text = sys.argv[1:]" in coordinator_text,
        )
        check(
            "the armer derives the coordinator demand-gate timeout from source",
            shape.coordinator_gate_seconds == 10,
        )
        AR.require_proof_budget(8)
        PASSES.append("the nominal proof budget plus margin fits inside an 8s gate")
        refuses(
            "a gate with no proof-execution margin is refused",
            lambda: AR.require_proof_budget(7),
            "proof budget plus execution margin",
        )
        check(
            "the armer never reads the credentials file in its own source",
            "credentials_file.read" not in MODULE_PATH.read_text()
            and "open(config.credentials_file" not in MODULE_PATH.read_text(),
        )

        # --- happy path: not running, then running, then re-proved -----------
        root = base_root / "happy"
        root.mkdir()
        seed(root)
        happy = config(root)
        fake = FakeLaunchctl(
            running_after_bootstrap=[False, True, True], print_noise=SECRET
        )
        receipt = AR.arm(
            happy, runner=fake, now=lambda: 1_800_000_000.0, sleeper=lambda _: None
        )
        check(
            "a coordinator that appears on the second poll arms cleanly",
            happy.receipt_file.is_file() and receipt["attests"] == "launchd_one_shot_started",
        )
        check(
            "arming used only print, bootstrap and print again",
            fake.verbs == ["print", "bootstrap", "print", "print", "print"],
            f"got {fake.verbs}",
        )
        check(
            "launchctl submit and kickstart are never used",
            not ({"submit", "kickstart", "load", "unload", "start"} & set(fake.verbs)),
        )
        check(
            "bootstrap ran exactly once, against the armed plist",
            fake.verbs.count("bootstrap") == 1
            and fake.calls[1]
            == [AR.LAUNCHCTL, "bootstrap", happy.domain, str(happy.plist_file)],
        )
        check(
            "running is re-proved immediately before the receipt is written",
            fake.verbs[-2:] == ["print", "print"],
        )
        check(
            "the receipt attests a start and explicitly refuses to attest completion",
            receipt["does_not_attest"] == "coordinator_completion_or_series_success"
            and receipt["job_remains_loaded_after_coordinator_exit"] is True
            and receipt["teardown_required_bootout_target"] == happy.target,
        )
        check(
            "the receipt declares that coordinator demand blocks until this receipt exists",
            receipt["coordinator_blocks_before_arming_receipt"] is True
            and receipt["coordinator_arming_gate_seconds"] == 10
            and receipt["armer_nominal_proof_budget_seconds"] == 5.0
            and receipt["armer_proof_execution_margin_seconds"] == 2,
        )
        check(
            "the receipt pins the plist, coordinator, armer and pre-spend digests",
            receipt["plist_sha256"] == AR.sha256_file(happy.plist_file)
            and receipt["coordinator_source_sha256"] == AR.sha256_file(COORDINATOR_PATH)
            and receipt["armer_source_sha256"] == AR.sha256_file(MODULE_PATH)
            and receipt["pre_spend_receipt_sha256"]
            == AR.sha256_file(root / "pre-spend.json"),
        )
        check(
            "the receipt pins both launchctl proof digests and persists no proof text",
            len(receipt["launchctl_proof_sha256"]) == 64
            and len(receipt["launchctl_recheck_proof_sha256"]) == 64
            and receipt["launchctl_proof_text_persisted"] is False,
        )
        check(
            "the receipt names the six coordinator identities without any secret",
            receipt["coordinator_argv_identities"]
            == {
                "variant": "anchor",
                "public_base": BASE,
                "credentials_file_path": str(root / "credentials.txt"),
                "run_prefix": RUN,
                "artifact_root": str(root / "series"),
                "pre_spend_receipt": str(root / "pre-spend.json"),
            },
        )
        check(
            "the receipt states the credentials file was neither read nor digested",
            receipt["credentials_file_contents_read"] is False
            and receipt["credentials_file_digest_recorded"] is False,
        )

        # --- the credential value reaches no artifact and no output ----------
        artifacts = {
            path.name: path.read_bytes()
            for path in (
                happy.plist_file,
                happy.receipt_file,
                happy.stdout_log,
                happy.stderr_log,
            )
        }
        for name, payload in artifacts.items():
            check(
                f"the admin key value is absent from {name}",
                SECRET.encode() not in payload,
            )
        check(
            "the admin key value is absent from the returned receipt object",
            SECRET not in json.dumps(receipt, sort_keys=True),
        )
        check(
            "the launchctl proof digest is over text that DID carry the key",
            receipt["launchctl_proof_sha256"]
            == hashlib.sha256(fake.running_body().encode()).hexdigest(),
            "the fake print echoed the secret, so digest-only storage is load-bearing",
        )

        # --- arming works against a credentials file we cannot read ----------
        root = base_root / "unreadable"
        root.mkdir()
        seed(root)
        os.chmod(root / "credentials.txt", 0o000)
        try:
            unreadable = config(root)
            AR.arm(
                unreadable,
                runner=FakeLaunchctl(running_after_bootstrap=[True, True]),
                now=lambda: 1_800_000_000.0,
                sleeper=lambda _: None,
            )
            check(
                "arming succeeds against a credentials file the process cannot read",
                unreadable.receipt_file.is_file(),
            )
        finally:
            os.chmod(root / "credentials.txt", 0o600)

        # --- modes -----------------------------------------------------------
        check("the armer run directory is 0700", mode(happy.run_dir) == 0o700)
        check("the plist is 0600", mode(happy.plist_file) == 0o600)
        check("the arming receipt is 0600", mode(happy.receipt_file) == 0o600)
        check(
            "both coordinator logs are pre-created 0600 so launchd appends to them",
            mode(happy.stdout_log) == 0o600 and mode(happy.stderr_log) == 0o600,
        )

        # --- never running: bootout, no receipt ------------------------------
        root = base_root / "never"
        root.mkdir()
        seed(root)
        never = config(root)
        dead = FakeLaunchctl(running_after_bootstrap=[])
        refuses(
            "a job that never proves running publishes no receipt",
            lambda: AR.arm(never, runner=dead, sleeper=lambda _: None),
            "never reported the coordinator running",
        )
        check(
            "the never-running job is booted out",
            dead.verbs[-1] == "bootout"
            and dead.calls[-1] == [AR.LAUNCHCTL, "bootout", never.target],
        )
        check(
            "no arming receipt exists after a failed proof",
            not never.receipt_file.exists(),
        )
        check(
            "the plist and logs survive a failed proof as evidence",
            never.plist_file.is_file() and never.stdout_log.is_file(),
        )

        # --- bootout failure is reported honestly ----------------------------
        root = base_root / "never-stuck"
        root.mkdir()
        seed(root)
        stuck = config(root)
        refuses(
            "a failed bootout after a failed proof is reported, not hidden",
            lambda: AR.arm(
                stuck,
                runner=FakeLaunchctl(running_after_bootstrap=[], bootout_rc=1),
                sleeper=lambda _: None,
            ),
            "bootout ALSO FAILED",
        )
        check(
            "a failed bootout still publishes no receipt",
            not stuck.receipt_file.exists(),
        )

        # --- exits between the two proofs: bootout, no receipt ---------------
        root = base_root / "raced"
        root.mkdir()
        seed(root)
        raced = config(root)
        racer = FakeLaunchctl(running_after_bootstrap=[True, False])
        refuses(
            "a coordinator that dies between the proof and the receipt arms nothing",
            lambda: AR.arm(raced, runner=racer, sleeper=lambda _: None),
            "between the arming proof and the arming receipt",
        )
        check(
            "the raced job is booted out",
            racer.verbs[-1] == "bootout",
        )
        check(
            "no arming receipt exists after a raced exit",
            not raced.receipt_file.exists(),
        )

        root = base_root / "raced-stuck"
        root.mkdir()
        seed(root)
        raced_stuck = config(root)
        refuses(
            "a failed bootout after the coordinator dies between proofs is reported",
            lambda: AR.arm(
                raced_stuck,
                runner=FakeLaunchctl(
                    running_after_bootstrap=[True, False], bootout_rc=1
                ),
                sleeper=lambda _: None,
            ),
            "bootout ALSO FAILED",
        )
        check(
            "a raced exit with failed bootout still publishes no receipt",
            not raced_stuck.receipt_file.exists(),
        )

        # --- an interruption after publication must not revoke authorization -
        root = base_root / "published-then-interrupted"
        root.mkdir()
        seed(root)
        published = config(root)
        published_fake = FakeLaunchctl(running_after_bootstrap=[True, True])
        real_write_once = AR.write_once

        def publish_then_fail(path: Path, payload: bytes) -> None:
            real_write_once(path, payload)
            if path == published.receipt_file:
                raise RuntimeError("forced interruption after receipt publication")

        AR.write_once = publish_then_fail
        try:
            refuses(
                "an armer failure after receipt publication preserves authorization",
                lambda: AR.arm(
                    published,
                    runner=published_fake,
                    sleeper=lambda _: None,
                ),
                "already published",
            )
        finally:
            AR.write_once = real_write_once
        check(
            "a published receipt is never followed by recovery bootout",
            published.receipt_file.is_file()
            and "bootout" not in published_fake.verbs,
        )

        # --- bootstrap refusal: nothing to bootout, no receipt ---------------
        root = base_root / "nobootstrap"
        root.mkdir()
        seed(root)
        nobootstrap = config(root)
        refused_bootstrap = FakeLaunchctl(
            running_after_bootstrap=[True, True], bootstrap_rc=1
        )
        refuses(
            "a refused bootstrap publishes no receipt",
            lambda: AR.arm(
                nobootstrap, runner=refused_bootstrap, sleeper=lambda _: None
            ),
            "launchctl bootstrap refused",
        )
        check(
            "a refused bootstrap does not bootout a job it never created",
            "bootout" not in refused_bootstrap.verbs,
        )
        check(
            "a refused bootstrap leaves no receipt",
            not nobootstrap.receipt_file.exists(),
        )

        # --- an already-loaded label is never bootstrapped over --------------
        root = base_root / "taken"
        root.mkdir()
        seed(root)
        taken = config(root)
        occupied = FakeLaunchctl(
            running_after_bootstrap=[True, True], pre_bootstrap_rc=0
        )
        refuses(
            "an already-loaded label refuses instead of bootstrapping over it",
            lambda: AR.arm(taken, runner=occupied, sleeper=lambda _: None),
            "launchd already carries this label",
        )
        check(
            "an already-loaded label is neither bootstrapped nor booted out",
            occupied.verbs == ["print"],
            f"got {occupied.verbs}",
        )

        # --- re-arming one run directory refuses -----------------------------
        refuses(
            "re-arming a run directory that already carries arming state refuses",
            lambda: AR.arm(
                config(base_root / "happy"),
                runner=FakeLaunchctl(running_after_bootstrap=[True, True]),
                sleeper=lambda _: None,
            ),
            "already carries arming state",
        )
        check(
            "the original arming receipt is untouched by the refused re-arm",
            json.loads(happy.receipt_file.read_text()) == receipt,
        )

        # --- write_once never overwrites and never follows a symlink ---------
        root = base_root / "writeonce"
        root.mkdir(mode=0o700)
        victim = root / "victim.txt"
        victim.write_text("original")
        refuses(
            "write_once refuses to replace an existing artifact",
            lambda: AR.write_once(victim, b"replaced"),
            "refusing to replace immutable arming artifact",
        )
        check("the existing artifact is unchanged", victim.read_text() == "original")
        link = root / "link.txt"
        link.symlink_to(victim)
        refuses(
            "write_once refuses to write through a symlink",
            lambda: AR.write_once(link, b"through the link"),
            "refusing to replace immutable arming artifact",
        )
        check("the symlink target is unchanged", victim.read_text() == "original")
        dangling = root / "dangling.txt"
        dangling.symlink_to(root / "absent.txt")
        refuses(
            "write_once refuses a dangling symlink that plain exists() would miss",
            lambda: AR.write_once(dangling, b"planted"),
            "refusing to replace immutable arming artifact",
        )

        # --- a symlinked armer run directory refuses -------------------------
        root = base_root / "linkeddir"
        root.mkdir()
        seed(root)
        (root / "real").mkdir()
        (root / "armer").symlink_to(root / "real")
        refuses(
            "a symlinked armer run directory refuses",
            lambda: AR.arm(
                config(root),
                runner=FakeLaunchctl(running_after_bootstrap=[True, True]),
                sleeper=lambda _: None,
            ),
            "must not be a symlink",
        )

        # --- red inputs -------------------------------------------------------
        root = base_root / "reds"
        root.mkdir()
        seed(root)
        reds = [
            ("a relative armer run directory", {"run_dir": "armer"}, "must be an absolute path"),
            ("a relative artifact root", {"artifact_root": "series"}, "must be an absolute path"),
            (
                "an absent credentials file",
                {"credentials_file": str(root / "absent.txt")},
                "credentials file does not exist",
            ),
            (
                "an absent pre-spend receipt",
                {"pre_spend_receipt": str(root / "absent.json")},
                "pre-spend receipt does not exist",
            ),
            ("an unregistered variant", {"variant": "sweep"}, "exactly anchor or realistic"),
            (
                "a label that does not derive from the run prefix",
                {"label": "com.weinfer.other.stacked-n24-admit"},
                "label must be exactly",
            ),
            (
                "a label with the wrong suffix",
                {"label": f"com.weinfer.{RUN}.watchdog"},
                "label must be exactly",
            ),
            (
                "a run prefix carrying a dot",
                {"run_prefix": "n24.anchor", "label": "com.weinfer.n24.anchor.stacked-n24-admit"},
                "run-prefix must be 1-72 characters",
            ),
            ("an empty run prefix", {"run_prefix": "", "label": "com.weinfer..stacked-n24-admit"}, "run-prefix must be 1-72 characters"),
            ("a plaintext http public base", {"public_base": "http://tunnel.example"}, "must be https"),
            (
                "a public base embedding credentials",
                {"public_base": "https://user:pass@tunnel.example"},
                "must not embed credentials",
            ),
            (
                "a public base carrying a query string",
                {"public_base": "https://tunnel.example/?key=abc"},
                "query string or fragment",
            ),
            (
                "a public base carrying whitespace",
                {"public_base": "https://tunnel.example /x"},
                "contain no whitespace",
            ),
            (
                "an armer run directory equal to the artifact root",
                {"run_dir": str(root / "series")},
                "must not overlap",
            ),
        ]
        for why, override, expect in reds:
            refuses(f"{why} refuses", lambda o=override: config(root, **o), expect)

        underscore_root = base_root / "underscore"
        underscore_root.mkdir()
        underscore_run = "n24_anchor"
        seed(underscore_root, run_id=underscore_run)
        underscore = config(
            underscore_root,
            run_prefix=underscore_run,
            label=f"com.weinfer.{underscore_run}.stacked-n24-admit",
        )
        check(
            "an underscore run prefix accepted by the coordinator is also accepted by the armer",
            underscore.run_prefix == underscore_run,
        )

        nested = base_root / "nested"
        nested.mkdir()
        seed(nested)
        refuses(
            "an artifact root nested inside the armer run directory refuses",
            lambda: config(
                nested,
                run_dir=str(nested / "armer"),
                artifact_root=str(nested / "armer" / "series"),
            ),
            "must not overlap",
        )

        # --- pre-spend identity binding --------------------------------------
        root = base_root / "prespend"
        root.mkdir()
        seed(root, variant="realistic")
        refuses(
            "a pre-spend receipt binding another variant refuses",
            lambda: config(root),
            "binds a different variant",
        )
        seed(root, run_id="n24-anchor-other")
        refuses(
            "a pre-spend receipt binding another run refuses",
            lambda: config(root),
            "binds a different run",
        )
        (root / "pre-spend.json").write_text(json.dumps({"object": "something_else"}))
        refuses(
            "a document that is not a pre-spend receipt refuses",
            lambda: config(root),
            "not a stacked N=24 pre-spend receipt",
        )
        (root / "pre-spend.json").write_text("not json")
        refuses(
            "an unparseable pre-spend receipt refuses",
            lambda: config(root),
            "not readable JSON",
        )

        # --- artifact root preconditions -------------------------------------
        root = base_root / "artifactroot"
        root.mkdir()
        seed(root)
        (root / "series").mkdir()
        (root / "series" / "one-shot.lock").mkdir()
        refuses(
            "an artifact root that already carries the coordinator lock refuses",
            lambda: config(root),
            "already carries a one-shot lock",
        )
        root = base_root / "artifactfile"
        root.mkdir()
        seed(root)
        (root / "series").write_text("not a directory")
        refuses(
            "an artifact root that is a plain file refuses",
            lambda: config(root),
            "not a directory",
        )
        root = base_root / "artifactlink"
        root.mkdir()
        seed(root)
        (root / "real-series").mkdir()
        (root / "series").symlink_to(root / "real-series")
        refuses(
            "a symlinked artifact root refuses",
            lambda: config(root),
            "must not be a symlink",
        )

        # --- mutation: the recheck before the receipt is load-bearing --------
        # Mutants live in their own directory beside a link to the real
        # coordinator, so each mutant resolves the same coordinator source
        # the armer does without writing anything into the repository.
        mutants = base_root / "mutants"
        mutants.mkdir()
        (mutants / "stacked_n24_admit.py").symlink_to(COORDINATOR_PATH)
        mutant_source = MODULE_PATH.read_text().replace(
            "                confirmed = run_launchctl([LAUNCHCTL, \"print\", config.target], runner)\n"
            "                if not proved_running(confirmed, config):",
            "                confirmed = printed\n"
            "                if False:",
            1,
        )
        check(
            "the recheck mutation actually patches the armer",
            mutant_source != MODULE_PATH.read_text(),
        )
        mutant_path = mutants / "mutant_recheck.py"
        mutant_path.write_text(mutant_source)
        MUT = module(mutant_path, "armer_mutant_recheck")
        root = base_root / "mutant-raced"
        root.mkdir()
        seed(root)
        mutated = config(root, module_ref=MUT)
        MUT.arm(
            mutated,
            runner=FakeLaunchctl(running_after_bootstrap=[True, False]),
            now=lambda: 1_800_000_000.0,
            sleeper=lambda _: None,
        )
        check(
            "MUTATION: dropping the recheck publishes a receipt for a dead job",
            mutated.receipt_file.is_file(),
            "the raced-exit red case is therefore load-bearing, not decorative",
        )

        # --- mutation: never opening the credentials file is load-bearing ----
        mutant_source = MODULE_PATH.read_text().replace(
            '        "credentials_file_contents_read": False,',
            '        "credentials_file_contents_read": '
            "config.credentials_file.read_text(),",
            1,
        )
        check(
            "the credential-read mutation actually patches the armer",
            mutant_source != MODULE_PATH.read_text(),
        )
        mutant_path = mutants / "mutant_credential.py"
        mutant_path.write_text(mutant_source)
        MUT = module(mutant_path, "armer_mutant_credential")
        root = base_root / "mutant-credential"
        root.mkdir()
        seed(root)
        leaked = config(root, module_ref=MUT)
        leaked_receipt = MUT.arm(
            leaked,
            runner=FakeLaunchctl(running_after_bootstrap=[True, True]),
            now=lambda: 1_800_000_000.0,
            sleeper=lambda _: None,
        )
        check(
            "MUTATION: reading the credentials file puts the admin key in the receipt",
            SECRET in leaked.receipt_file.read_text()
            and SECRET in json.dumps(leaked_receipt),
            "the secret-absent checks are therefore load-bearing",
        )
        os.chmod(root / "credentials.txt", 0o000)
        try:
            root2 = base_root / "mutant-credential-2"
            root2.mkdir()
            seed(root2)
            os.chmod(root2 / "credentials.txt", 0o000)
            unreadable_mutant = config(root2, module_ref=MUT)
            try:
                MUT.arm(
                    unreadable_mutant,
                    runner=FakeLaunchctl(running_after_bootstrap=[True, True]),
                    now=lambda: 1_800_000_000.0,
                    sleeper=lambda _: None,
                )
            except PermissionError:
                PASSES.append(
                    "MUTATION: the unreadable-credentials case fails under a "
                    "reading armer, so it proves contents are never opened"
                )
            else:
                raise AssertionError(
                    "the unreadable-credentials case did not detect a reading armer"
                )
            os.chmod(root2 / "credentials.txt", 0o600)
        finally:
            os.chmod(root / "credentials.txt", 0o600)

        # --- mutation: digest-only proof storage is load-bearing -------------
        mutant_source = MODULE_PATH.read_text().replace(
            '        "launchctl_proof_text_persisted": False,',
            '        "launchctl_proof_text_persisted": True,\n'
            '        "launchctl_proof_text": _LEAKED_PROOF,',
            1,
        ).replace(
            "                proof_digest = hashlib.sha256(printed.stdout.encode()).hexdigest()",
            "                proof_digest = hashlib.sha256(printed.stdout.encode()).hexdigest()\n"
            "                globals()['_LEAKED_PROOF'] = printed.stdout",
            1,
        )
        mutant_path = mutants / "mutant_proof.py"
        mutant_path.write_text(mutant_source)
        MUT = module(mutant_path, "armer_mutant_proof")
        root = base_root / "mutant-proof"
        root.mkdir()
        seed(root)
        proof_leak = config(root, module_ref=MUT)
        MUT.arm(
            proof_leak,
            runner=FakeLaunchctl(
                running_after_bootstrap=[True, True], print_noise=SECRET
            ),
            now=lambda: 1_800_000_000.0,
            sleeper=lambda _: None,
        )
        check(
            "MUTATION: persisting the launchctl proof text leaks the job environment",
            SECRET in proof_leak.receipt_file.read_text(),
            "launchctl print dumps the environment, so digest-only storage matters",
        )

    native_one_shot_proof()

    for label in PASSES:
        print(f"PASS {label}")
    for label in SKIPS:
        print(f"SKIP {label}")
    print(f"\ncoordinator-armer regression: {len(PASSES)} checks PASS", end="")
    print(f", {len(SKIPS)} skipped" if SKIPS else "")
    return 0


def native_one_shot_proof() -> None:
    """Prove on THIS machine that the armed plist shape runs exactly once.

    Everything above is a fake-launchctl argument about what the plist says.
    This is the only case that asks the real launchd what the plist DOES.  It
    is skip-safe: a GUI domain that refuses to bootstrap reports SKIP rather
    than failing, and the job is always booted out.
    """

    if sys.platform != "darwin" or not Path(AR.LAUNCHCTL).is_file():
        SKIPS.append("native launchd run-count proof (not macOS)")
        return
    label = f"com.weinfer.armer_selftest-{os.getpid()}-{int(time.time())}.one-shot"
    target = f"gui/{os.getuid()}/{label}"
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        os.chmod(root, 0o700)
        counter = root / "runs.txt"
        script = root / "once.sh"
        script.write_text("#!/bin/bash\necho ran >> " + str(counter) + "\n")
        os.chmod(script, 0o700)
        value = {
            "Label": label,
            "ProgramArguments": ["/bin/bash", str(script)],
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Background",
            "EnvironmentVariables": {
                AR.ARMING_RECEIPT_ENV: str(root / "gate.json")
            },
            "StandardOutPath": str(root / "out.log"),
            "StandardErrorPath": str(root / "err.log"),
        }
        # The throwaway job must carry the SAME key set and the same one-shot
        # values the armer writes, or this proves nothing about the armer.
        if set(value) != AR.ALLOWED_PLIST_KEYS:
            raise AssertionError(
                "native proof plist key set drifted from the armer's registered keys"
            )
        if not (
            value["RunAtLoad"] is True
            and value["KeepAlive"] is False
            and value["ProcessType"] == "Background"
            and not (set(value) & AR.RESTART_CAPABLE_KEYS)
        ):
            raise AssertionError("native proof plist is not the armer's one-shot shape")
        plist = root / "once.plist"
        plist.write_bytes(plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True))
        booted = subprocess.run(
            [AR.LAUNCHCTL, "bootstrap", f"gui/{os.getuid()}", str(plist)],
            capture_output=True,
            text=True,
            check=False,
        )
        if booted.returncode != 0:
            SKIPS.append(
                "native launchd run-count proof (GUI domain refused bootstrap: "
                + (booted.stderr.strip() or "unknown")
                + ")"
            )
            return
        try:
            for _ in range(40):
                if counter.exists() and counter.read_text().count("ran") >= 1:
                    break
                time.sleep(0.1)
            first = counter.read_text().count("ran") if counter.exists() else 0
            if first != 1:
                raise AssertionError(
                    f"native one-shot ran {first} times on its first observation"
                )
            time.sleep(3.0)
            second = counter.read_text().count("ran")
            check(
                "NATIVE: the armed plist shape ran exactly once and never restarted",
                second == 1,
                f"ran {second} times after a 3s settle",
            )
            printed = subprocess.run(
                [AR.LAUNCHCTL, "print", target],
                capture_output=True,
                text=True,
                check=False,
            )
            check(
                "NATIVE: the job stays loaded after the one-shot exits",
                printed.returncode == 0 and AR.RUNNING_MARKER not in printed.stdout,
                "a loaded-but-not-running job is what teardown must bootout",
            )
        finally:
            subprocess.run(
                [AR.LAUNCHCTL, "bootout", target],
                capture_output=True,
                text=True,
                check=False,
            )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"FAIL {error}", file=sys.stderr)
        raise SystemExit(1)
