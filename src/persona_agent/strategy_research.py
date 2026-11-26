"""MVP-06D: authorized saved-source research and Goal-owned strategy comparison.

Only local Knowledge owner reads are performed. No arbitrary URLs, provider
calls, inference, operation admission or automatic strategy selection.
"""
from dataclasses import dataclass
from uuid import UUID

from psycopg.rows import dict_row

from .conversation import _require_actor
from .goal_contracts import ResearchGoalStrategiesCommand,StrategySourcePin
from .goal_state import _goal,_read_selected,_selected,_source,_require_goal_approval
from .goal_decomposition import _authorized_lineage
from .knowledge_contracts import KnowledgePassage
from .knowledge_memory import select_knowledge
from .operation_contracts import require_idle
from .runtime import PermissionDenied,VersionConflict

MAX_SOURCES=4
MAX_STRATEGIES=12
MAX_RUNS_PER_STRATEGY=12


@dataclass(frozen=True)
class StrategyResearchResult:
    goal_id:UUID
    goal_version:int
    status:str
    sources:tuple[KnowledgePassage,...]
    independently_verified:bool=False


def _current_approval(cur,actor,command,source=None):
    """New research actions require evidence and approval in this generation."""
    source=source or _source(cur,actor,command)
    _require_goal_approval(cur,actor,command,source)
    cur.execute(
        """SELECT count(*) AS n FROM conversation_turns
           WHERE turn_id=ANY(%s) AND conversation_id=%s AND user_id=%s
             AND instance_id=%s AND selection_generation=%s""",
        ([command.evidence_turn_id,command.approval_turn_id],command.conversation_id,
         actor.user_id,command.instance_id,command.expected_generation),
    )
    if cur.fetchone()["n"]!=2:
        raise PermissionDenied("research approval belongs to another selection generation")


def _search(cur,actor,command):
    return select_knowledge(cur,user_id=actor.user_id,instance_id=command.instance_id,
        query=command.query,max_results=MAX_SOURCES,max_chars=4800)


def research_goal_strategies(connection,actor,command:ResearchGoalStrategiesCommand):
    """Read-only discovery: eligible supplied sources, not validated advice.

    Exact separate approval binds goal/version/query. This read is live, not
    a durable receipt or a grant to fetch external resources. Repeated reads
    always recheck current eligibility.
    """
    require_idle(connection)
    _require_actor(actor)
    if type(command) is not ResearchGoalStrategiesCommand:
        raise TypeError("validated research command required")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,command.instance_id)
            _selected(cur,actor,command)
            _current_approval(cur,actor,command)
            goal=_goal(cur,actor,command.instance_id,command.goal_id,lock="SHARE")
            _authorized_lineage(cur,actor,command.instance_id,goal)
            if goal["version"]!=command.expected_goal_version or goal["status"]!="ACTIVE":
                raise VersionConflict("research goal changed or inactive")
            cur.execute(
                """WITH RECURSIVE chain AS (
                       SELECT goal_id,parent_goal_id,status FROM goal_records
                       WHERE goal_id=%s AND user_id=%s AND instance_id=%s
                       UNION ALL
                       SELECT g.goal_id,g.parent_goal_id,g.status FROM goal_records g
                       JOIN chain c ON g.goal_id=c.parent_goal_id
                       WHERE g.user_id=%s AND g.instance_id=%s
                   ) SELECT status FROM chain""",
                (goal["goal_id"],actor.user_id,command.instance_id,actor.user_id,command.instance_id),
            )
            if any(row["status"]!="ACTIVE" for row in cur.fetchall()):
                raise VersionConflict("research goal ancestor inactive")
            hits=_search(cur,actor,command)
            return StrategyResearchResult(goal["goal_id"],goal["version"],
                "SOURCES_AVAILABLE" if hits else "NO_ELIGIBLE_SOURCE",hits)


def source_pin(hit):
    return StrategySourcePin(document_id=hit.document_id,version=hit.version,
        position=hit.position,content_sha256=hit.content_sha256)


def _resolve_pins(cur,actor,instance_id,query,pins,*,lock=False):
    """Use exactly the Knowledge owner's current eligibility and integrity rules."""
    if not pins:
        return None
    if lock:
        # Fence a concurrent withdrawal/revision through the short Goal
        # transaction. Lock in deterministic order; no external call.
        ids=sorted({pin.document_id for pin in pins})
        cur.execute(
            """SELECT document_id FROM memory_knowledge_documents
               WHERE document_id=ANY(%s) AND user_id=%s AND instance_id=%s
               ORDER BY document_id FOR SHARE""",
            (ids,actor.user_id,instance_id),
        )
        if len(cur.fetchall())!=len(ids):
            return None
    hits=[]
    for pin in pins:
        rows=select_knowledge(cur,user_id=actor.user_id,instance_id=instance_id,
            query=query,max_results=1,max_chars=2400,document_id=pin.document_id,
            pinned_version=pin.version,pinned_position=pin.position)
        if len(rows)!=1 or source_pin(rows[0])!=pin:
            return None
        hits.append(rows[0])
    return tuple(hits)


def validate_research_candidate(cur,actor,goal,command):
    _authorized_lineage(cur,actor,command.instance_id,goal)
    request=command.research
    if (request.instance_id!=command.instance_id
            or request.expected_generation!=command.expected_generation
            or request.goal_id!=command.goal_id
            or request.expected_goal_version!=command.expected_goal_version):
        raise PermissionDenied("research approval outside strategy scope")
    if goal["version"]!=command.expected_goal_version:
        raise VersionConflict("research goal version changed")
    _current_approval(cur,actor,command)
    _current_approval(cur,actor,command.research)
    # Research approval covers the exact search; proposal approval covers
    # the user's proposed approach, limitations and selected citations.
    if _resolve_pins(cur,actor,command.instance_id,command.research.query,
                     command.sources,lock=True) is None:
        raise VersionConflict("research source unavailable, changed or ineligible")


def save_research_links(cur,actor,command,event):
    cur.execute(
        """INSERT INTO goal_strategy_research(
               strategy_id,goal_id,user_id,instance_id,goal_version,query,limitations,
               research_conversation_id,research_turn_id,research_approval_turn_id,
               proposal_conversation_id,proposal_approval_turn_id,event_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (command.strategy_id,command.goal_id,actor.user_id,command.instance_id,
         command.expected_goal_version,command.research.query,command.limitations,
         command.research.conversation_id,command.research.evidence_turn_id,
         command.research.approval_turn_id,command.conversation_id,command.approval_turn_id,event),
    )
    cur.executemany(
        """INSERT INTO goal_strategy_research_sources(
               strategy_id,goal_id,user_id,instance_id,ordinal,document_id,
               version,position,content_sha256
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        [(command.strategy_id,command.goal_id,actor.user_id,command.instance_id,i,
          p.document_id,p.version,p.position,p.content_sha256)
         for i,p in enumerate(command.sources,1)],
    )


def research_record(cur,actor,instance_id,goal_id,strategy_id):
    cur.execute(
        """SELECT * FROM goal_strategy_research
           WHERE strategy_id=%s AND goal_id=%s AND user_id=%s AND instance_id=%s""",
        (strategy_id,goal_id,actor.user_id,instance_id),
    )
    return cur.fetchone()


def _pins(cur,record):
    cur.execute(
        """SELECT document_id,version,position,content_sha256
           FROM goal_strategy_research_sources
           WHERE strategy_id=%s AND goal_id=%s AND user_id=%s AND instance_id=%s
           ORDER BY ordinal""",
        (record["strategy_id"],record["goal_id"],record["user_id"],record["instance_id"]),
    )
    return tuple(StrategySourcePin.model_validate(row) for row in cur.fetchall())


def validate_research_selection(cur,actor,command,record):
    # A user can choose an alternative, but a withdrawn citation cannot
    # silently authorize the research-backed selection. Prior selections
    # remain historical facts; no automatic deselection or goal edit.
    _current_approval(cur,actor,command)
    if _resolve_pins(cur,actor,command.instance_id,record["query"],
                     _pins(cur,record),lock=True) is None:
        raise VersionConflict("research source unavailable; review strategy before selection")


@dataclass(frozen=True)
class StrategyRunObservation:
    run_id:UUID
    variant_id:UUID
    variant_revision:int
    template_id:UUID
    template_version:int
    evaluation_id:UUID|None
    outcome:str
    failure_cause:str|None


@dataclass(frozen=True)
class StrategyComparisonItem:
    strategy_id:UUID
    title:str
    approach:str
    suitability_note:str
    selected:bool
    limitations:str|None
    source_status:str
    sources:tuple[KnowledgePassage,...]
    observations:tuple[StrategyRunObservation,...]
    observations_truncated:bool
    independently_verified_effectiveness:bool=False


@dataclass(frozen=True)
class StrategyComparison:
    goal_id:UUID
    goal_version:int
    desired_outcome:str
    success_criterion:str
    strategies:tuple[StrategyComparisonItem,...]
    truncated:bool


def _observations(cur,actor,instance_id,goal_id,strategy_id):
    # Each run belongs to one pinned private variant and revision.
    # Superseded assessments do not count twice; stale assessment versions
    # are not evidence of present success/failure.
    cur.execute(
        """SELECT r.run_id,r.variant_id,r.variant_revision,r.template_id,r.template_version,
                  e.evaluation_id,
                  CASE WHEN e.evaluation_id IS NULL THEN 'UNEVALUATED'
                       WHEN e.run_version<>r.version
                         OR NOT workflow_evaluation_current(r.user_id,r.instance_id,e.evaluation_id)
                         OR NOT workflow_run_current(r.user_id,r.instance_id,r.run_id) THEN 'STALE'
                       ELSE e.outcome END AS outcome,
                  CASE WHEN e.run_version=r.version
                       AND workflow_evaluation_current(r.user_id,r.instance_id,e.evaluation_id)
                       AND workflow_run_current(r.user_id,r.instance_id,r.run_id)
                       THEN e.failure_cause ELSE NULL END AS failure_cause
           FROM personalized_procedures p
           JOIN procedure_runs r ON r.variant_id=p.variant_id
             AND r.user_id=p.user_id AND r.instance_id=p.instance_id
           LEFT JOIN LATERAL (
               SELECT evaluation_id,run_version,outcome,failure_cause
               FROM procedure_run_evaluations e
               WHERE e.run_id=r.run_id AND e.user_id=r.user_id AND e.instance_id=r.instance_id
               ORDER BY e.evaluation_revision DESC LIMIT 1
           ) e ON true
           WHERE p.user_id=%s AND p.instance_id=%s AND p.goal_id=%s AND p.strategy_id=%s
           ORDER BY r.created_at DESC,r.run_id LIMIT %s""",
        (actor.user_id,instance_id,goal_id,strategy_id,MAX_RUNS_PER_STRATEGY+1),
    )
    rows=cur.fetchall()
    return (tuple(StrategyRunObservation(**row) for row in rows[:MAX_RUNS_PER_STRATEGY]),
            len(rows)>MAX_RUNS_PER_STRATEGY)


def compare_goal_strategies(connection,actor,instance_id,goal_id):
    """Scoped bounded alternatives, source eligibility and personal observations.

    No ranking, estimated causal success rate, model recommendation or goal
    mutation. User-approved adoption and actual reported results are distinct.
    """
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            goal=_goal(cur,actor,instance_id,goal_id,lock="SHARE")
            cur.execute(
                """SELECT strategy_id FROM goal_strategy_selections
                   WHERE goal_id=%s AND user_id=%s AND instance_id=%s
                   ORDER BY goal_version DESC LIMIT 1""",(goal_id,actor.user_id,instance_id),
            )
            selection=cur.fetchone()
            selected=selection["strategy_id"] if selection else None
            cur.execute(
                """SELECT strategy_id,title,approach,suitability_note FROM goal_strategies
                   WHERE goal_id=%s AND user_id=%s AND instance_id=%s
                   ORDER BY (strategy_id=%s::uuid) DESC NULLS LAST,created_at DESC,strategy_id
                   LIMIT %s""",(goal_id,actor.user_id,instance_id,selected,MAX_STRATEGIES+1),
            )
            rows=cur.fetchall()
            items=[]
            for row in rows[:MAX_STRATEGIES]:
                record=research_record(cur,actor,instance_id,goal_id,row["strategy_id"])
                sources=(_resolve_pins(cur,actor,instance_id,record["query"],_pins(cur,record))
                         if record else None)
                observations,truncated=_observations(cur,actor,instance_id,goal_id,row["strategy_id"])
                items.append(StrategyComparisonItem(**row,
                    selected=row["strategy_id"]==selected,
                    limitations=record["limitations"] if record else None,
                    source_status=("CURRENT_SUPPLIED_SOURCE" if sources else
                                   "SOURCE_UNAVAILABLE" if record else "UNSOURCED"),
                    sources=sources or (),observations=observations,
                    observations_truncated=truncated))
            return StrategyComparison(goal_id,goal["version"],goal["desired_outcome"],
                goal["success_criterion"],tuple(items),len(rows)>MAX_STRATEGIES)
