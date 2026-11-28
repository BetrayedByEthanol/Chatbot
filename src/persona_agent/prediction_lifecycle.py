"""Cognitive prediction owner: explicit dated activity pilot.

Ingress pins a visible forecast and prior expectation, not a result. Only the
leased Cognitive owner can persist predictions, comparisons and resolution.
No model-visible context, appraisal, instructions or tool authority.
"""
import json
import re
from datetime import date,datetime,time,timedelta,timezone
from uuid import NAMESPACE_URL,uuid4,uuid5

from psycopg.rows import dict_row

from .contextual_expectations import parse_activity_report,_authorize,_latest
from .conversation_contracts import StaleHistoricalContext
from .operation_contracts import require_idle
from .prediction_contracts import (
    ComparisonRead,ForecastPlan,ComparisonPlan,ResolutionPlan,PredictionBatch,PredictionView,
    WithdrawPrediction,PredictionWithdrawalResult,RecordedComparison,
)
from .predictive_admission import capture_predictive_fence,require_predictive_fence
from .runtime import PermissionDenied,VersionConflict,IdempotencyConflict,_fingerprint

MAX_HORIZON_DAYS=7
MAX_OUTCOME_REPORTS=16
_PLAN=re.compile(r"On (\d{4}-\d{2}-\d{2}), I plan to complete my ([a-z][a-z0-9 -]{1,63})\.",re.I|re.ASCII)
_AMBIGUOUS=re.compile(r"^(?:I (?:finished|completed|did it|did not|couldn't|could not|managed|failed)\b|Actually\b|On \d{4}-\d{2}-\d{2}, I\b)",re.I)


def _key(text):
    return " ".join(text.lower().split())


def _plan(text,at):
    match=_PLAN.fullmatch(text.strip())
    if not match:
        return None
    try:
        occasion=date.fromisoformat(match[1])
    except ValueError:
        return None
    today=at.astimezone(timezone.utc).date()
    if not today<=occasion<=today+timedelta(days=MAX_HORIZON_DAYS):
        return None
    return _key(match[2]),occasion


def _horizon(occasion):
    return datetime.combine(occasion+timedelta(days=1),time.min,tzinfo=timezone.utc)


def comparison_route(turn_id,text,at):
    """No DB lookup for irrelevant or unsupported/ambiguous language."""
    if len(text)<=120 and parse_activity_report(turn_id,text,at) is not None:
        return "COMPARE_NOW"
    if _AMBIGUOUS.match(text.strip()):
        return "DEFER"
    return "SKIP"


def stamp_report(cur,user_id,instance_id,event_id,report,received_at):
    """One exact input-admission insert, no lifecycle or Memory writes.

    A row selected here is already visible to the ingress statement.
    Report pins are immutable, including NULL pins: delayed processing
    cannot retroactively associate an earlier outcome with a forecast.
    The latest expectation is selected before checking its age/disposition;
    there is never fallback to an older version.
    """
    cur.execute("""WITH c AS MATERIALIZED (
          SELECT * FROM predictive_learning_controls
          WHERE user_id=%s AND instance_id=%s AND learning_enabled
        )
        INSERT INTO predictive_input_admissions(
          turn_id,user_id,instance_id,source_event_id,admitted_control_version,reset_generation,
          reported_context,reported_occasion,reported_completion,reported_at,
          comparison_expectation_id,prediction_id)
        SELECT %s,c.user_id,c.instance_id,%s,c.version,c.reset_generation,
               %s,%s,%s,%s,
               CASE WHEN e.created_at<%s THEN e.revision_id END,p.prediction_id
        FROM c
        LEFT JOIN LATERAL (
          SELECT revision_id,created_at FROM cognitive_expectation_revisions e
          WHERE e.user_id=c.user_id AND e.instance_id=c.instance_id
            AND e.reset_generation=c.reset_generation AND e.context_key=%s
          ORDER BY version DESC LIMIT 1
        ) e ON TRUE
        LEFT JOIN LATERAL (
          SELECT prediction_id FROM cognitive_predictions p
          WHERE p.user_id=c.user_id AND p.instance_id=c.instance_id
            AND p.reset_generation=c.reset_generation AND p.control_version=c.version
            AND p.context_key=%s AND p.target_occasion=%s
            AND p.created_at<%s AND p.expires_at>%s
        ) p ON TRUE""",
        (user_id,instance_id,report.turn_id,event_id,report.context_key,report.occasion,
         report.completed,received_at,received_at,report.context_key,
         report.context_key,report.occasion,received_at,received_at))


def _control(cur,user_id,instance_id):
    cur.execute("""SELECT version,reset_generation FROM predictive_learning_controls
                   WHERE user_id=%s AND instance_id=%s AND learning_enabled""",
                (user_id,instance_id))
    return cur.fetchone()


def _now(cur):
    cur.execute("SELECT clock_timestamp() AS now")
    return cur.fetchone()["now"]


def _expectation(cur,user_id,instance_id,revision_id):
    cur.execute("""SELECT * FROM cognitive_expectation_revisions
                   WHERE revision_id=%s AND user_id=%s AND instance_id=%s""",
                (revision_id,user_id,instance_id))
    return cur.fetchone()


def _latest_id(cur,e,*,ignore_operation=None):
    # During the same atomic outcome+expectation revision commit, compare
    # to the preexisting revision, not the revision derived from this outcome.
    cur.execute("""SELECT revision_id FROM cognitive_expectation_revisions
                   WHERE user_id=%s AND instance_id=%s AND reset_generation=%s AND context_key=%s
                     AND (%s::uuid IS NULL OR operation_id IS DISTINCT FROM %s::uuid)
                   ORDER BY version DESC LIMIT 1""",
                (e["user_id"],e["instance_id"],e["reset_generation"],e["context_key"],
                 ignore_operation,ignore_operation))
    row=cur.fetchone()
    return row["revision_id"] if row else None


def _usable(e,control,now):
    # Conservative pilot: a pre-pause expectation needs a fresh owner
    # revision before new forecasting/comparison. No disabled catch-up.
    return (e is not None and control is not None
            and e["reset_generation"]==control["reset_generation"]
            and e["control_version"]==control["version"]
            and e["disposition"]=="HYPOTHESIS"
            and e["expires_at"] is not None and e["expires_at"]>now)


def _eligible_latest(cur,latest,control,now):
    if not _usable(latest,control,now):
        return False
    cur.execute("SELECT predictive_sources_current(%s,%s,%s,%s::uuid[]) AS eligible",
                (latest["user_id"],latest["instance_id"],latest["reset_generation"],latest["source_turn_ids"]))
    return cur.fetchone()["eligible"]


def _fence(cur,user_id,instance_id,control,ids):
    ids=tuple(dict.fromkeys(ids))
    if not 1<=len(ids)<=128:
        return None
    f=capture_predictive_fence(cur,user_id,instance_id,ids)
    if f is None or f.control_version!=control["version"] or f.reset_generation!=control["reset_generation"]:
        return None
    return f


def _admission(cur,user_id,instance_id,report,at):
    cur.execute("""SELECT * FROM predictive_input_admissions
                   WHERE turn_id=%s AND user_id=%s AND instance_id=%s
                     AND reported_context=%s AND reported_occasion=%s
                     AND reported_completion=%s AND reported_at=%s""",
                (report.turn_id,user_id,instance_id,report.context_key,report.occasion,
                 report.completed,at))
    return cur.fetchone()


def _report_exists(cur,user_id,instance_id,generation,key,occasion,*,unbound_to=None,before_at=None):
    cur.execute("""SELECT 1 FROM predictive_input_admissions
                   WHERE user_id=%s AND instance_id=%s AND reset_generation=%s
                     AND reported_context=%s AND reported_occasion=%s
                     AND (%s::uuid IS NULL OR prediction_id IS DISTINCT FROM %s::uuid)
                     AND (%s::timestamptz IS NULL OR reported_at<%s::timestamptz)
                   LIMIT 1""",
                (user_id,instance_id,generation,key,occasion,unbound_to,unbound_to,before_at,before_at))
    return cur.fetchone() is not None


def _opposite_report(cur,user_id,instance_id,control,report,at):
    # Only an earlier exact report can contradict the current one. Unknown
    # eligibility of the selected opposite abstains rather than hiding it.
    cur.execute("""SELECT turn_id FROM predictive_input_admissions
                   WHERE user_id=%s AND instance_id=%s AND reset_generation=%s
                     AND reported_context=%s AND reported_occasion=%s
                     AND reported_completion<>%s
                     AND (reported_at,turn_id)<(%s,%s)
                   ORDER BY reported_at DESC,turn_id DESC LIMIT 1""",
                (user_id,instance_id,control["reset_generation"],report.context_key,
                 report.occasion,report.completed,at,report.turn_id))
    row=cur.fetchone()
    return row["turn_id"] if row else None


def _earlier_duplicate(cur,user_id,instance_id,control,report,at):
    cur.execute("""SELECT 1 FROM predictive_input_admissions
                   WHERE user_id=%s AND instance_id=%s AND reset_generation=%s
                     AND reported_context=%s AND reported_occasion=%s
                     AND reported_completion=%s
                     AND (reported_at,turn_id)<(%s,%s) LIMIT 1""",
                (user_id,instance_id,control["reset_generation"],report.context_key,
                 report.occasion,report.completed,at,report.turn_id))
    return cur.fetchone() is not None


def _unknown(reason):
    return ComparisonRead("COMPARE_NOW","UNKNOWN",reason)


def _compare(cur,user_id,instance_id,report,at,admission,control,now,*,ignore_operation=None):
    if (admission is None or control is None
            or admission["admitted_control_version"]!=control["version"]
            or admission["reset_generation"]!=control["reset_generation"]):
        return _unknown("CONTROL_OR_SOURCE_UNAVAILABLE")
    ref=admission["comparison_expectation_id"]
    if ref is None:
        return _unknown("NO_PREEXISTING_EXPECTATION")
    e=_expectation(cur,user_id,instance_id,ref)
    if (not _usable(e,control,now) or e["context_key"]!=report.context_key
            or e["created_at"]>=at or _latest_id(cur,e,ignore_operation=ignore_operation)!=ref):
        return _unknown("EXPECTATION_UNAVAILABLE")
    # Deduplicate the same reported occasion, including repeats across
    # sessions before the ordinary expectation revision has been written.
    previous={v["completed"] for v in e["occasion_evidence"]
              if v["occasion"]==report.occasion.isoformat()}
    if report.completed in previous or _earlier_duplicate(cur,user_id,instance_id,control,report,at):
        return _unknown("DUPLICATE_OCCASION")
    opposite=_opposite_report(cur,user_id,instance_id,control,report,at)
    ids=[*e["source_turn_ids"],report.turn_id]
    if opposite is not None:
        ids.append(opposite)
    f=_fence(cur,user_id,instance_id,control,ids)
    if f is None:
        return _unknown("SOURCE_UNAVAILABLE")
    if opposite is not None or (not report.completed) in previous:
        label="CONTRADICTORY"
    elif e["maturity"]!="TENTATIVE" or e["direction"] not in ("COMPLETION","NON_COMPLETION"):
        return _unknown("INSUFFICIENT_EXPECTATION")
    else:
        label="EXPECTED" if report.completed==(e["direction"]=="COMPLETION") else "UNEXPECTED"
    return ComparisonRead("COMPARE_NOW",label,"COMPARABLE_USER_REPORT",
                          ref,report.turn_id,f,e["expires_at"])


def compare_incoming_report(cur,user_id,instance_id,turn_id,text,at):
    """Immediate host-only diagnostic; no prompt/output dependency yet."""
    route=comparison_route(turn_id,text,at)
    if route!="COMPARE_NOW":
        return ComparisonRead(route,reason="AMBIGUOUS" if route=="DEFER" else "IRRELEVANT")
    report=parse_activity_report(turn_id,text,at)
    admission=_admission(cur,user_id,instance_id,report,at)
    if admission is None:
        return _unknown("SOURCE_NOT_ADMITTED")
    control=_control(cur,user_id,instance_id)
    if control is None:
        return _unknown("CONTROL_DISABLED")
    result=_compare(cur,user_id,instance_id,report,at,admission,control,_now(cur))
    if result.fence is None:
        return result
    try:
        require_predictive_fence(cur,user_id,instance_id,result.fence)
        e=_expectation(cur,user_id,instance_id,result.expectation_revision_id)
        if not _usable(e,control,_now(cur)) or _latest_id(cur,e)!=result.expectation_revision_id:
            return _unknown("EXPECTATION_CHANGED")
    except StaleHistoricalContext:
        return _unknown("SOURCE_CHANGED")
    return result


def _prediction(cur,user_id,instance_id,prediction_id):
    cur.execute("""SELECT * FROM cognitive_predictions
                   WHERE prediction_id=%s AND user_id=%s AND instance_id=%s""",
                (prediction_id,user_id,instance_id))
    return cur.fetchone()


def _resolution(cur,user_id,instance_id,prediction_id):
    cur.execute("""SELECT * FROM cognitive_prediction_resolutions
                   WHERE prediction_id=%s AND user_id=%s AND instance_id=%s""",
                (prediction_id,user_id,instance_id))
    return cur.fetchone()


def _withdrawn(cur,user_id,instance_id,prediction_id):
    cur.execute("""SELECT 1 FROM cognitive_prediction_withdrawals
                   WHERE prediction_id=%s AND user_id=%s AND instance_id=%s""",
                (prediction_id,user_id,instance_id))
    return cur.fetchone() is not None


def _bound_reports(cur,p):
    cur.execute("""SELECT turn_id,reported_completion FROM predictive_input_admissions
                   WHERE prediction_id=%s AND user_id=%s AND instance_id=%s
                     AND reset_generation=%s AND admitted_control_version=%s
                     AND reported_context=%s AND reported_occasion=%s
                     AND reported_at>=%s AND reported_at<%s
                   ORDER BY reported_at,turn_id LIMIT %s""",
                (p["prediction_id"],p["user_id"],p["instance_id"],p["reset_generation"],
                 p["control_version"],p["context_key"],p["target_occasion"],
                 p["created_at"],p["expires_at"],MAX_OUTCOME_REPORTS+1))
    return cur.fetchall()


def _prediction_matches_expectation(p,e):
    return (p["expectation_revision_id"]==e["revision_id"]
            and p["expectation_version"]==e["version"]
            and p["context_key"]==e["context_key"]
            and p["reset_generation"]==e["reset_generation"]
            and p["control_version"]==e["control_version"]
            and e["maturity"]=="TENTATIVE"
            and e["direction"] in ("COMPLETION","NON_COMPLETION")
            and p["expected_completion"]==(e["direction"]=="COMPLETION")
            and p["expires_at"]<=e["expires_at"]
            and set(p["source_turn_ids"])==set(e["source_turn_ids"])|{p["commitment_turn_id"]})


def _prepare_forecast(cur,user_id,instance_id,row,key,occasion,control,now):
    e=_latest(cur,user_id,instance_id,control["reset_generation"],key)
    if (not _usable(e,control,now) or e["created_at"]>=row["created_at"]
            or e["maturity"]!="TENTATIVE" or e["direction"] not in ("COMPLETION","NON_COMPLETION")):
        return None
    expires=_horizon(occasion)
    if expires<=now or expires>e["expires_at"]:
        return None
    if any(v["occasion"]==occasion.isoformat() for v in e["occasion_evidence"]):
        return None
    # An already-reported target cannot become a forecast. This also
    # prevents plans/outcomes processed in the same delayed range.
    if _report_exists(cur,user_id,instance_id,control["reset_generation"],key,occasion):
        return None
    cur.execute("""SELECT 1 FROM cognitive_predictions
                   WHERE user_id=%s AND instance_id=%s AND reset_generation=%s
                     AND context_key=%s AND target_occasion=%s""",
                (user_id,instance_id,control["reset_generation"],key,occasion))
    if cur.fetchone() is not None:
        return None
    f=_fence(cur,user_id,instance_id,control,(*e["source_turn_ids"],row["turn_id"]))
    if f is None:
        return None
    # Commitment and all sources must belong to this enabled generation.
    # An older commitment from before a pause cannot mint a new forecast.
    cur.execute("""SELECT 1 FROM predictive_input_admissions WHERE turn_id=%s
                   AND user_id=%s AND instance_id=%s AND admitted_control_version=%s""",
                (row["turn_id"],user_id,instance_id,control["version"]))
    if cur.fetchone() is None:
        return None
    return ForecastPlan(e["revision_id"],e["version"],row["turn_id"],key,occasion,
                        e["direction"]=="COMPLETION",expires,f)


def _prepare_resolution(cur,user_id,instance_id,pid,control,now):
    p=_prediction(cur,user_id,instance_id,pid)
    if (p is None or p["control_version"]!=control["version"]
            or p["reset_generation"]!=control["reset_generation"] or p["expires_at"]<=now):
        return None
    if (_resolution(cur,user_id,instance_id,pid) is not None
            or _withdrawn(cur,user_id,instance_id,pid)):
        return None
    e=_expectation(cur,user_id,instance_id,p["expectation_revision_id"])
    if (not _usable(e,control,now) or not _prediction_matches_expectation(p,e)
            or _latest_id(cur,e)!=e["revision_id"]):
        return None
    # A concurrent report that did not see this forecast is not retroactive
    # evidence. Even a later duplicate cannot repair that causal gap.
    if _report_exists(cur,user_id,instance_id,p["reset_generation"],p["context_key"],
                      p["target_occasion"],unbound_to=pid):
        return None
    outcomes=_bound_reports(cur,p)
    if not 1<=len(outcomes)<=MAX_OUTCOME_REPORTS:
        return None
    ids=tuple(r["turn_id"] for r in outcomes)
    f=_fence(cur,user_id,instance_id,control,(*p["source_turn_ids"],*ids))
    if f is None:
        return None
    polarities={r["reported_completion"] for r in outcomes}
    if len(polarities)>1:
        label,comparison="INCONCLUSIVE","CONTRADICTORY"
    elif p["expected_completion"] in polarities:
        label,comparison="HIT","EXPECTED"
    else:
        label,comparison="MISS","UNEXPECTED"
    return ResolutionPlan(pid,e["revision_id"],ids,label,comparison,p["expires_at"],f)


def prepare_prediction_batch(cur,user_id,instance_id,rows,expectation_plans):
    """Bounded existing Cognitive range; no full user-model or unrelated lookup."""
    inputs=[]
    for row in rows:
        if not row["source_eligible"] or len(row["input_text"])>120:
            continue
        report=parse_activity_report(row["turn_id"],row["input_text"],row["created_at"])
        target=_plan(row["input_text"],row["created_at"])
        if report is not None or target is not None:
            inputs.append((row,report,target))
    if not inputs:
        return PredictionBatch()
    control=_control(cur,user_id,instance_id)
    if control is None:
        return PredictionBatch()
    now=_now(cur)
    forecasts,comparisons,resolutions=[],[],[]
    seen_targets,seen_comparisons,seen_predictions=set(),set(),set()
    changing={p.context_key for p in expectation_plans}
    for row,report,target in inputs:
        if target is not None and target not in seen_targets and target[0] not in changing:
            prepared=_prepare_forecast(cur,user_id,instance_id,row,*target,control,now)
            if prepared is not None:
                forecasts.append(prepared)
                seen_targets.add(target)
        if report is None:
            continue
        admission=_admission(cur,user_id,instance_id,report,row["created_at"])
        read=_compare(cur,user_id,instance_id,report,row["created_at"],admission,control,now)
        if read.fence is not None:
            key=(read.expectation_revision_id,report.occasion,report.completed)
            if key not in seen_comparisons:
                comparisons.append(ComparisonPlan(read,report.occasion,report.completed))
                seen_comparisons.add(key)
        if admission is None or admission["prediction_id"] is None:
            continue
        pid=admission["prediction_id"]
        if pid not in seen_predictions:
            seen_predictions.add(pid)
            prepared=_prepare_resolution(cur,user_id,instance_id,pid,control,now)
            if prepared is not None and report.turn_id in prepared.outcome_turn_ids:
                resolutions.append(prepared)
    return PredictionBatch(tuple(forecasts),tuple(comparisons),tuple(resolutions))


def stage_prediction_batch(cur,work):
    """Stage rows and event before late dependency/owner locks."""
    batch=work.prediction_batch
    admitted={"predictions":[],"comparisons":[],"resolutions":[]}
    for p in sorted(batch.forecasts,key=lambda item:(item.context_key,item.occasion)):
        pid=uuid4()
        cur.execute("""WITH stamp AS MATERIALIZED (SELECT clock_timestamp() AS now)
              INSERT INTO cognitive_predictions(
              prediction_id,user_id,instance_id,reset_generation,control_version,
              expectation_revision_id,expectation_version,operation_id,commitment_turn_id,
              context_key,target_occasion,expected_completion,source_turn_ids,expires_at,created_at)
              SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,stamp.now
              FROM stamp WHERE stamp.now<%s
              ON CONFLICT DO NOTHING RETURNING prediction_id""",
            (pid,work.user_id,work.instance_id,p.fence.reset_generation,p.fence.control_version,
             p.expectation_revision_id,p.expectation_version,work.operation_id,p.commitment_turn_id,
             p.context_key,p.occasion,p.expected_completion,list(p.fence.source_turn_ids),
             p.expires_at,p.expires_at))
        if cur.fetchone() is not None:
            admitted["predictions"].append(str(pid))
    for p in sorted(batch.comparisons,key=lambda item:(item.read.expectation_revision_id,item.occasion,item.completed)):
        r=p.read
        cid=uuid4()
        cur.execute("""INSERT INTO cognitive_expectation_comparisons(
              comparison_id,user_id,instance_id,expectation_revision_id,operation_id,
              control_version,reset_generation,outcome_turn_id,occasion,completed,
              classification,source_turn_ids)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT DO NOTHING RETURNING comparison_id""",
            (cid,work.user_id,work.instance_id,r.expectation_revision_id,work.operation_id,
             r.fence.control_version,r.fence.reset_generation,r.outcome_turn_id,p.occasion,
             p.completed,r.classification,list(r.fence.source_turn_ids)))
        if cur.fetchone() is not None:
            admitted["comparisons"].append(str(cid))
    for p in sorted(batch.resolutions,key=lambda item:item.prediction_id):
        cur.execute("""INSERT INTO cognitive_prediction_resolutions(
              prediction_id,user_id,instance_id,operation_id,control_version,
              outcome_turn_ids,resolution,comparison)
              SELECT %s,%s,%s,%s,%s,%s,%s,%s WHERE clock_timestamp()<%s
              ON CONFLICT DO NOTHING RETURNING prediction_id""",
            (p.prediction_id,work.user_id,work.instance_id,work.operation_id,p.fence.control_version,
             list(p.outcome_turn_ids),p.resolution,p.comparison,p.expires_at))
        if cur.fetchone() is not None:
            admitted["resolutions"].append(str(p.prediction_id))
    if any(admitted.values()):
        from .conversation import _event
        _event(cur,user_id=work.user_id,instance_id=work.instance_id,
               conversation_id=work.conversation_id,event_type="cognition.prediction.lifecycle.v1",
               source_id="cognition.reflection",trust_level="TRUSTED_PLATFORM",
               detail={"operation_id":str(work.operation_id),**admitted})


def lock_learning_owner(cur,user_id,instance_id):
    """Final scoped owner-write gate: Memory then Governance, no upgrade.

    All Cognitive/Memory/event writes are staged before this gate. The
    Memory pattern owner may already hold its exclusive revision lock.
    NO KEY UPDATE serializes learned writers/withdrawal, while remaining
    compatible with ingress foreign-key KEY SHARE.
    """
    cur.execute("""SELECT revision FROM historical_recall_revisions
                   WHERE user_id=%s AND instance_id=%s FOR SHARE""",(user_id,instance_id))
    if cur.fetchone() is None:
        raise StaleHistoricalContext("predictive source revision unavailable")
    cur.execute("""SELECT version FROM predictive_learning_controls
                   WHERE user_id=%s AND instance_id=%s FOR NO KEY UPDATE""",
                (user_id,instance_id))
    if cur.fetchone() is None:
        raise StaleHistoricalContext("predictive learning control unavailable")


def _require_prior(cur,work,revision_id,fence,expires):
    require_predictive_fence(cur,work.user_id,work.instance_id,fence)
    e=_expectation(cur,work.user_id,work.instance_id,revision_id)
    control={"version":fence.control_version,"reset_generation":fence.reset_generation}
    now=_now(cur)
    if (not _usable(e,control,now) or expires<=now
            or _latest_id(cur,e,ignore_operation=work.operation_id)!=revision_id):
        raise StaleHistoricalContext("prior expectation changed or expired")
    return e


def require_prediction_batch(cur,work):
    """Late exact prior revision, sources, current clock, report set."""
    for p in work.prediction_batch.forecasts:
        _require_prior(cur,work,p.expectation_revision_id,p.fence,p.expires_at)
        if _report_exists(cur,work.user_id,work.instance_id,p.fence.reset_generation,
                          p.context_key,p.occasion):
            raise StaleHistoricalContext("forecast target already reported")
    for p in work.prediction_batch.comparisons:
        r=p.read
        _require_prior(cur,work,r.expectation_revision_id,r.fence,r.expires_at)
        # Another session may have admitted an earlier duplicate or
        # counterexample while this range was prepared.
        row=next((x for x in work.turns if x["turn_id"]==r.outcome_turn_id),None)
        if row is None:
            raise StaleHistoricalContext("comparison source not in cognitive range")
        report=parse_activity_report(row["turn_id"],row["input_text"],row["created_at"])
        if report is None:
            raise StaleHistoricalContext("comparison source no longer supported")
        admission=_admission(cur,work.user_id,work.instance_id,report,row["created_at"])
        control={"version":r.fence.control_version,"reset_generation":r.fence.reset_generation}
        current=_compare(cur,work.user_id,work.instance_id,report,row["created_at"],
                         admission,control,_now(cur),ignore_operation=work.operation_id)
        if (current.classification!=r.classification or current.fence is None
                or current.expectation_revision_id!=r.expectation_revision_id
                or current.fence.source_turn_ids!=r.fence.source_turn_ids):
            raise StaleHistoricalContext("comparison evidence changed")
    for p in work.prediction_batch.resolutions:
        e=_require_prior(cur,work,p.expectation_revision_id,p.fence,p.expires_at)
        prediction=_prediction(cur,work.user_id,work.instance_id,p.prediction_id)
        if (prediction is None or not _prediction_matches_expectation(prediction,e)
                or _withdrawn(cur,work.user_id,work.instance_id,p.prediction_id)
                or _report_exists(cur,work.user_id,work.instance_id,p.fence.reset_generation,
                    prediction["context_key"],prediction["target_occasion"],unbound_to=p.prediction_id)
                or tuple(r["turn_id"] for r in _bound_reports(cur,prediction))!=p.outcome_turn_ids):
            raise StaleHistoricalContext("prediction outcome binding changed")


def read_prediction(connection,actor,instance_id,prediction_id):
    """Authenticated exact diagnostic, not authority to use a forecast.

    Pending requires latest applicable expectation. A resolved historical
    comparison may be inspected after an ordinary expectation revision,
    but never after control changes, withdrawal, expiry of source validity
    or a new contrary report. Horizon-only expiry is terminal INCONCLUSIVE
    from the durable deadline; no synthetic outcome, penalty or timer job.
    """
    require_idle(connection)
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _authorize(cur,actor,instance_id)
        p=_prediction(cur,actor.user_id,instance_id,prediction_id)
        if p is None or _withdrawn(cur,actor.user_id,instance_id,prediction_id):
            return None
        control=_control(cur,actor.user_id,instance_id)
        e=_expectation(cur,actor.user_id,instance_id,p["expectation_revision_id"])
        now=_now(cur)
        if not _usable(e,control,now) or not _prediction_matches_expectation(p,e):
            return None
        r=_resolution(cur,actor.user_id,instance_id,prediction_id)
        if r and r["control_version"]!=p["control_version"]:
            return None
        ids=[*p["source_turn_ids"],*(r["outcome_turn_ids"] if r else ())]
        f=_fence(cur,actor.user_id,instance_id,control,ids)
        if f is None:
            return None
        try:
            require_predictive_fence(cur,actor.user_id,instance_id,f)
        except StaleHistoricalContext:
            return None
        latest=_latest(cur,actor.user_id,instance_id,p["reset_generation"],p["context_key"])
        now=_now(cur)
        if (not _usable(e,control,now) or not _eligible_latest(cur,latest,control,now)
                or _withdrawn(cur,actor.user_id,instance_id,prediction_id)):
            return None
        if _resolution(cur,actor.user_id,instance_id,prediction_id)!=r:
            return None  # concurrent commit; next inspection may read it
        if _report_exists(cur,actor.user_id,instance_id,p["reset_generation"],p["context_key"],
                          p["target_occasion"],unbound_to=prediction_id,before_at=p["expires_at"]):
            return None
        state="PENDING"
        resolution=comparison=None
        outcomes=()
        if r:
            bound=_bound_reports(cur,p)
            if len(bound)>MAX_OUTCOME_REPORTS:
                return None
            current_ids={b["turn_id"] for b in bound}
            if not set(r["outcome_turn_ids"]).issubset(current_ids):
                return None
            if r["resolution"] in ("HIT","MISS"):
                observed=p["expected_completion"] if r["resolution"]=="HIT" else not p["expected_completion"]
                # Even a late report outside the original horizon can
                # contradict the recorded outcome; duplicate polarity
                # outside the horizon does not create another resolution.
                cur.execute("""SELECT 1 FROM predictive_input_admissions
                               WHERE user_id=%s AND instance_id=%s AND reset_generation=%s
                                 AND reported_context=%s AND reported_occasion=%s
                                 AND reported_completion<>%s LIMIT 1""",
                            (actor.user_id,instance_id,p["reset_generation"],p["context_key"],
                             p["target_occasion"],observed))
                if cur.fetchone() is not None:
                    return None  # later conflicting report invalidates certainty
            # Superseded forecast is historical only. Never reuse as a
            # current forecast; source withdrawal still suppresses labels.
            state="RESOLVED" if latest["revision_id"]==e["revision_id"] else "HISTORICAL"
            resolution,comparison=r["resolution"],r["comparison"]
            outcomes=tuple(r["outcome_turn_ids"])
        elif latest["revision_id"]!=e["revision_id"]:
            return None
        elif p["expires_at"]<=now:
            state,resolution,comparison="EXPIRED","INCONCLUSIVE","UNKNOWN"
        return PredictionView(prediction_id=prediction_id,expectation_revision_id=e["revision_id"],
            expectation_version=e["version"],context_key=p["context_key"],target_occasion=p["target_occasion"],
            expected_completion=p["expected_completion"],created_at=p["created_at"],expires_at=p["expires_at"],
            state=state,resolution=resolution,comparison=comparison,outcome_turn_ids=outcomes)


def inspect_prediction_history(connection,actor,instance_id,*,limit=20):
    """Metadata-only history, including suppressed generations; no learned text."""
    require_idle(connection)
    if not 1<=limit<=100:
        raise ValueError("invalid inspection limit")
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _authorize(cur,actor,instance_id)
        cur.execute("""WITH candidates AS MATERIALIZED (
                         SELECT * FROM cognitive_predictions WHERE user_id=%s AND instance_id=%s
                         ORDER BY created_at DESC,prediction_id LIMIT %s)
                       SELECT p.prediction_id,p.expectation_revision_id,p.created_at,p.expires_at,
                         CASE WHEN w.prediction_id IS NOT NULL THEN 'WITHDRAWN'
                              WHEN r.prediction_id IS NOT NULL THEN 'RESOLUTION_RECORDED'
                              WHEN p.expires_at<=clock_timestamp() THEN 'EXPIRED'
                              ELSE 'OPEN_RECORDED' END AS historical_status,
                         CASE WHEN w.prediction_id IS NOT NULL THEN 'WITHDRAWN'
                              WHEN c.reset_generation IS DISTINCT FROM p.reset_generation THEN 'RESET'
                              WHEN NOT COALESCE(c.learning_enabled,false) THEN 'DISABLED'
                              WHEN c.version<>p.control_version THEN 'CONTROL_CHANGED'
                              ELSE 'REQUIRES_ELIGIBILITY_CHECK' END AS governance_status
                       FROM candidates p LEFT JOIN cognitive_prediction_resolutions r
                         ON r.prediction_id=p.prediction_id AND r.user_id=p.user_id AND r.instance_id=p.instance_id
                       LEFT JOIN cognitive_prediction_withdrawals w
                         ON w.prediction_id=p.prediction_id AND w.user_id=p.user_id AND w.instance_id=p.instance_id
                       LEFT JOIN predictive_learning_controls c
                         ON c.user_id=p.user_id AND c.instance_id=p.instance_id
                       ORDER BY p.created_at DESC,p.prediction_id""",(actor.user_id,instance_id,limit))
        return tuple(dict(r) for r in cur.fetchall())



def withdraw_prediction(connection,actor,command:WithdrawPrediction):
    """Exact immutable target; receipt replay does not withdraw twice.

    May suppress a historical prediction while learning is disabled.
    No change to the expectation or to independent user reports.
    """
    require_idle(connection)
    if type(command) is not WithdrawPrediction:
        raise TypeError("expected WithdrawPrediction")
    from .conversation import _selection
    kind="cognition.prediction.withdraw.v1"
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
                raise IdempotencyConflict("prediction withdrawal key reused")
            return PredictionWithdrawalResult.model_validate(saved["result_json"]).model_copy(
                update={"replayed":True})
        if _prediction(cur,actor.user_id,command.instance_id,command.prediction_id) is None:
            raise PermissionDenied("prediction outside authenticated scope")
        stream=uuid5(NAMESPACE_URL,f"persona-agent/{command.instance_id}/cognition.prediction")
        cur.execute("""INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
                       VALUES (%s,%s,%s,'cognition.prediction') ON CONFLICT DO NOTHING""",
                    (stream,actor.user_id,command.instance_id))
        cur.execute("""UPDATE event_streams SET last_sequence=last_sequence+1
                       WHERE stream_id=%s AND user_id=%s AND instance_id=%s RETURNING last_sequence""",
                    (stream,actor.user_id,command.instance_id))
        seq=cur.fetchone()["last_sequence"]
        event_id=uuid4()
        cur.execute("""INSERT INTO events(event_id,event_type,schema_version,user_id,instance_id,
                       stream_id,stream_sequence,source_id,trust_level,correlation_id,payload)
                       VALUES (%s,%s,1,%s,%s,%s,%s,%s,'AUTHENTICATED_USER',%s,%s::jsonb)""",
                    (event_id,kind,actor.user_id,command.instance_id,stream,seq,actor.source_id,receipt,
                     json.dumps({"prediction_id":str(command.prediction_id)})))
        cur.execute("""INSERT INTO cognitive_prediction_withdrawals(
                       prediction_id,user_id,instance_id,source_event_id) VALUES (%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING RETURNING prediction_id""",
                    (command.prediction_id,actor.user_id,command.instance_id,event_id))
        if cur.fetchone() is None:
            raise VersionConflict("prediction already withdrawn")
        cur.execute("INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",(uuid4(),event_id))
        cur.execute("""SELECT version FROM predictive_learning_controls
                       WHERE user_id=%s AND instance_id=%s FOR NO KEY UPDATE""",
                    (actor.user_id,command.instance_id))
        if cur.fetchone() is None:
            raise VersionConflict("learning control unavailable")
        result=PredictionWithdrawalResult(prediction_id=command.prediction_id,event_id=event_id)
        cur.execute("UPDATE command_receipts SET result_json=%s::jsonb,event_id=%s WHERE receipt_id=%s",
                    (result.model_dump_json(),event_id,receipt))
        return result


def read_recorded_comparison(connection,actor,instance_id,comparison_id):
    """Scoped historical diagnostic; not an appraisal execution grant.

    A normal later expectation revision makes this historical, never a
    current forecast. New contrary evidence or withdrawn dependencies
    suppress old certainty. No suppressed content returned.
    """
    require_idle(connection)
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _authorize(cur,actor,instance_id)
        cur.execute("""SELECT * FROM cognitive_expectation_comparisons
                       WHERE comparison_id=%s AND user_id=%s AND instance_id=%s""",
                    (comparison_id,actor.user_id,instance_id))
        r=cur.fetchone()
        if r is None:
            return None
        c=_control(cur,actor.user_id,instance_id)
        e=_expectation(cur,actor.user_id,instance_id,r["expectation_revision_id"])
        if (not _usable(e,c,_now(cur)) or r["control_version"]!=c["version"]
                or r["reset_generation"]!=c["reset_generation"]):
            return None
        f=_fence(cur,actor.user_id,instance_id,c,r["source_turn_ids"])
        if f is None:
            return None
        try:
            require_predictive_fence(cur,actor.user_id,instance_id,f)
        except StaleHistoricalContext:
            return None
        latest=_latest(cur,actor.user_id,instance_id,e["reset_generation"],e["context_key"])
        now=_now(cur)
        if not _usable(e,c,now) or not _eligible_latest(cur,latest,c,now):
            return None
        if r["classification"]!="CONTRADICTORY":
            cur.execute("""SELECT 1 FROM predictive_input_admissions
                           WHERE user_id=%s AND instance_id=%s AND reset_generation=%s
                             AND reported_context=%s AND reported_occasion=%s
                             AND reported_completion<>%s LIMIT 1""",
                        (actor.user_id,instance_id,e["reset_generation"],e["context_key"],
                         r["occasion"],r["completed"]))
            if cur.fetchone() is not None:
                return None
        return RecordedComparison(comparison_id=comparison_id,expectation_revision_id=e["revision_id"],
            outcome_turn_id=r["outcome_turn_id"],classification=r["classification"],
            historical=latest["revision_id"]!=e["revision_id"],source_turn_ids=tuple(r["source_turn_ids"]))
