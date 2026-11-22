"""`nuramesh calibrate run|status` (R2.08, `spec/r208/README.md`).

`run` asks one verifier agent to judge every sample of a calibration suite with the fake model and
records ONE `calibration.scored` event; `status` shows whether the verifier is calibrated for each
suite and the trend. Exit codes are those of `eval run`: 0 passed, 1 failed, 2 usage, 3 refused,
4 halted or paused. Calibration never edits a verifier, a gate or a suite.
"""

import argparse
import secrets
import sys
from pathlib import Path
from typing import Any

from nuramesh.cli_rules import add_team

OK, NOT_DONE, USAGE, REFUSED, STOPPED = 0, 1, 2, 3, 4


def add_parsers(sub: Any) -> None:
    """Register `calibrate` on the top-level subparsers."""
    cal = sub.add_parser("calibrate", help="calibrate verifiers against labelled samples")
    cal_sub = cal.add_subparsers(dest="calibrate_command", required=True)
    run = cal_sub.add_parser("run", help="judge a suite's samples with a verifier and record it")
    add_team(run)
    run.add_argument("--suite", required=True)
    run.add_argument("--verifier", required=True)
    run.add_argument("--fake", action="store_true")
    run.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    status = cal_sub.add_parser("status", help="is a verifier calibrated?")
    add_team(status)
    status.add_argument("--verifier", required=True)
    status.add_argument("--suite")
    status.add_argument("--store", help="SQLite file (ignored when NURAMESH_DATABASE_URL is set)")
    cal.set_defaults(func=cmd_calibrate)


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Dispatch `calibrate run` and `calibrate status`."""
    from nuramesh import cli as c
    from nuramesh.config import ConfigError
    from nuramesh.core.config import ConfigValidationError
    from nuramesh.team import TeamError, load_team_config

    root = Path(args.team).resolve()
    try:
        config = load_team_config(root)
    except (TeamError, ConfigError, ConfigValidationError) as exc:
        _say(str(exc))
        return USAGE
    if config.calibration is None:
        _say("the configuration has no calibration suites")
        return USAGE
    if args.calibrate_command == "status":
        return _status(c, args, root, config)
    return _run(c, args, root, config)


def _status(c: Any, args: argparse.Namespace, root: Path, config: Any) -> int:
    from nuramesh.cli_models import context_for, effective_agent_refs
    from nuramesh.engine.calibration import CalibrationLedgerError, standings

    try:
        config.org.agent(args.verifier)
    except KeyError:
        _say(f"unknown agent '{args.verifier}'")
        return USAGE
    ids = [s.id for s in config.calibration.suites]
    if args.suite is not None:
        if args.suite not in ids:
            _say(f"unknown suite '{args.suite}'")
            return USAGE
        ids = [args.suite]
    store = c._open_cli_store(root, args.store, create=False, missing_ok=True)
    if store is None:
        return USAGE
    try:
        model = effective_agent_refs(context_for(root, config, store)).get(args.verifier)
        if model is None:
            model = config.org.model_for(args.verifier)
        try:
            found = standings(
                store,
                config.calibration,
                ids,
                verifier=args.verifier,
                model=model,
                org_id=config.org.id,
                now=store.now(),
            )
        except CalibrationLedgerError as exc:
            _say(str(exc))
            return REFUSED
    finally:
        store.close()
    for s in found:
        print(f"{s.suite}: {'calibrated' if s.calibrated else 'NOT calibrated'}", end="")
        print(f" ({s.reason})" if s.reason else "")
        if s.latest is not None:
            v = s.latest
            k = v.counts
            print(
                f"  latest seq {v.seq} ({v.at:%Y-%m-%d}) {'passed' if v.passed else 'failed'}: "
                f"caught {k.caught} missed {k.missed} wrong_reason {k.wrong_reason} "
                f"bad_error {k.bad_error} accepted {k.accepted} false_reject {k.false_reject} "
                f"good_error {k.good_error}; catch {v.catch_rate} false_accept "
                f"{v.false_accept_rate} accept {v.accept_rate} agreement {v.agreement}"
            )
            print(f"  trend delta_catch_rate: {s.trend if s.trend is not None else 'null'}")
        for r in s.recent_results:
            print(
                f"  result seq {r.seq} ({r.at:%Y-%m-%d}) {'passed' if r.passed else 'failed'} "
                f"catch {r.catch_rate}"
            )
    return OK if all(s.calibrated for s in found) else NOT_DONE


def _run(c: Any, args: argparse.Namespace, root: Path, config: Any) -> int:
    from nuramesh.cli_models import (
        context_for,
        effective_agent_refs,
        governance_for,
        seed_and_warn,
    )
    from nuramesh.core.models import FakeModel
    from nuramesh.engine.calibration import (
        CalibrationError,
        Stop,
        run_calibration,
        verifier_problem,
    )
    from nuramesh.engine.content import (
        ContentError,
        configured_content_service,
        default_ledger_hashing,
    )
    from nuramesh.engine.policy import ModelPolicy

    if not args.fake:
        _say(
            "calibrate run is fake-only: no real provider is supported for calibration in this "
            "version; use --fake"
        )
        return USAGE
    suite = config.calibration.suite(args.suite)
    if suite is None:
        _say(f"unknown suite '{args.suite}'")
        return USAGE
    configured = None
    try:
        configured = config.org.model_for(args.verifier)
    except KeyError:
        pass
    problem = verifier_problem(config.org, args.verifier, configured)
    if problem:
        _say(problem)
        return USAGE
    store = c._open_cli_store(root, args.store, create=True)
    if store is None:
        return USAGE
    run_id = "cal-" + store.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    try:
        denied = c._deny_checks(store, config, command="calibrate", run_id=run_id)
        if denied is not None:
            return int(denied)
        seed_and_warn(store, config)
        refs = effective_agent_refs(context_for(root, config, store))
        problem = verifier_problem(config.org, args.verifier, refs.get(args.verifier))
        if problem:
            _say(problem)
            return USAGE
        governance = governance_for(context_for(root, config, store))
        try:
            module = c.load_team_module(root)
            env = c.TeamEnv(
                root=root, config=config, store=store, run_id=run_id, fake=True,
                sandbox=lambda: None,
            )  # fmt: skip
            team = c.setup_team(module, env)
        except c.TeamError as exc:
            _say(str(exc))
            return USAGE
        fake = team.fake_provider
        if not isinstance(fake, FakeModel):
            _say("--fake needs the team's fake_provider to be a FakeModel")
            return USAGE
        for budget in (*config.budgets, *team.run_budgets):
            c._define(store, budget, config.org.id)
        hashing = default_ledger_hashing(store)
        content = None
        try:
            service = configured_content_service(store, config.content)
            if service.retired(config.org.id) is None:
                content = service
        except ContentError:
            pass
        policy = ModelPolicy(
            store,
            config.effective_policies,
            config.org,
            hasher=hashing.bind(None, config.org.id),
            content=content,
            evals=config.evals,
        )
        try:
            overrides = c._keyed_overrides(config, store)
        except (c.KeyDestroyedError, c.OrphanedContentError) as exc:
            _say(c._destroyed_message(exc))
            return REFUSED
        try:
            result = run_calibration(
                store,
                fake,
                suite,
                org=config.org,
                verifier=args.verifier,
                prices=config.prices,
                run_id=run_id,
                authorize=policy.authorize,
                authorize_spend=policy.authorize_spend,
                overrides=overrides,
                governance=governance,
                hasher=hashing.bind(None, config.org.id),
                after_call=hashing.flush_alerts,
            )
        except CalibrationError as exc:
            _say(str(exc))
            return USAGE
        finally:
            hashing.flush_alerts()
    finally:
        store.close()
    record = result.record
    k = record.counts
    print(
        f"suite {suite.id} (v{suite.version}) for {args.verifier}: "
        f"{'PASSED' if record.passed else 'FAILED'}; catch {record.catch_rate} "
        f"false_accept {record.false_accept_rate} accept {record.accept_rate} "
        f"agreement {record.agreement}"
    )
    print(
        f"  caught {k.caught} missed {k.missed} wrong_reason {k.wrong_reason} "
        f"bad_error {k.bad_error} accepted {k.accepted} false_reject {k.false_reject} "
        f"good_error {k.good_error}; cost {record.cost} {record.currency or ''}"
        f"{'' if record.cost_complete else ' (incomplete)'}"
    )
    if result.stop is not None:
        _say(
            f"the calibration stopped early ({result.stop_detail}); the samples not judged are "
            "recorded as errors"
        )
    if result.stop == Stop.HALT:
        return STOPPED
    if result.stop == Stop.REFUSED:
        return REFUSED
    return OK if record.passed else NOT_DONE
