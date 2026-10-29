"""Durable E0 cases; Fleet owns execution leases, this store owns frozen scope."""

from contextlib import contextmanager
import json
import re
from pathlib import Path
import sqlite3
from uuid import uuid4

from .contracts import (CAPABILITIES, SCHEMA_VERSION, TERMINAL, ExplorationConflict,
                        ExplorationError, budget_contract, copied, digest, encoded,
                        now, original_authorization)
from .question import build_question
from .records import summarize_archive


class ExplorationStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "archives").mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / "exploration.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS exploration_runs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE, request_hash TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exploration_cases (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, job_id TEXT UNIQUE, data TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES exploration_runs(id)
                );
                CREATE INDEX IF NOT EXISTS exploration_cases_run ON exploration_cases(run_id);
            """)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def create_run(self, *, effective_set, drawings=None, original_user_input="", dynamic_methods="off",
                   tool_snapshot=None, model_snapshot=None, budget=None, idempotency_key=None, label=""):
        effective_set, drawings = copied(effective_set), copied(drawings or [])
        if not isinstance(effective_set, dict) or not effective_set.get("effective_set_id"):
            raise ExplorationError("EFFECTIVE_SET_REQUIRED")
        rules = effective_set.get("rules")
        if not isinstance(rules, list):
            raise ExplorationError("INVALID_EFFECTIVE_RULES")
        if len(drawings) > 9:
            raise ExplorationError("TOO_MANY_DRAWINGS", "E0 使用现有 Fleet 的最多 9 张图纸入口")
        for drawing in drawings:
            if not isinstance(drawing, dict) or not drawing.get("id") or not drawing.get("sha256"):
                raise ExplorationError("DRAWING_SOURCE_HASH_REQUIRED")
        if len({d["id"] for d in drawings}) != len(drawings):
            raise ExplorationError("DUPLICATE_DRAWINGS")
        active = [r for r in rules if isinstance(r, dict) and r.get("status", "active") != "disabled"]
        refs = [r.get("qualified_ref") for r in active]
        if any(not isinstance(ref, str) or not ref for ref in refs) or len(set(refs)) != len(refs):
            raise ExplorationError("RULE_REFERENCE_REQUIRED_OR_DUPLICATED")
        scope = {"effective_set": effective_set, "drawings": drawings,
                 "original_user_input": original_user_input,
                 "authorization": original_authorization(original_user_input, dynamic_methods),
                 "tool_snapshot": copied(tool_snapshot or {}), "model_snapshot": copied(model_snapshot or {}),
                 "budget": budget_contract(budget)}
        scope_hash = digest(scope)
        request_hash = digest({"scope_hash": scope_hash, "label": label})
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200):
            raise ExplorationError("INVALID_IDEMPOTENCY_KEY")
        with self.connection(True) as db:
            if idempotency_key:
                old = db.execute("SELECT id,request_hash FROM exploration_runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if old:
                    if old["request_hash"] != request_hash:
                        raise ExplorationConflict("IDEMPOTENCY_SCOPE_CHANGED", "幂等键已绑定另一份冻结输入")
                    return self._run(db, old["id"])
            ident, timestamp = "exp_" + uuid4().hex, now()
            value = {"schema_version": SCHEMA_VERSION, "run_id": ident, "id": ident,
                     "project_id": str(effective_set.get("project_id", "")), "label": str(label),
                     "created_at": timestamp, "scope": scope, "scope_hash": scope_hash,
                     "cancel_requested": False, "capabilities": copied(CAPABILITIES),
                     "control": {"revision":0,"applied_revision":0,"state":"running","desired_state":"running",
                                 "effective":{},"desired":{},"dispatch_budget":copied(scope['budget']),"commands":[]}}
            db.execute("INSERT INTO exploration_runs VALUES(?,?,?,?,?)",
                       (ident, value["project_id"], idempotency_key, request_hash, encoded(value)))
            for rule in active:
                case_id = "case_" + digest([ident, rule["qualified_ref"]])[:32]
                case = {"schema_version": SCHEMA_VERSION, "case_id": case_id, "id": case_id,
                        "run_id": ident, "rule_ref": rule["qualified_ref"], "rule": rule,
                        "rule_snapshot_hash": digest(rule), "scope_hash": scope_hash,
                        "status": "queued" if drawings else "waiting_input",
                        "readiness": "ready_to_start" if drawings else "awaiting_drawings",
                        "created_at": timestamp, "updated_at": timestamp, "job_id": None,
                        "dispatch_key": f"exploration:{ident}:{case_id}", "prepared": False,
                        "started_at": None, "finished_at": None, "cancel_requested": False,
                        "case_record": None, "attempt_records": {}, "archive_conflicts": [],
                        "dispatch_failures": [], "usage_by_attempt": {},
                        "usage": {"known": False, "total_tokens": None, "reported_tokens": 0},
                        "rule_completed": False, "rule_assessment_status": "not_evaluated"}
                db.execute("INSERT INTO exploration_cases VALUES(?,?,NULL,?)", (case_id, ident, encoded(case)))
            return self._run(db, ident)

    def _cases(self, db, run_id):
        return [json.loads(r[0]) for r in db.execute("SELECT data FROM exploration_cases WHERE run_id=? ORDER BY rowid", (run_id,))]

    def _run(self, db, run_id):
        row = db.execute("SELECT data FROM exploration_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        run = json.loads(row[0])
        cases = self._cases(db, run_id)
        states = [c["status"] for c in cases]
        started = [c for c in cases if c["started_at"]]
        active = [c for c in cases if c["status"] not in TERMINAL and (c["prepared"] or c["job_id"])]
        if run["cancel_requested"]:
            status = "cancelled"
        elif not cases or not run["scope"]["drawings"]:
            status = "waiting_input"
        elif any(s == "running" for s in states):
            status = "running"
        elif active:
            status = "queued"
        elif all(s in TERMINAL for s in states):
            status = states[0] if len(set(states)) == 1 else "partial"
        elif any(s in TERMINAL for s in states):
            status = "partial"
        else:
            status = "queued"
        unknown = sum(not c["usage"]["known"] for c in started)
        run.update(status=status, cases=cases, closed=run["cancel_requested"] or bool(cases) and all(s in TERMINAL for s in states),
                   readiness="awaiting_rules" if not cases else "awaiting_drawings" if not run["scope"]["drawings"] else "ready_to_start",
                   budget=run.get("control", {}).get("dispatch_budget", run["scope"]["budget"]),
                   budget_usage={"case_starts": len(started), "active_cases": len(active),
                                 "reported_tokens": sum(c["usage"].get("reported_tokens", 0) for c in started),
                                 "usage_unknown_cases": unknown, "usage_complete": unknown == 0},
                   coverage={"total_cases": len(cases), "terminal_cases": sum(s in TERMINAL for s in states),
                             "automatic_rule_completions": 0, "scope_complete": False,
                             "note": "按规则生成探索任务；E0 未枚举全部工程对象，也未完成规则自动判断"})
        return run

    def _case(self, db, run_id, case_id):
        row = db.execute("SELECT data FROM exploration_cases WHERE id=? AND run_id=?", (case_id, run_id)).fetchone()
        if not row:
            raise KeyError(case_id)
        return json.loads(row[0])

    def _save_case(self, db, case):
        case["updated_at"] = now()
        db.execute("UPDATE exploration_cases SET job_id=?,data=? WHERE id=?", (case["job_id"], encoded(case), case["case_id"]))

    def get_run(self, run_id):
        with self.connection() as db:
            return self._run(db, run_id)

    def list_runs(self, project_id=None):
        with self.connection() as db:
            rows = db.execute("SELECT id FROM exploration_runs" + (" WHERE project_id=?" if project_id is not None else "") + " ORDER BY rowid DESC",
                              (project_id,) if project_id is not None else ()).fetchall()
            return [self._run(db, row[0]) for row in rows]

    def get_case(self, run_id, case_id):
        with self.connection() as db:
            return self._case(db, run_id, case_id)

    def list_cases(self, run_id):
        return self.get_run(run_id)["cases"]

    def _check_start(self, db, run_id, case_id):
        run, case = self._run(db, run_id), self._case(db, run_id, case_id)
        if case["job_id"]:
            return run, case  # A repeated start returns the same existing job.
        if run["cancel_requested"] or case["status"] in TERMINAL:
            raise ExplorationConflict("CASE_CLOSED", "已结束的探索不能被覆盖；请创建新的运行")
        if run.get('control',{}).get('desired_state')=='paused':
            raise ExplorationConflict('EXPLORATION_PAUSED','探索已暂停，请先继续任务')
        if not run["scope"]["drawings"]:
            raise ExplorationConflict("AWAITING_DRAWINGS", "图纸尚未选择，请创建带图纸的新运行")
        build_question(run, case)  # Check complete input before reserving a slot.
        if case["prepared"]:
            return run, case
        limits, used = run["budget"], run["budget_usage"]
        if not case["started_at"] and used["case_starts"] >= limits["max_case_starts"]:
            raise ExplorationConflict("CASE_START_BUDGET_EXHAUSTED")
        if used["active_cases"] >= limits["max_concurrent_cases"]:
            raise ExplorationConflict("CONCURRENT_CASE_LIMIT")
        if limits["max_reported_tokens"] is not None:
            others = [c for c in run["cases"] if c["started_at"] and c["case_id"] != case_id]
            if any(c["status"] not in TERMINAL or not c["usage"]["known"] for c in others):
                raise ExplorationConflict("USAGE_UNKNOWN", "已有任务的 token 用量未知，不能按零放行下一任务")
            if used["reported_tokens"] >= limits["max_reported_tokens"]:
                raise ExplorationConflict("REPORTED_TOKEN_BUDGET_EXHAUSTED")
        return run, case

    def check_start(self, run_id, case_id):
        with self.connection() as db:
            return self._check_start(db, run_id, case_id)[1]

    def prepare_case(self, run_id, case_id, dispatch_payload=None):
        with self.connection(True) as db:
            _, case = self._check_start(db, run_id, case_id)
            if dispatch_payload is not None and not case["job_id"]:
                fingerprint = digest(dispatch_payload)
                if case.get("dispatch_payload_hash") not in (None, fingerprint):
                    raise ExplorationConflict("DISPATCH_INPUT_CHANGED", "本条探索的首次派发参数已冻结，不能并发改换执行节点或输入")
                case["dispatch_payload_hash"] = fingerprint
            if not case["job_id"] and not case["prepared"]:
                case.update(prepared=True, status="queued", started_at=case["started_at"] or now(), readiness="dispatch_prepared")
            if not case["job_id"]:
                self._save_case(db, case)
            return case

    def reconcile_job(self, run_id, case_id, job):
        """Recover enqueue-before-bind, including cancelled cases, without restart."""
        with self.connection(True) as db:
            case = self._case(db, run_id, case_id)
            job_id = job.get("job_id") or job.get("id")
            expected = {"idempotency_key": case["dispatch_key"], "scope_hash": case["scope_hash"],
                        "exploration_run_id": run_id, "exploration_case_id": case_id}
            if not job_id or any(job.get(key) != value for key, value in expected.items()) or not case["started_at"]:
                raise ExplorationConflict("DISPATCH_RECOVERY_SCOPE_MISMATCH")
            if case["job_id"] not in (None, job_id):
                raise ExplorationConflict("CASE_ALREADY_BOUND")
            if not case["job_id"]:
                case.update(job_id=job_id, prepared=False)
                self._save_case(db, case)
            return case

    def bind_job(self, run_id, case_id, job_id):
        if not isinstance(job_id, str) or not job_id:
            raise ExplorationError("JOB_ID_REQUIRED")
        with self.connection(True) as db:
            _, case = self._check_start(db, run_id, case_id)
            if case["job_id"]:
                if case["job_id"] != job_id:
                    raise ExplorationConflict("CASE_ALREADY_BOUND")
                return case
            existing = db.execute("SELECT id FROM exploration_cases WHERE job_id=?", (job_id,)).fetchone()
            if existing:
                raise ExplorationConflict("JOB_ALREADY_BOUND")
            case.update(job_id=job_id, prepared=False, started_at=case["started_at"] or now(),
                        status="queued", readiness="dispatched")
            self._save_case(db, case)
            return case

    def fail_dispatch(self, run_id, case_id, error, retryable=False, preserve_preparation=False):
        with self.connection(True) as db:
            case = self._case(db, run_id, case_id)
            if case["job_id"] or case["status"] in TERMINAL:
                return case
            case["dispatch_failures"].append({"at": now(), "error": copied(error), "retryable": bool(retryable)})
            case.update(prepared=bool(retryable and preserve_preparation and case["prepared"]), status="queued" if retryable else "failed",
                        readiness="dispatch_retryable" if retryable else "dispatch_failed",
                        finished_at=None if retryable else now())
            if not retryable:
                case["case_record"] = {"archive_ref": None, "stage": "dispatch",
                                       "failures": copied(case["dispatch_failures"]),
                                       "rule_completed": False, "rule_assessment_status": "not_evaluated"}
            self._save_case(db, case)
            return case

    def _archive_copy(self, archive):
        sha = digest(archive)
        target = self.root / "archives" / (sha + ".json")
        if not target.exists():
            import os
            temporary = target.with_suffix("." + uuid4().hex + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as file:
                    file.write(encoded(archive))
                    file.flush()
                    os.fsync(file.fileno())
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return str(target)

    def _summarize(self, run, case, archive, archive_ref):
        summary = summarize_archive(archive, archive_ref=archive_ref, drawings=run["scope"]["drawings"])
        task_payload = archive.get("task") or {}
        expected = {"scope_hash": run["scope_hash"], "exploration_run_id": run["run_id"],
                    "exploration_case_id": case["case_id"], "rule_snapshot_hash": case["rule_snapshot_hash"],
                    "upload_ids": [d["id"] for d in run["scope"]["drawings"]],
                    "tool_snapshot": run["scope"]["tool_snapshot"], "model_snapshot": run["scope"]["model_snapshot"],
                    "original_user_input": run["scope"]["original_user_input"],
                    "dynamic_methods": run["scope"]["authorization"]["mode"],
                    "authorization_source_sha256": run["scope"]["authorization"]["original_input_sha256"]}
        for key, value in expected.items():
            if not isinstance(task_payload, dict) or task_payload.get(key) != value:
                summary["validation_issues"].append({"code": "ARCHIVE_FROZEN_SCOPE_MISMATCH", "field": key})
        summary["local_archive_path"] = self._archive_copy(archive)
        return summary

    @staticmethod
    def _usage(case):
        usages = list(case["usage_by_attempt"].values())
        reported = sum(u["total_tokens"] for u in usages if u["known"])
        known = bool(usages) and all(u["known"] for u in usages)
        case["usage"] = {"known": known, "total_tokens": reported if known else None, "reported_tokens": reported}

    def record_attempt_archive(self, job_id, attempt, archive=None):
        """Import actual past Fleet attempts without changing current execution status."""
        attempt_id = attempt["id"]
        with self.connection(True) as db:
            row = db.execute("SELECT run_id,id FROM exploration_cases WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return None
            run, case = self._run(db, row["run_id"]), self._case(db, row["run_id"], row["id"])
            old = case["attempt_records"].get(attempt_id)
            if archive is not None:
                if archive.get("job_id") != job_id or archive.get("attempt_id") != attempt_id:
                    raise ExplorationConflict("ARCHIVE_ATTEMPT_MISMATCH")
                if old and old.get("archive_sha256"):
                    if old["archive_sha256"] != digest(archive):
                        raise ExplorationConflict("ATTEMPT_ARCHIVE_CONFLICT")
                    return case
                record = self._summarize(run, case, archive, attempt.get("archive_url"))
                case["attempt_records"][attempt_id] = record
                case["usage_by_attempt"][attempt_id] = record["usage"]
            elif old is None:
                case["attempt_records"][attempt_id] = {"attempt_id": attempt_id, "execution_status": attempt.get("status"),
                                                      "error": copied(attempt.get("error")), "archive_ref": None,
                                                      "source": "fleet_attempt_record"}
                case["usage_by_attempt"][attempt_id] = {"known": False, "total_tokens": None}
            self._usage(case)
            self._save_case(db, case)
            return case

    def record_job_result(self, job, archive=None, archive_ref=None):
        """Call with the current authoritative FleetStore.job, not a worker claim."""
        job_id = job.get("job_id") or job.get("id")
        with self.connection(True) as db:
            row = db.execute("SELECT run_id,id FROM exploration_cases WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return None
            run, case = self._run(db, row["run_id"]), self._case(db, row["run_id"], row["id"])
            was_terminal = case["status"] in TERMINAL
            if was_terminal and archive is None:
                return case
            summary = None
            if archive is not None:
                if not isinstance(archive, dict) or archive.get("job_id") != job_id:
                    raise ExplorationError("ARCHIVE_JOB_MISMATCH")
                if job.get("attempt_id") and archive.get("attempt_id") != job["attempt_id"]:
                    raise ExplorationConflict("ARCHIVE_ATTEMPT_MISMATCH")
                prior_hash = (case.get("case_record") or {}).get("archive_sha256")
                if was_terminal and prior_hash:
                    if digest(archive) != prior_hash:
                        conflict = {"archive_sha256": digest(archive), "archive_ref": archive_ref,
                                    "attempt_id": archive.get("attempt_id"), "code": "TERMINAL_ARCHIVE_CONFLICT",
                                    "local_archive_path": self._archive_copy(archive)}
                        if not any(c["archive_sha256"] == conflict["archive_sha256"] for c in case["archive_conflicts"]):
                            case["archive_conflicts"].append(conflict)
                            self._save_case(db, case)
                    return case
                summary = self._summarize(run, case, archive, archive_ref)
                if was_terminal:
                    # Late evidence enriches the record without rewriting the
                    # already-published cancellation/failure/partial decision.
                    summary["late_archive"] = True
                    summary["preserved_case_status"] = case["status"]
                    summary["prior_failures"] = copied((case.get("case_record") or {}).get("failures", []))
                case["case_record"] = summary
                case["attempt_records"][archive.get("attempt_id") or "unknown"] = summary
            attempt_id = job.get("attempt_id")
            if attempt_id:
                case["usage_by_attempt"].setdefault(attempt_id, {"known": False, "total_tokens": None})
                if summary is not None:
                    case["usage_by_attempt"][attempt_id] = summary["usage"]
            self._usage(case)
            if was_terminal:
                self._save_case(db, case)
                return case
            state = job.get("status")
            if state == "completed":
                valid = (summary is not None and summary["result_ref"] is not None
                         and not summary["validation_issues"] and bool(summary["fact_package"]["records"])
                         and job.get("delivery_status") == "completed"
                         and summary.get("execution_status") == "completed")
                case["status"] = "completed" if valid else "partial"
            elif state in ("failed", "cancelled", "partial"):
                case["status"] = state
            elif state in ("running", "cancel_requested"):
                case["status"] = "running"
            elif state == "queued":
                case["status"] = "queued"
            else:
                raise ExplorationError("INVALID_FLEET_STATUS")
            case.update(job_status=state, job_attempt_id=job.get("attempt_id"),
                        job_error=copied(job.get("error")), prepared=False,
                        rule_completed=False, rule_assessment_status="not_evaluated")
            if case["status"] in TERMINAL:
                case.update(finished_at=now(), readiness="exploration_ended")
                if summary is None:
                    case["case_record"] = {"archive_ref": None, "failures": [{"kind": "execution_or_archive", "error": copied(job.get("error")) or {"code": "ARCHIVE_UNAVAILABLE"}}],
                                           "rule_completed": False, "rule_assessment_status": "not_evaluated"}
            self._save_case(db, case)
            return case

    def cancel_case(self, run_id, case_id):
        with self.connection(True) as db:
            case = self._case(db, run_id, case_id)
            jobs = []
            if case["status"] not in TERMINAL:
                if case["job_id"]:
                    jobs.append(case["job_id"])
                case.update(status="cancelled", prepared=False, cancel_requested=True,
                            finished_at=now(), readiness="exploration_ended")
                self._save_case(db, case)
            return case | {"cancel_job_ids": jobs}

    def cancel_run(self, run_id):
        with self.connection(True) as db:
            run = self._run(db, run_id)
            if run["closed"]:
                return run | {"cancel_job_ids": []}
            jobs = []
            for case in run["cases"]:
                if case["status"] not in TERMINAL:
                    if case["job_id"]:
                        jobs.append(case["job_id"])
                    case.update(status="cancelled", prepared=False, cancel_requested=True,
                                finished_at=now(), readiness="exploration_ended")
                    self._save_case(db, case)
            row = db.execute("SELECT data FROM exploration_runs WHERE id=?", (run_id,)).fetchone()
            stored = json.loads(row[0])
            stored["cancel_requested"] = True
            db.execute("UPDATE exploration_runs SET data=? WHERE id=?", (encoded(stored), run_id))
            return self._run(db, run_id) | {"cancel_job_ids": jobs}


    def control_run(self, run_id, command_id, expected_revision, action, payload, source='human'):
        """Apply dispatch overlays atomically with case admission; retain frozen scope."""
        from cad_agent.fleet.operations import validate_command
        payload=validate_command(action,payload,exploration=True)
        if not isinstance(command_id,str) or not 1<=len(command_id)<=160 or not re.fullmatch(r'[A-Za-z0-9_.:-]+',command_id) or source not in {'human','supervisor'}:
            raise ExplorationError('INVALID_CONTROL_COMMAND')
        if source=='supervisor' and action!='guidance':
            raise ExplorationError('SUPERVISOR_GUIDANCE_ONLY')
        if isinstance(expected_revision,bool) or not isinstance(expected_revision,int) or expected_revision<0:
            raise ExplorationError('INVALID_CONTROL_REVISION')
        with self.connection(True) as db:
            run=self._run(db,run_id)
            stored=json.loads(db.execute('SELECT data FROM exploration_runs WHERE id=?',(run_id,)).fetchone()[0])
            control=stored.setdefault('control',{'revision':0,'applied_revision':0,'state':'running','desired_state':'running','effective':{},'desired':{},'dispatch_budget':copied(run['scope']['budget']),'commands':[]})
            old=next((c for c in control['commands'] if c['command_id']==command_id),None)
            if old:
                if (old['action'],old['payload'],old['source'])!=(action,payload,source):
                    raise ExplorationConflict('CONTROL_COMMAND_CONFLICT')
                return copied(control)
            if control['revision']!=expected_revision:
                raise ExplorationConflict('CONTROL_REVISION_CONFLICT','控制版本已改变，请刷新后重试')
            if run.get('closed'):
                raise ExplorationConflict('EXPLORATION_CLOSED')
            revision=control['revision']+1
            command={'command_id':command_id,'revision':revision,'action':action,'payload':payload,'source':source,
                     'status':'applied','requested_at':now(),'applied_at':now(),'boundary':'before_case_dispatch',
                     'before':copied({'desired_state':control['desired_state'],'effective':control['effective'],'dispatch_budget':control['dispatch_budget']}),
                     'fanout':'reconciled_idempotently_to_each_active_job'}
            if action in {'pause','resume'}:
                control['desired_state']='paused' if action=='pause' else 'running'
                control['state']=control['desired_state']
            elif action=='configure':
                if 'dispatch_budget' in payload:
                    control['dispatch_budget']=budget_contract(control['dispatch_budget']|payload['dispatch_budget'])
                for section,values in payload.items():
                    if section!='dispatch_budget':control.setdefault('desired',{}).setdefault(section,{}).update(values)
                # Runtime overlay is desired, not yet asserted as applied by child agents.
            if action=='guidance' or (action=='configure' and any(k!='dispatch_budget' for k in payload)):
                command['status']='pending'
                command.pop('applied_at',None)
                command['scope_applied_at']=now()
            command['after']=copied({'desired_state':control['desired_state'],'desired':control.get('desired',{}),'dispatch_budget':control['dispatch_budget']})
            control['revision']=revision
            if command['status']=='applied':control['applied_revision']=revision
            control['commands'].append(command)
            db.execute('UPDATE exploration_runs SET data=? WHERE id=?',(encoded(stored),run_id))
            return copied(control)


    def save_control_projection(self, run_id, control):
        """Persist observed child receipts without overwriting a concurrent command."""
        with self.connection(True) as db:
            row=db.execute('SELECT data FROM exploration_runs WHERE id=?',(run_id,)).fetchone()
            if not row:raise KeyError(run_id)
            stored=json.loads(row[0]);current=stored.get('control',{})
            if current.get('revision',0)!=control.get('revision',0):return
            projected={c['command_id']:c for c in control.get('commands',[])}
            for command in current.get('commands',[]):
                value=projected.get(command['command_id'])
                if value:
                    for key in ('status','targets','boundary'):
                        if key in value:command[key]=copied(value[key])
                    if value['status']=='applied':
                        command.setdefault('applied_at',now())
                    else:command.pop('applied_at',None)
            current['state']=control['state']
            current['applied_revision']=control.get('applied_revision',0)
            db.execute('UPDATE exploration_runs SET data=? WHERE id=?',(encoded(stored),run_id))
