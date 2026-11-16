#!/usr/bin/env python3
"""Run real persona conversations on a newly created, disposable PostgreSQL DB.

Setup, migration, observation, ZIP creation, and safe teardown are in one
foreground process. Never point this runner at a database with real user data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from persona_agent.behavioral_evaluation import (
    git_revision, hash_migrations, run_scenario, timestamp,
    validate_scenario, write_bundle,
)
from persona_agent.model_gateway import OllamaChatGateway


def create_isolated_database(admin_dsn: str) -> tuple[str, str]:
    """Own only one freshly CREATED database with a randomized name."""
    name = "persona_e2e_" + uuid4().hex[:16]
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        if conn.info.dbname != "postgres":
            raise ValueError(
                "admin DSN must connect to the postgres maintenance DB"
            )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return name, make_conninfo(admin_dsn, dbname=name)


def drop_isolated_database(admin_dsn: str, name: str) -> None:
    if not name.startswith("persona_e2e_") or len(name) != 28:
        raise ValueError("refusing to drop an unknown database name")
    # PostgreSQL 16; FORCE disconnects only connections to this owned DB.
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        if conn.info.dbname != "postgres":
            raise ValueError("admin DSN must connect to postgres")
        conn.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
        )


def publish_status_file(path: Path) -> None:
    """Signal ZIP publication only after write_bundle returns successfully.

    A private wrapper-owned status path is optional for direct CLI use.
    Atomic replacement prevents the shell from observing a partial marker.
    Failure to publish the marker is treated as uncertain evidence status:
    the runner retains its created database and the wrapper retains Compose.
    """
    temporary = path.with_name(path.name + ".partial")
    try:
        temporary.write_text("PUBLISHED\n", encoding="ascii")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--admin-dsn",default=os.environ.get("PERSONA_E2E_ADMIN_DSN"),
        help="Admin connection to an ISOLATED PostgreSQL server's postgres DB",
    )
    parser.add_argument(
        "--scenario",type=Path,
        default=Path("scenarios/mvp01e_conversation.json"),
    )
    parser.add_argument(
        "--model",default=os.environ.get("ACCEPTANCE_MODEL","qwen2.5:7b"),
    )
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("ACCEPTANCE_OLLAMA_URL","http://127.0.0.1:11434"),
    )
    parser.add_argument(
        "--report",type=Path,
        default=Path("artifacts") / (
            "mvp01e-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-" + uuid4().hex[:8] + ".zip"
        ),
    )
    parser.add_argument(
        "--model-memory-worker",action="store_true",
        help="Enable reviewed model-memory proposals and scripted user review",
    )
    parser.add_argument(
        "--semantic-diagnostics",action="store_true",
        help="Record read-only Stage A semantic classification predictions",
    )
    parser.add_argument(
        "--keep-db",action="store_true",
        help="Keep the newly created database for debugging (no teardown)",
    )
    parser.add_argument(
        "--publication-status-file",type=Path,
        help="Private wrapper-owned path marked PUBLISHED only after ZIP publication",
    )
    args = parser.parse_args(argv)
    if not args.admin_dsn:
        parser.error("PERSONA_E2E_ADMIN_DSN or --admin-dsn is required")
    if args.report.suffix.lower() != ".zip" or args.report.exists():
        parser.error("report must be a NEW .zip path (will not overwrite)")
    scenario = validate_scenario(
        json.loads(args.scenario.read_text(encoding="utf-8"))
    )
    if (not args.semantic_diagnostics
            and any("expect_semantics" in step for step in scenario["steps"])):
        parser.error("scenario semantic expectations require --semantic-diagnostics")
    paths = sorted(Path("migrations").glob("*.sql"))
    if not paths:
        parser.error("no migration files found; run from repository root")
    migration_hashes = hash_migrations(paths)
    git_sha = git_revision()
    database_name = None
    result = None
    bundle_written = False
    cleanup_error = None
    try:
        database_name, dsn = create_isolated_database(args.admin_dsn)
        print("Created isolated database:",database_name,flush=True)
        with psycopg.connect(dsn,autocommit=True) as conn:
            for path in paths:
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
        gateway = OllamaChatGateway(
            model=args.model,base_url=args.ollama_url,timeout_seconds=180,
        )
        result = run_scenario(
            dsn,scenario,gateway,
            run_model_memory_worker=args.model_memory_worker,
            run_semantic_diagnostics=args.semantic_diagnostics,
        )
    except Exception as exc:
        # Produce a partial evidence bundle even on setup or model failures.
        if result is None:
            result = {
                "format_version":"mvp01e.1","name":scenario["name"],
                "started_at":timestamp(),"completed_at":timestamp(),
                "passed":False,"transcript":[],"snapshots":[],
                "model_requests":[],"deliveries":[],"checks":[],
                "errors":[{
                    "step_index":None,"type":type(exc).__name__,
                    "message":str(exc)[:1000],
                }],
            }
    try:
        write_bundle(
            result,scenario,args.report,
            model=args.model,git_sha=git_sha,
            migration_hashes=migration_hashes,
        )
        # The shell wrapper must know whether deleting the Compose volume is
        # safe. If this marker fails, preserve the DB as well: the wrapper
        # cannot distinguish a saved ZIP from an interrupted publication.
        if args.publication_status_file is not None:
            publish_status_file(args.publication_status_file)
        bundle_written = True
        print("Saved evaluation bundle:",args.report.resolve(),flush=True)
    finally:
        if database_name is not None and not args.keep_db and bundle_written:
            try:
                drop_isolated_database(args.admin_dsn,database_name)
                print("Dropped isolated database:",database_name,flush=True)
            except Exception as exc:
                cleanup_error = exc
                print(
                    "CLEANUP FAILED; database retained:",database_name,
                    type(exc).__name__,str(exc)[:500],file=sys.stderr,
                )
        elif database_name is not None:
            print("Database retained:",database_name,flush=True)
    if cleanup_error is not None:
        return 2
    print(
        "Contract checks:", result.get("contract_passed",result["passed"]),
        "| Behavioral checks:", result.get("behavioral_checks_passed","NOT_RUN"),
        "| Human review: REQUIRED",
        flush=True,
    )
    if not result["passed"]:
        print(
            "Contract/behavioral expectations or execution errors recorded in ZIP",
            file=sys.stderr,
        )
        return 1
    print("Deterministic checks passed; conversational quality still requires review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
