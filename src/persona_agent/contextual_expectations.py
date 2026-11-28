"""Cognition's initial, deliberately narrow expectation producer.

Only explicit dated completion/non-completion reports about the same named
activity are comparable. No model confidence, generic trait or new inference.
A date is the user's reported occasion, not independently verified completion.
"""
import json
import re
from collections import defaultdict
from datetime import date,datetime,time,timedelta,timezone
from uuid import NAMESPACE_URL,uuid4,uuid5

from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation_contracts import StaleHistoricalContext
from .expectation_contracts import (
    ActivityReport,ExpectationPlan,ExpectationView,WithdrawExpectation,WithdrawalResult,
)
from .operation_contracts import require_idle
from .predictive_admission import capture_predictive_fence,require_predictive_fence
from .runtime import PermissionDenied,VersionConflict,IdempotencyConflict,_fingerprint

# Provisional pilot validity and storage budget, not calibrated maturity rules.
VALIDITY_DAYS=30
REVIEW_AT_SOURCES=64
_REPORT=re.compile(
    r"On (\d{4}-\d{2}-\d{2}), I (completed|did not complete) my "
    r"([a-z][a-z0-9 -]{1,63})\.",re.I|re.ASCII,
)


def parse_activity_report(turn_id,text,received_at):
    match=_REPORT.fullmatch(text.strip())
    if not match:
        return None
    try:
        occasion=date.fromisoformat(match[1])
    except ValueError:
        return None
    today=received_at.astimezone(timezone.utc).date()
    if not today-timedelta(days=VALIDITY_DAYS)<=occasion<=today:
        return None
    key=" ".join(match[3].lower().split())
    return ActivityReport(turn_id,key,occasion,match[2].lower()=="completed")


def assess_reports(reports):
    """Deduplicate by reported occasion and polarity; opposite reports
    about the same occasion are unresolved, not two independent outcomes.
    """
    days=defaultdict(set)
    for report in reports:
        days[report.occasion].add(report.completed)
    yes=sum(values=={True} for values in days.values())
    no=sum(values=={False} for values in days.values())
    conflicts=sum(len(values)>1 for values in days.values())
    if conflicts:
        return "CONFLICTED","UNKNOWN",yes,no,conflicts
    direction=("COMPLETION" if yes>no else "NON_COMPLETION" if no>yes else "MIXED")
    maturity="EARLY" if yes+no<2 else "TENTATIVE"
    return maturity,direction,yes,no,conflicts


def _latest(cur,user_id,instance_id,generation,key):
    cur.execute("""SELECT * FROM cognitive_expectation_revisions
                   WHERE user_id=%s AND instance_id=%s AND reset_generation=%s AND context_key=%s
                   ORDER BY version DESC LIMIT 1""",(user_id,instance_id,generation,key))
    return cur.fetchone()


def prepare_expectations(cur,user_id,instance_id,rows):
    """Bounded range preparation; no owner locks or model input dependency.

    Existing Memory episode patterns remain the authority for broader
    patterns. Their derived dependencies are not admitted by 0065 yet.
    This pilot pins only the raw reports it actually consumes.
    """
    grouped=defaultdict(list)
    for row in rows:
        if row["source_eligible"]:
            report=parse_activity_report(row["turn_id"],row["input_text"],row["created_at"])
            if report:
                grouped[report.context_key].append(report)
    if not grouped:
        return ()
    cur.execute("""SELECT version,reset_generation FROM predictive_learning_controls
                   WHERE user_id=%s AND instance_id=%s AND learning_enabled""",
                (user_id,instance_id))
    control=cur.fetchone()
    if control is None:
        return ()
    gen=control["reset_generation"]
    cur.execute("SELECT clock_timestamp() AS now")
    now=cur.fetchone()["now"]
    plans=[]
    for key,new in sorted(grouped.items()):
        prior=_latest(cur,user_id,instance_id,gen,key)
        version=prior["version"] if prior else 0
        if prior and prior["disposition"]!="HYPOTHESIS":
            continue
        reports=[]
        if prior:
            # Do not silently drop withdrawn or contrary prior evidence.
            # Expired/invalid hypotheses need owner review or reset.
            if prior["expires_at"]<=now:
                continue
            prior_fence=capture_predictive_fence(cur,user_id,instance_id,prior["source_turn_ids"])
            if prior_fence is None or prior_fence.control_version!=control["version"]:
                continue
            cur.execute("""SELECT turn_id,input_text,created_at FROM conversation_turns
                           WHERE user_id=%s AND instance_id=%s AND turn_id=ANY(%s::uuid[])""",
                        (user_id,instance_id,prior["source_turn_ids"]))
            reports=[parse_activity_report(r["turn_id"],r["input_text"],r["created_at"])
                     for r in cur.fetchall()]
            if any(r is None or r.context_key!=key for r in reports) or len(reports)!=len(prior["source_turn_ids"]):
                continue
        known={(r.occasion,r.completed) for r in reports}
        new_count=0
        for report in new:
            if (report.occasion,report.completed) in known:
                continue
            # The final union is fenced once; this exact check keeps
            # disabled/pre-reset raw reports out of the selected input set.
            cur.execute("SELECT predictive_sources_current(%s,%s,%s,%s::uuid[]) AS eligible",
                        (user_id,instance_id,gen,[report.turn_id]))
            if not cur.fetchone()["eligible"]:
                continue
            reports.append(report)
            known.add((report.occasion,report.completed))
            new_count+=1
        if not new_count:
            continue
        fence=capture_predictive_fence(cur,user_id,instance_id,tuple(r.turn_id for r in reports))
        if fence is None or fence.control_version!=control["version"] or fence.reset_generation!=gen:
            continue
        maturity,direction,yes,no,conflicts=assess_reports(reports)
        disposition="HYPOTHESIS"
        if len(reports)>=REVIEW_AT_SOURCES:
            disposition,maturity,direction="REVIEW_REQUIRED","UNAVAILABLE","UNKNOWN"
        # Oldest contributing occasion determines expiry: duplicate or new
        # reports cannot extend the validity of stale constituent evidence.
        expiry=datetime.combine(min(r.occasion for r in reports)+timedelta(days=VALIDITY_DAYS+1),
                                time.min,tzinfo=timezone.utc)
        if expiry<=now:
            continue
        plans.append(ExpectationPlan(key,version,fence,disposition,maturity,direction,
                                     yes,no,conflicts,expiry,tuple(reports)))
    return tuple(plans)


def stage_expectations(cur,work):
    """Stage Cognitive rows and event before either owner's Memory fence.

    Caller owns the optional savepoint and validates after pattern writes.
    Unique next version prevents two workers from both advancing a context.
    """
    if not work.expectation_plans:
        return
    from .conversation import _event
    admitted=[]
    for plan in work.expectation_plans:
        revision=uuid4()
        f=plan.fence
        cur.execute("""INSERT INTO cognitive_expectation_revisions(
            revision_id,user_id,instance_id,reset_generation,context_key,version,
            operation_id,control_version,disposition,maturity,direction,
            completion_occasions,non_completion_occasions,conflicting_occasions,
            source_turn_ids,occasion_evidence,expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT DO NOTHING RETURNING revision_id""",
            (revision,work.user_id,work.instance_id,f.reset_generation,plan.context_key,
             plan.expected_version+1,work.operation_id,f.control_version,plan.disposition,
             plan.maturity,plan.direction,plan.completion_occasions,plan.non_completion_occasions,
             plan.conflicting_occasions,list(f.source_turn_ids),json.dumps([
                 {"turn_id":str(report.turn_id),"context_key":report.context_key,
                  "occasion":report.occasion.isoformat(),"completed":report.completed}
                 for report in plan.reports]),plan.expires_at))
        if cur.fetchone() is None:
            raise StaleHistoricalContext("expectation version already advanced")
        admitted.append({"revision_id":str(revision),"version":plan.expected_version+1})
    _event(cur,user_id=work.user_id,instance_id=work.instance_id,
           conversation_id=work.conversation_id,event_type="cognition.expectations.revised.v1",
           source_id="cognition.reflection",trust_level="TRUSTED_PLATFORM",
           detail={"operation_id":str(work.operation_id),"revisions":admitted})


def require_expectation_plans(cur,work):
    """Late recheck, after optional Memory pattern writes and lock acquisition."""
    for plan in work.expectation_plans:
        require_predictive_fence(cur,work.user_id,work.instance_id,plan.fence)
        cur.execute("SELECT clock_timestamp()<%s AS current",(plan.expires_at,))
        if not cur.fetchone()["current"]:
            raise StaleHistoricalContext("expectation expired during preparation")


def _authorize(cur,actor,instance_id):
    if type(actor) is not TrustedActor or actor.application_session_id is not None:
        raise PermissionDenied("expectation inspection requires trusted local authenticated user")
    cur.execute("SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",(actor.user_id,instance_id))
    if cur.fetchone() is None:
        raise PermissionDenied("expectation outside authenticated scope")


def read_expectation(connection,actor,instance_id,context_key):
    """Exact-context diagnostic read, never automatically included in prompts.

    Re-enable only admits current-generation, unexpired, still-eligible
    latest records. Even a tentative record is not a forecast or verified fact.
    """
    require_idle(connection)
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _authorize(cur,actor,instance_id)
        cur.execute("""SELECT reset_generation FROM predictive_learning_controls
                       WHERE user_id=%s AND instance_id=%s AND learning_enabled""",
                    (actor.user_id,instance_id))
        control=cur.fetchone()
        if not control:
            return None
        row=_latest(cur,actor.user_id,instance_id,control["reset_generation"],context_key)
        if not row or row["disposition"]!="HYPOTHESIS":
            return None
        fence=capture_predictive_fence(cur,actor.user_id,instance_id,row["source_turn_ids"])
        if fence is None or fence.reset_generation!=row["reset_generation"]:
            return None
        try:
            require_predictive_fence(cur,actor.user_id,instance_id,fence)
        except StaleHistoricalContext:
            return None
        # Re-read after the fence in case a competing owner revision won.
        latest=_latest(cur,actor.user_id,instance_id,row["reset_generation"],context_key)
        cur.execute("SELECT clock_timestamp()<%s AS current",(row["expires_at"],))
        if latest["revision_id"]!=row["revision_id"] or not cur.fetchone()["current"]:
            return None
        return ExpectationView.model_validate({k:row[k] for k in ExpectationView.model_fields if k in row})


def inspect_expectation_history(connection,actor,instance_id,*,limit=20):
    """Metadata-only authorized history, including suppressed generations.
    No withdrawn source content or context keys exposed by this audit read.
    """
    require_idle(connection)
    if not 1<=limit<=100:
        raise ValueError("invalid inspection limit")
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _authorize(cur,actor,instance_id)
        cur.execute("""SELECT revision_id,reset_generation,version,disposition,created_at,expires_at
                       FROM cognitive_expectation_revisions WHERE user_id=%s AND instance_id=%s
                       ORDER BY created_at DESC,revision_id LIMIT %s""",(actor.user_id,instance_id,limit))
        return tuple(dict(r) for r in cur.fetchall())


def withdraw_expectation(connection,actor,command:WithdrawExpectation):
    """Append a scoped tombstone, even while disabled; no learned use enabled."""
    require_idle(connection)
    if type(command) is not WithdrawExpectation:
        raise TypeError("expected WithdrawExpectation")
    from .conversation import _selection
    kind="cognition.expectation.withdraw.v1"
    fingerprint=_fingerprint(command)
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _authorize(cur,actor,command.instance_id)
        _selection(cur,actor,command.instance_id,command.expected_generation)
        receipt=uuid4()
        cur.execute("""INSERT INTO command_receipts(receipt_id,user_id,instance_id,command_type,
                       idempotency_key,request_fingerprint) VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING RETURNING receipt_id""",
                    (receipt,actor.user_id,command.instance_id,kind,command.idempotency_key,fingerprint))
        if cur.fetchone() is None:
            cur.execute("""SELECT result_json,request_fingerprint FROM command_receipts
                           WHERE user_id=%s AND instance_id=%s AND command_type=%s AND idempotency_key=%s""",
                        (actor.user_id,command.instance_id,kind,command.idempotency_key))
            saved=cur.fetchone()
            if saved is None or saved["request_fingerprint"]!=fingerprint:
                raise IdempotencyConflict("expectation withdrawal key reused")
            return WithdrawalResult.model_validate(saved["result_json"]).model_copy(update={"replayed":True})
        cur.execute("""SELECT * FROM cognitive_expectation_revisions
                       WHERE revision_id=%s AND user_id=%s AND instance_id=%s""",
                    (command.revision_id,actor.user_id,command.instance_id))
        row=cur.fetchone()
        if row is None:
            raise PermissionDenied("expectation revision outside scope")
        latest=_latest(cur,actor.user_id,command.instance_id,row["reset_generation"],row["context_key"])
        if latest["revision_id"]!=row["revision_id"] or row["version"]!=command.expected_version:
            raise VersionConflict("expectation changed")
        if row["disposition"]=="WITHDRAWN":
            raise VersionConflict("expectation already withdrawn")
        cur.execute("""SELECT version FROM predictive_learning_controls WHERE user_id=%s AND instance_id=%s""",
                    (actor.user_id,command.instance_id))
        control=cur.fetchone()
        if control is None:
            raise VersionConflict("learning control unavailable")
        stream=uuid5(NAMESPACE_URL,f"persona-agent/{command.instance_id}/cognition.expectation")
        cur.execute("""INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
                       VALUES (%s,%s,%s,'cognition.expectation') ON CONFLICT DO NOTHING""",
                    (stream,actor.user_id,command.instance_id))
        cur.execute("""UPDATE event_streams SET last_sequence=last_sequence+1
                       WHERE stream_id=%s AND user_id=%s AND instance_id=%s RETURNING last_sequence""",
                    (stream,actor.user_id,command.instance_id))
        sequence=cur.fetchone()["last_sequence"]
        event_id,revision=uuid4(),uuid4()
        cur.execute("""INSERT INTO events(event_id,event_type,schema_version,user_id,instance_id,
                       stream_id,stream_sequence,source_id,trust_level,correlation_id,payload)
                       VALUES (%s,%s,1,%s,%s,%s,%s,%s,'AUTHENTICATED_USER',%s,%s::jsonb)""",
                    (event_id,kind,actor.user_id,command.instance_id,stream,sequence,actor.source_id,receipt,
                     json.dumps({"previous_revision_id":str(row["revision_id"]),"revision_id":str(revision)})))
        cur.execute("""INSERT INTO cognitive_expectation_revisions(revision_id,user_id,instance_id,
                       reset_generation,context_key,version,control_version,disposition,maturity,direction,
                       completion_occasions,non_completion_occasions,conflicting_occasions,source_turn_ids,source_event_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'WITHDRAWN','UNAVAILABLE','UNKNOWN',0,0,0,'{}',%s)
                       ON CONFLICT DO NOTHING RETURNING revision_id""",
                    (revision,actor.user_id,command.instance_id,row["reset_generation"],row["context_key"],
                     row["version"]+1,control["version"],event_id))
        if cur.fetchone() is None:
            raise VersionConflict("expectation concurrently revised")
        cur.execute("INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",(uuid4(),event_id))
        result=WithdrawalResult(revision_id=revision,version=row["version"]+1,event_id=event_id)
        cur.execute("UPDATE command_receipts SET result_json=%s::jsonb,event_id=%s WHERE receipt_id=%s",
                    (result.model_dump_json(),event_id,receipt))
        return result
