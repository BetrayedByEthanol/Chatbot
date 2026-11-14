#!/usr/bin/env python3
"""Live MVP-01B observational probe: no synthetic intent/goal writes."""
import argparse
import json
import os
from pathlib import Path

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.model_gateway import OllamaChatGateway


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn",default=os.getenv("ACCEPTANCE_DATABASE_URL"))
    parser.add_argument("--model",default=os.getenv("ACCEPTANCE_MODEL","qwen2.5:0.5b"))
    parser.add_argument("--ollama-url",default=os.getenv("ACCEPTANCE_OLLAMA_URL","http://127.0.0.1:11434"))
    parser.add_argument("--report",type=Path)
    args=parser.parse_args(argv)
    if not args.dsn:
        parser.error("--dsn or ACCEPTANCE_DATABASE_URL is required")
    identity=seed_local_demo(args.dsn)
    gateway=OllamaChatGateway(
        model=args.model,base_url=args.ollama_url,timeout_seconds=180,
    )
    host=BehavioralAcceptanceHarness(args.dsn,identity,gateway)
    host.begin_session("implicit-intent")
    messages=(
        "Lately I've been thinking about making daily walks part of my routine.",
        "That sounds manageable.",
    )
    turns=[]
    for index,message in enumerate(messages):
        turns.append(host.send(message,idempotency_key=f"mvp01b-{index}"))
    signals=host.observe_intent_signals()
    scoped=host.observe_state()
    report={
        "checkpoint":"MVP-01B",
        "model":args.model,
        "turns_sent":len(messages),
        "delivered_responses":scoped["delivered_responses"],
        "model_proposals":[s for s in signals if s["epistemic_status"]=="MODEL_PROPOSED"],
        "natural_goal_approval":"MISSING" if scoped["goals"]==0 else "REVIEW",
        "reply_lengths":[len(turn.text) for turn in turns],
    }
    if args.report:
        args.report.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({
        "checkpoint":report["checkpoint"],
        "turns_sent":report["turns_sent"],
        "delivered_responses":report["delivered_responses"],
        "model_proposals":len(report["model_proposals"]),
        "natural_goal_approval":report["natural_goal_approval"],
        "reply_lengths":report["reply_lengths"],
    },indent=2))
    # Semantic recognition quality remains observational, not a fabricated
    # pass assertion on a tiny probabilistic model.
    return 0 if scoped["delivered_responses"]==len(messages) else 1


if __name__=="__main__":
    raise SystemExit(main())
