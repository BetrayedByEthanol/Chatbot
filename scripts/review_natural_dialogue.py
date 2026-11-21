#!/usr/bin/env python3
"""Build a synthetic review packet or reconcile independent human ratings.

No network, model evaluator, production state or hidden prompt inspection.
Outputs are private review artifacts: never use real-user evaluation bundles.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from persona_agent.natural_dialogue_review import (
    packet_from_bundle,rating_template,
    summarize_independent_reviews,validate_packet,
)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def publish(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("x",encoding="utf-8") as stream:
        json.dump(content,stream,indent=2,ensure_ascii=False,allow_nan=False)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    cmds=parser.add_subparsers(dest="action",required=True)
    export=cmds.add_parser("packet",help="extract synthetic blind review material")
    export.add_argument("--bundle",type=Path,required=True)
    export.add_argument("--out",type=Path,required=True)
    template=cmds.add_parser("template",help="create one empty reviewer scorecard")
    template.add_argument("--packet",type=Path,required=True)
    template.add_argument("--reviewer",required=True)
    template.add_argument("--out",type=Path,required=True)
    aggregate=cmds.add_parser("aggregate",help="compare two independent reviews")
    aggregate.add_argument("--packet",type=Path,required=True)
    aggregate.add_argument("--first",type=Path,required=True)
    aggregate.add_argument("--second",type=Path,required=True)
    aggregate.add_argument("--out",type=Path,required=True)
    args=parser.parse_args(argv)
    if args.action=="packet":
        result=packet_from_bundle(args.bundle)
    else:
        packet=validate_packet(load(args.packet))
        if args.action=="template":
            result=rating_template(packet,args.reviewer)
        else:
            result=summarize_independent_reviews(
                packet,load(args.first),load(args.second),
            )
    publish(args.out,result)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
