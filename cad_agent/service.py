from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from cad_agent.execution import BoundedExecutor, ExecutionManager, ExecutionContext, ExecutionCancelled
from hashlib import sha256
from pathlib import Path
import json
import threading
import shutil
from tempfile import TemporaryDirectory
import time
from uuid import uuid4
from copy import copy
from datetime import datetime, timezone

from cad_agent.analysis.query import answer_facts
from cad_agent.config import Settings
from cad_agent.contracts import SourceRegion, ExtractionRequest, Diagnostic, stable_id
from cad_agent.cad.reader import read_drawing, PARSER_VERSION
from cad_agent.cad.regions import build_regions
from cad_agent.building.masonry import (
    extract,
    EXTRACTOR_VERSION,
    EXTRACTOR_REVISION,
)
from cad_agent.adapters.model import ModelGateway, ModelError, PROMPT_VERSION
from cad_agent.adapters.store import Store, now
from cad_agent.adapters.oda import OdaConverter, ConversionError


def _parse_drawing(path, snapshot_id):
    """Spawned worker: no workspace, connection, credentials or model client."""
    texts, scene, rectangles, segments, diagnostics = read_drawing(path, snapshot_id)
    regions, frames = build_regions(texts, scene, rectangles, segments, snapshot_id)
    return texts, scene, rectangles, segments, diagnostics, regions, frames


class Workspace:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.gateway = ModelGateway(settings)
        self.model_catalog = settings.model_catalog()
        from cad_agent.runtimes.subscription import CodexAccount
        self.codex_account = CodexAccount(settings.data_dir)
        from cad_agent.adapters.siwc import SiwcAccount
        self.siwc_account = SiwcAccount(settings.data_dir, settings.siwc_max_concurrency)
        self.execution = ExecutionManager(self.store)
        from cad_agent.geometry.compute import GeometryExecutor
        self.geometry_executor = GeometryExecutor()
        self.parse_executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        self.executor = BoundedExecutor(
            max_workers=2, thread_name_prefix="cad-extract"
        )
        self.agent_executor = BoundedExecutor(
            max_workers=2, thread_name_prefix="cad-chat"
        )
        self.lock = threading.RLock()
        # Shared by shallow model views, including caches first built on a view.
        self._derived_caches = {}
        self.import_lock = threading.Lock()
        self.converter = OdaConverter(settings)

    def for_model(self, profile):
        if profile is None or profile == self.settings.model_profile:
            return self
        selected = self.model_catalog[profile]
        view = copy(self)
        view.settings = selected
        view.gateway = ModelGateway(selected)
        return view

    def close(self):
        self.agent_executor.shutdown(wait=True, cancel_futures=True)
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.geometry_executor.close()
        self.parse_executor.shutdown(wait=True, cancel_futures=True)
        self.execution.close()
        self.codex_account.close()
        self.siwc_account.close()

    def ingest(self, filename, content, synthetic=False):
        # Serialize imports to deduplicate the same DWG without holding the
        # workspace lock while waiting on the LAN conversion service.
        with self.import_lock:
            return self._ingest(filename, content, synthetic)

    def reindex(self, snapshot_id):
        """Use the shared immutable reindexer; never reconvert or rewrite history."""
        from cad_agent.cad.reindex import reindex_drawing
        return reindex_drawing(self, snapshot_id)

    def _ingest(self, filename, content, synthetic=False):
        filename = Path(filename.replace("\\", "/")).name
        suffix = Path(filename).suffix.lower()
        if suffix not in (".dxf", ".dwg"):
            raise ValueError("仅支持 DXF 或 DWG 图纸；本次导入已终止。")
        if len(content) > self.settings.max_upload_bytes:
            raise ValueError("图纸超过 32 MB 上传上限。")
        original_digest = sha256(content).hexdigest()
        converted = None
        if suffix == ".dwg":
            # A validated local result remains usable after the server's 24h retention.
            for previous in self.store.list("drawing",filters={"original_sha256":original_digest,"parser_version":PARSER_VERSION}):
                if (previous.get("source_format") == "DWG"
                        and previous.get("original_sha256") == original_digest
                        and previous.get("parser_version") == PARSER_VERSION
                        and previous.get("import_version") == "oda-dwg-v1"):
                    folder = self.store.root / "drawings" / previous["id"]
                    expected = {"source.dwg": original_digest, "source.dxf": previous["sha256"],
                                "oda.json": previous["conversion"]["json_sha256"]}
                    if (all((folder / name).is_file() and sha256((folder / name).read_bytes()).hexdigest() == digest
                            for name, digest in expected.items())
                            and all((folder / name).is_file() for name in ("scene.json", "entities.json", "regions.json"))):
                        return previous
                    raise ConversionError("ODA_LOCAL_RESULT_DAMAGED", "此前导入的本地转换文件缺失或校验失败，请恢复该快照文件后重试。")
            converted = self.converter.convert(filename, content)
        parsed_content = converted.dxf if converted else content
        digest = sha256(parsed_content).hexdigest()
        identity = ("oda-dwg-v1", original_digest) if converted else ()
        snapshot_id = stable_id("snap", digest, PARSER_VERSION, *identity)
        with self.lock:
            try:
                return self.store.get("drawing", snapshot_id)
            except KeyError:
                pass
        # No drawing record or permanent source artifacts before DXF validation.
        with TemporaryDirectory(prefix=".import-", dir=self.settings.data_dir) as temporary:
            folder = Path(temporary)
            target = folder / "source.dxf"
            target.write_bytes(parsed_content)
            try:
                texts, scene, rectangles, segments, diagnostics, regions, frames = self.parse_executor.submit(_parse_drawing, target, snapshot_id).result()
            except Exception:
                if converted:
                    raise ConversionError("ODA_DXF_UNREADABLE", "内网转换返回的 DXF 无法解析或建立来源索引。") from None
                raise
            omitted = converted.metadata.get("omitted_null_block_reference_handles", []) if converted else []
            if omitted:
                diagnostics.append(Diagnostic(
                    code="ODA_NULL_BLOCK_REFERENCE",
                    message=f"原 DWG 有 {len(omitted)} 个无目标定义且无附带属性的块引用，转换 DXF 时未保留这些引用。"
                            "其几何无法恢复，当前图纸仅部分解析；原 DWG 和 ODA JSON 保留异常对象记录。"
                            + " 对象句柄：" + ", ".join(omitted[:12]) + (" 等。" if len(omitted) > 12 else "。"),
                ))
                scene["parse_coverage"]["traversal_complete"] = False
            scene["frames"] = frames
            scene["snapshot_id"] = snapshot_id
            self.store.write_json(folder / "scene.json", scene)
            self.store.write_json(folder / "entities.json", [t.model_dump() for t in texts])
            self.store.write_json(folder / "regions.json", [r.model_dump() for r in regions])
            record = {
                "id": snapshot_id, "snapshot_id": snapshot_id, "name": filename,
                # Existing evidence/geometry hashes continue to identify the parsed DXF.
                "sha256": digest, "original_sha256": original_digest,
                "created_at": now(), "parser_version": PARSER_VERSION,
                "source_format": "DWG" if converted else "DXF", "parsed_format": "DXF",
                "unit": scene["unit"], "size_bytes": len(parsed_content),
                "original_size_bytes": len(content), "synthetic": synthetic,
                "layouts": scene["layouts"], "frames": frames,
                "text_count": len(texts), "region_count": len(regions),
                "index_coverage": scene["parse_coverage"],
                "diagnostics": [d.model_dump() for d in diagnostics],
                "status": "partial" if diagnostics else "ready", "latest_fact_set_id": None,
            }
            if converted:
                (folder / "source.dwg").write_bytes(content)
                (folder / "oda.json").write_bytes(converted.json_bytes)
                record.update(import_version="oda-dwg-v1", conversion=converted.metadata)
            with self.lock:
                try:
                    return self.store.get('drawing',snapshot_id)
                except KeyError:
                    pass
                destination = self.store.artifact_dir(snapshot_id)
                try:
                    for source in folder.iterdir():
                        source.replace(destination / source.name)
                    references = self.store.share_artifacts(snapshot_id)
                    if references:
                        record["artifact_references"] = references
                    # Publish last, so failed imports never appear as ready drawings.
                    self.store.put("drawing", snapshot_id, record)
                except Exception:
                    shutil.rmtree(destination)
                    raise
                return record

    def regions(self, snapshot_id):
        self.store.get("drawing", snapshot_id)
        return [
            SourceRegion.model_validate(x)
            for x in json.loads(
                self.store.artifact(snapshot_id, "regions.json").read_text()
            )
        ]

    def rendering(self, snapshot_id):
        """Versioned display cache; never rewrite facts, evidence or source IDs."""
        from cad_agent.cad.render import RENDER_VERSION, render_drawing, font_signature
        self.store.get("drawing", snapshot_id)
        target = self.store.artifact(snapshot_id, "render.json")
        with self.lock:
            if target.exists():
                cached = json.loads(target.read_text())
                if (cached.get("version") == RENDER_VERSION
                        and cached.get("font_signature") == font_signature()):
                    return cached
            rendered = render_drawing(self.store.artifact(snapshot_id, "source.dxf"), snapshot_id)
            self.store.write_json(target, rendered)
            return rendered

    def preview_scene(self, snapshot_id):
        self.store.get("drawing", snapshot_id)
        scene = json.loads(self.store.artifact(snapshot_id, "scene.json").read_text())
        scene["render"] = self.rendering(snapshot_id)
        # The renderer already contains geometry. Keep semantic text metadata
        # for source navigation but do not transfer a second set of linework.
        scene["lines"] = []
        # Display bounds follow visible geometry; semantic scope remains in the
        # original scene/regions files and is not migrated by a rendering fix.
        scene["layouts"] = scene["render"]["layouts"]
        scene["preview_note"] = "DXF 轻量预览 · 简化字体、线型及填充花纹 · 来源定位框为实体估算范围"
        return scene

    def start_extract(self, request: ExtractionRequest):
        drawing = self.store.get("drawing", request.snapshot_id)
        if request.layout_ids and not set(request.layout_ids) <= set(
            x["layout_id"] for x in drawing["layouts"]
        ):
            raise ValueError("所选布局不属于当前文件版本。")
        if request.frame_ids and not set(request.frame_ids) <= set(
            x["frame_id"] for x in drawing["frames"]
        ):
            raise ValueError("所选图框不属于当前文件版本。")
        signature = (
            json.dumps(request.model_dump(), sort_keys=True) + EXTRACTOR_REVISION
        )
        if request.mode == "model":
            signature += (
                self.settings.api_base
                + self.settings.model
                + PROMPT_VERSION
                + self.settings.protocol
            )
        cache_id = stable_id("facts", signature)
        with self.lock:
            for j in self.store.list("job",filters={"cache_id":cache_id,"status":["queued","running","completed","partial"]}):
                if j.get("cache_id") == cache_id and j["status"] in (
                    "queued",
                    "running",
                    "completed",
                    "partial",
                ):
                    return j | {"cached": True}
            job = {
                "id": "job_" + uuid4().hex,
                "status": "queued",
                "stage": "等待处理",
                "snapshot_id": request.snapshot_id,
                "cache_id": cache_id,
                "created_at": now(),
                "request": request.model_dump(),
                "result": None,
                "error": None,
            }
            claimed = self.execution.claim('job', job)
            if claimed['id'] != job['id']:
                return claimed | {'cached': True}
            try:
                self.executor.submit(self._extract, job, request)
            except Exception:
                self.execution.update('job',job['id'],job['execution_token'],{'status':'failed','error':{'code':'QUEUE_FULL','message':'任务队列已满。'}})
                self.execution.finish('job',job['id'],job['execution_token'])
                raise
            return job

    def _extract(self, job, request):
        started = time.monotonic()

        def update(**kwargs):
            if kwargs.get('status') not in ('failed','cancelled'):
                ExecutionContext(self.execution,'job',job['id'],job['execution_token']).check_cancelled()
            job.update(self.execution.update('job',job['id'],job['execution_token'],kwargs))
            if kwargs.get('status') in ('completed','partial','failed','cancelled'):
                self.execution.finish('job',job['id'],job['execution_token'])

        try:
            update(status="running", stage="读取来源区域", queue_wait_seconds=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(job["created_at"])).total_seconds()))
            drawing = self.store.get("drawing", request.snapshot_id)
            regions = [
                r
                for r in self.regions(request.snapshot_id)
                if (not request.layout_ids or r.layout_id in request.layout_ids)
                and (not request.frame_ids or r.frame_id in request.frame_ids)
            ]
            if not regions:
                raise ValueError("所选范围没有原生文字，无法提取砌体事实。")
            proposals = None
            usage = []
            if request.mode == "model":
                proposals = []
                batch = []
                chars = 0
                batches = []
                for region in regions:
                    if len(region.text) > 16000:
                        raise ValueError(
                            "单个来源区域过长，模型模式未截断原文。请缩小图框范围或使用本地提取。"
                        )
                    if chars + len(region.text) > 18000 and batch:
                        batches.append(batch)
                        batch = []
                        chars = 0
                    batch.append(region)
                    chars += len(region.text)
                if batch:
                    batches.append(batch)
                if len(batches) > 12:
                    raise ValueError(
                        "范围超过单次模型任务的 12 批预算，请选择更小的图框范围。"
                    )
                for i, batch in enumerate(batches):
                    update(stage=f"模型整理来源 {i+1}/{len(batches)}")
                    result, u = self.gateway.propose(batch)
                    proposals.extend(result)
                    usage.append(u)
            update(stage="绑定原文与校验事实")
            practices, evidence, coverage, diagnostics, mode = extract(
                request.snapshot_id, regions, drawing["unit"], proposals
            )
            if drawing["diagnostics"]:
                coverage.completeness = "partial"
            result = {
                "id": job["cache_id"],
                "fact_set_id": job["cache_id"],
                "snapshot_id": request.snapshot_id,
                "created_at": now(),
                "schema_version": "1.0",
                "extractor_ref": EXTRACTOR_VERSION,
                "mode": mode,
                "model": self.settings.model if request.mode == "model" else None,
                "practices": [x.model_dump() for x in practices],
                "evidence": [e.model_dump() for e in evidence],
                "coverage": coverage.model_dump(),
                "diagnostics": diagnostics,
                "manifest": {
                    "parser_version": PARSER_VERSION,
                    "extractor_version": EXTRACTOR_REVISION,
                    "prompt_version": PROMPT_VERSION if proposals is not None else None,
                    "input_sha256": drawing["sha256"],
                    "source_scope": request.model_dump(),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "usage": usage,
                    "policy_applied": False,
                },
            }
            with self.execution.transaction() as c:
                current=self.execution.read(c,'job',job['id'])
                if current.get('execution_token')!=job['execution_token'] or current.get('execution_lease_until',0)<time.time() or current.get('cancel_requested'):
                    raise ExecutionCancelled('提取取消或执行租约已失效。')
                self.execution.write(c,'fact_set',result['id'],result)
                drawing=self.execution.read(c,'drawing',request.snapshot_id)
                drawing['latest_fact_set_id']=result['id']
                self.execution.write(c,'drawing',drawing['id'],drawing)
                current.update(status='partial' if coverage.completeness=='partial' else 'completed',stage='提取完成',
                    result={'fact_set_id':result['id'],'practice_count':len(practices)},elapsed_seconds=result['manifest']['elapsed_seconds'])
                self.execution.write(c,'job',job['id'],current)
            self.execution.finish('job',job['id'],job['execution_token'])
        except ExecutionCancelled:
            try:
                update(status='cancelled',stage='提取已取消')
            except ExecutionCancelled:
                pass
        except ModelError as e:
            update(
                status="failed",
                stage="模型调用未完成",
                error={"code": e.code, "message": e.message},
            )
        except ValueError as e:
            update(
                status="failed",
                stage="提取未完成",
                error={"code": "INVALID_SCOPE", "message": str(e)},
            )
        except Exception:
            update(
                status="failed",
                stage="提取未完成",
                error={
                    "code": "EXTRACTION_FAILED",
                    "message": "提取发生错误，未发布新事实，请检查来源格式。",
                },
            )

    def facts(
        self,
        fact_set_id,
        query="",
        wall_role="",
        review_status="",
        frame_id="",
        offset=0,
        limit=100,
    ):
        data = self.store.get("fact_set", fact_set_id)
        items = data["practices"]
        if query:
            items = [
                p
                for p in items
                if query.casefold() in p["original_text"].casefold()
                or query.casefold() in p["frame_name"].casefold()
            ]
        if wall_role:
            items = [
                p for p in items if p["attributes"]["wall_role"]["value"] == wall_role
            ]
        if review_status:
            items = [p for p in items if p["review_status"] == review_status]
        if frame_id:
            items = [p for p in items if p["frame_id"] == frame_id]
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "offset": offset,
            "limit": limit,
            "coverage": data["coverage"],
            "diagnostics": data["diagnostics"],
            "manifest": data["manifest"],
            "mode": data["mode"],
        }

    def answer(self, request):
        if request.mode == "agent":
            from cad_agent.analysis.agent import AgentRunner

            return AgentRunner(self, request).run(history=self.chat_history(request))
        drawing = self.store.get("drawing", request.snapshot_id)
        fid = request.fact_set_id or drawing.get("latest_fact_set_id")
        if not fid:
            return {
                "answer": "这张图纸还没有事实集合。请先提取砌体做法，再查询原文。",
                "citations": [],
                "mode": "grounded_query",
            }
        facts = self.store.get("fact_set", fid)
        if facts["snapshot_id"] != request.snapshot_id:
            raise ValueError("事实集合不属于当前图纸版本。")
        return answer_facts(facts, request)

    def chat_history(self, request):
        records = []
        parent = request.parent_run_id
        for _ in range(3):
            if not parent:
                break
            previous = self.store.get("chat_run", parent)
            if (
                previous["snapshot_id"] != request.snapshot_id
                or previous["status"] != "completed"
            ):
                raise ValueError("只能继续当前图纸已完成的问答。")
            if previous['request'].get('related_drawings',[]) != [d.model_dump() for d in request.related_drawings]:
                raise ValueError('关联资料已改变，请开始新的问答，不自动复用旧资料回答。')
            records.append(previous)
            parent = previous["request"].get("parent_run_id")
        messages = []
        for record in reversed(records):
            messages.extend(
                [
                    {"role": "user", "content": record["request"]["question"]},
                    {"role": "assistant", "content": record["result"]["answer"]},
                ]
            )
        return messages

    def start_chat(self, request):
        from cad_agent.analysis.agent import AgentRunner, AGENT_VERSION

        if request.mode != "agent":
            raise ValueError("后台问答只接受 agent 模式；本地查询使用 /api/chat。")
        runner = AgentRunner(self, request)
        history = self.chat_history(request)
        with self.lock:
            if request.resume_task_id:
                current=self.store.get('agent_task',request.resume_task_id)
                if current['status']=='running':raise ValueError('任务已经在运行。')
            active = [
                r
                for r in self.store.list("chat_run",filters={"status":["queued","running"]})
                if r["status"] in ("queued", "running")
            ]
            if len(active) >= 2:
                raise ValueError("已有两个模型问答正在执行，请等待完成后重试。")
            record = {
                "id": "chat_" + uuid4().hex,
                "snapshot_id": request.snapshot_id,
                "created_at": now(),
                "request": request.model_dump(),
                "status": "queued",
                "stage": "等待模型问答",
                "task_id": runner.task.data["task_id"],
                "trace": [],
                "run_events": [],
                "journal_version": 1,
                "usage": [],
                "result": None,
                "error": None,
                "model": runner.w.settings.model,
                "model_profile": runner.w.settings.model_profile,
                "runtime_requested": request.runtime,
                "input_sha256": runner.drawing["sha256"],
                "agent_version": AGENT_VERSION,
                "policy_applied": False,
                "code_execution_enabled": False,
            }
            self.execution.claim('chat_run',record,runner.task.data)
            try:
                self.agent_executor.submit(self._chat, runner, history, record)
            except Exception:
                self.execution.update('chat_run',record['id'],record['execution_token'],{'status':'failed','error':{'code':'QUEUE_FULL','message':'任务队列已满。'}})
                runner.task.data['status']='partial'
                runner.task.save()
                self.execution.finish('chat_run',record['id'],record['execution_token'],runner.task.data)
                raise
            return record

    def cancel_chat(self, run_id):
        return self.execution.cancel(run_id)

    def execution_status(self):
        return {'owner': self.execution.owner, 'chat': {'pending': self.agent_executor.pending, 'capacity': self.agent_executor.capacity},
                'extraction': {'pending': self.executor.pending, 'capacity': self.executor.capacity},
                'geometry': {'pending': self.geometry_executor.pending,'capacity':self.geometry_executor.capacity,'max_workers':1,'last_timing':self.geometry_executor.last_timing}}

    def _chat(self, runner, history, record):
        token=record['execution_token']
        runner.execution=ExecutionContext(self.execution,'chat_run',record['id'],token)
        def update(**fields):
            fields['task_id']=runner.task.data['task_id']
            fields['run_events']=runner.journal.snapshot(fields.get('status'))
            if hasattr(runner,'metrics'): fields['metrics']=runner.metrics.snapshot()
            if fields.get('status')=='failed':
                runner.task.publication_failed(fields.get('error'))
            if fields.get('status')=='completed':
                with self.execution.transaction() as c:
                    current=self.execution.read(c,'chat_run',record['id'])
                    saved_task=self.execution.read(c,'agent_task',runner.task.data['task_id'])
                    if current.get('execution_token')!=token or saved_task.get('execution_token')!=token or current.get('execution_lease_until',0)<time.time() or current.get('cancel_requested'):
                        raise ExecutionCancelled('任务取消或执行租约已失效。')
                    runner.task.data.update(execution_lease_until=0,execution_token=None,revision=saved_task.get('revision',0)+1,updated_at=now())
                    fields['result']['task']=runner.task.public()
                    current.update(fields,execution_token=None,execution_lease_until=0)
                    self.execution.write(c,'agent_task',runner.task.data['task_id'],runner.task.data)
                    self.execution.write(c,'chat_run',record['id'],current)
                    record.update(current)
            else:
                record.update(self.execution.update("chat_run", record["id"], token, fields))

        runner.progress = update
        runner.journal.bind(record['id'])
        try:
            update(status="running",queue_wait_seconds=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(record["created_at"])).total_seconds()))
            initial = runner.journal.start('input', '接收问题与图纸上下文')
            runner.journal.section(initial, 'context', record['request'], '本次问题与选定范围')
            runner.journal.section(initial, 'history', history, '传入的对话历史')
            runner.journal.section(initial, 'instructions', runner.system_prompt, '应用系统提示词')
            runner.journal.end(initial)
            runner.execution.check_cancelled()
            if runner.w.settings.provider == "responses_subscription":
                # Executor scheduling time is separate from the per-question budget.
                runner.started += record["queue_wait_seconds"]
            result = runner.run(history)
            runner.execution.check_cancelled()
            runner.task.data.update(publication_status='published',last_error=None)
            runner.task.refresh_status()
            result['task']=runner.task.public()
            for e in result["citations"]:
                e["evidence_url"] = (
                    f"/api/chat-runs/{record['id']}/evidence/{e['evidence_id']}"
                )
            delivery = runner.journal.start('delivery', '校验完成，准备发布答案')
            runner.journal.section(delivery, 'answer', {
                k: result[k] for k in ('answer', 'claims', 'citations', 'missing_information', 'task') if k in result
            }, '交付内容与未解决项')
            runner.journal.end(delivery)
            update(
                status="completed",
                stage="回答与引用校验完成",
                result=result,
                trace=runner.trace,
                usage=runner.usage,
                retrieval_usage=runner.retrieval_usage,
            )
        except ExecutionCancelled:
            try:
                runner.task.data.update(status='partial',publication_status='not_published')
                runner.task.save()
                update(status='cancelled',stage='问答已取消',error={'code':'CANCELLED','message':'已保存工具检查点和证据，可显式继续。'})
            except ExecutionCancelled:
                pass  # A new owner or expiry recovery owns the final state.
        except ModelError as e:
            update(
                status="failed",
                stage="模型问答未完成",
                error={"code": e.code, "message": e.message},
                runtime_diagnostics=getattr(runner, "runtime_diagnostics", None),
                trace=runner.trace,
                usage=runner.usage,
                retrieval_usage=runner.retrieval_usage,
            )
        except Exception:
            update(
                status="failed",
                stage="问答未完成",
                error={
                    "code": "AGENT_FAILED",
                    "message": "问答执行失败；已有事实与原图保持可用。",
                },
                trace=runner.trace,
                usage=runner.usage,
                retrieval_usage=runner.retrieval_usage,
            )

        finally:
            latest=self.store.get('chat_run',record['id'])
            if latest.get('cancel_requested') and latest['status'] in ('queued','running'):
                try:
                    runner.task.data.update(status='partial',publication_status='not_published')
                    runner.task.save()
                    self.execution.update('chat_run',record['id'],token,{'status':'cancelled','stage':'问答已取消',
                        'run_events':runner.journal.snapshot('cancelled')})
                except ExecutionCancelled:
                    pass
            self.execution.finish('chat_run',record['id'],token,runner.task.data)
