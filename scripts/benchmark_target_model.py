#!/usr/bin/env python3
"""Summarize a real target-model E2E ZIP without exporting conversations.

The output is safe for a public CI artifact only when the input is the fixed
synthetic scenario. Keep the source ZIP and reviewer packet local/private.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from persona_agent.target_model_benchmark import summarize_target_benchmark


def _ollama_digest(tags_path: Path, model: str) -> str:
    tags=json.loads(tags_path.read_text(encoding="utf-8"))
    matches=[
        entry["digest"] for entry in tags.get("models",[])
        if entry.get("name")==model or entry.get("model")==model
    ]
    if len(matches)!=1:
        raise ValueError("expected exactly one installed target model digest")
    return matches[0]


def main(argv: list[str]|None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle",required=True,type=Path)
    parser.add_argument("--scenario",required=True,type=Path)
    parser.add_argument("--model",required=True)
    parser.add_argument("--ollama-tags",type=Path)
    parser.add_argument("--out",required=True,type=Path)
    args=parser.parse_args(argv)
    scenario=json.loads(args.scenario.read_text(encoding="utf-8"))
    digest=(
        _ollama_digest(args.ollama_tags,args.model)
        if args.ollama_tags else None
    )
    result=summarize_target_benchmark(
        args.bundle,scenario,model=args.model,model_digest=digest,
    )
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with args.out.open("x",encoding="utf-8") as handle:
        json.dump(result,handle,ensure_ascii=False,indent=2,allow_nan=False)
        handle.write("\n")
    print(
        "Synthetic target benchmark recorded:",
        result["message_count"],"responses;",
        result["review_case_count"],"pending human reviews;",
        "host p95 seconds:",
        result["host_response_seconds"]["p95_nearest_rank"],
        "| natural quality:",result["natural_quality_status"],
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
