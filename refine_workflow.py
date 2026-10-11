"""Local, provenance-preserving refinement of exhausted evidence tasks.

No inference is performed. Source reads are limited to the existing workflow's
fingerprint check; splitting uses only the task's already prepared input.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import summary_workflow as workflow
from reference_registry import expand_reduction, expand_refs, load_registry, pack_entries

MAX_INPUT_BYTES = 12000
MAX_REFERENCE_NODES = 12
POLICY = "whole_inputs_contiguous_v2"
WRITER_FORMAT = "scoped_body_coverage_v7"
COVERAGE_WORKER_CONTRACT = "scoped_body_coverage_v2"


def retain_refinement_plan(old, fingerprint, settings):
    """Prepare hook: call before calculating its generation ID.

    Same-source refined workflows may be resumed, not silently rechunked.
    Reject changed settings rather than discarding the refinement/retry ledger.
    """
    settings = copy.deepcopy(settings)
    if (old and old.get("version") == workflow.PIPELINE_VERSION
            and old.get("source_hash") == fingerprint
            and old.get("settings", {}).get("refinement_plan")):
        original = {k: v for k, v in old["settings"].items() if k != "refinement_plan"}
        if (settings.get("writer_format") in {"chinese_markdown_independent_parts_v6", WRITER_FORMAT}
                and not any(task["kind"] == "write" for task in old.get("tasks", {}).values())):
            settings["writer_format"] = original.get("writer_format", settings["writer_format"])
        if original != {k: v for k, v in settings.items() if k != "refinement_plan"}:
            raise ValueError("Same-source refined workflow settings changed; retain the current settings")
        settings["refinement_plan"] = copy.deepcopy(old["settings"]["refinement_plan"])
    return settings


def _checked_input(task):
    payload = workflow.read(task["input"])
    if workflow.digest(payload) != task["input_hash"]:
        raise ValueError("Task input changed; refinement refused")
    if "reference_registry" in payload:
        load_registry(payload)
    return payload


def _input_bytes(payload):
    return len((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _extract_payload(parent, segments, context, index):
    payload = copy.deepcopy(parent)
    primary_hash = hashlib.sha256("\n".join(s["text"] for s in segments).encode("utf-8")).hexdigest()
    payload.update(id=f"{parent['id']}-refine-{index:04d}-{primary_hash[:12]}",
                   source_hash=primary_hash, segments=copy.deepcopy(segments),
                   context_segments=copy.deepcopy(context),
                   estimated_tokens=workflow.tokens(context + segments))
    return payload


def _extract_groups(payload, settings):
    segments = payload["segments"]
    ids = [s["id"] for s in segments]
    if len(segments) < 2:
        raise ValueError("Cannot refine an indivisible primary segment")
    if len(ids) != len(set(ids)) or any(not isinstance(key, str) or not key for key in ids):
        raise ValueError("Invalid primary segment IDs")
    context = payload.get("context_segments", [])
    limit = min(MAX_INPUT_BYTES, settings["target_tokens"])
    maximum = max(1, len(segments) // 2)
    groups, current = [], []
    offset = 0

    def child(group):
        full_context = copy.deepcopy(context)
        if offset and segments[offset - 1] not in full_context:
            full_context.append(copy.deepcopy(segments[offset - 1]))
        return _extract_payload(payload, group, full_context, len(groups))

    for segment in segments:
        if current and (len(current) >= maximum or _input_bytes(child(current + [segment])) > limit):
            groups.append(child(current))
            offset += len(current)
            current = []
        current.append(segment)
        if _input_bytes(child(current)) > limit:
            raise ValueError("Indivisible primary segment plus full context exceeds refinement budget")
    if current:
        groups.append(child(current))
    if len(groups) < 2 or [s for group in groups for s in group["segments"]] != segments:
        raise ValueError("Refinement must be an exact primary segment partition")
    return groups, [[s["id"] for s in group["segments"]] for group in groups]


def _canonical_entries(payload, state):
    registry = load_registry(payload)
    entries, seen_nodes, seen_leaves, seen_tasks = [], set(), set(), set()
    dependencies = payload.get("dependencies", {})
    for entry in payload["entries"]:
        key = entry["task_id"]
        if key in seen_tasks or key not in dependencies or key not in state["tasks"]:
            raise ValueError("Invalid or duplicate reduction source entry")
        seen_tasks.add(key)
        task = state["tasks"][key]
        if task["status"] != "complete" or workflow.output_hash(task) != dependencies[key]:
            raise ValueError("Reduction dependency changed or incomplete")
        output = workflow.validate_task(task)
        leaves = workflow.leaves(task, output)
        expected = {"items": output["items"], "omitted": output.get("omitted", [])}
        if task["kind"] == "extract":
            expected = {"items": [{**{k: v for k, v in item.items() if k not in {"id", "segment_ids"}},
                                   "evidence_ids": [f"{key}:{item['id']}"]} for item in output["items"]]}
        evidence = entry["evidence"]
        nodes = [ref for item in evidence.get("items", []) for ref in item["evidence_ids"]]
        nodes.extend(item["evidence_id"] for item in evidence.get("omitted", []))
        expanded = expand_reduction(payload, evidence)
        # expand_reduction supplies an empty omissions array even for extracts.
        expected = {**expected, "omitted": expected.get("omitted", [])}
        if payload.get("refinement_contract") == "canonical_topic_slices_v2":
            for field in ("items", "omitted"):
                canonical, cursor = expected[field], 0
                for item in expanded[field]:
                    while cursor < len(canonical) and canonical[cursor] != item:
                        cursor += 1
                    if cursor == len(canonical):
                        raise ValueError("Refined source topic differs from validated canonical evidence")
                    cursor += 1
            leaves = expand_refs(payload, nodes)
        elif expanded != expected:
            raise ValueError("Reduction source entry differs from validated canonical evidence")
        original = expand_refs(payload, nodes)
        if set(original) != set(leaves) or seen_leaves.intersection(original):
            raise ValueError("Reduction entries must exactly partition original leaf IDs")
        seen_leaves.update(original)
        seen_nodes.update(nodes)
        entries.append({"task_id": key, "leaf_ids": leaves, "evidence": expanded})
    if seen_tasks != set(dependencies) or seen_nodes != set(registry):
        raise ValueError("Reduction source entries do not cover the canonical input")
    if seen_leaves != set(expand_refs(payload, payload["leaf_ids"])):
        raise ValueError("Reduction source entries lost original leaf IDs")
    return entries


def _reduce_payload(parent, entries, generation, key):
    packed, nodes, registry = pack_entries(entries)
    payload = {k: copy.deepcopy(v) for k, v in parent.items()
               if k not in {"entries", "leaf_ids", "reference_registry", "dependencies"}}
    path = generation / "reference-registry" / f"{key}-{workflow.digest(registry)[:12]}.json"
    payload.update(entries=packed, leaf_ids=nodes, refinement_contract="canonical_topic_slices_v2",
                   reference_registry={"path": str(path), "sha256": workflow.digest(registry)},
                   dependencies={e["task_id"]: parent["dependencies"][e["task_id"]] for e in entries})
    return payload, registry


def _reduce_groups(payload, state, generation, prefix):
    entries = _canonical_entries(payload, state)
    limit = min(MAX_INPUT_BYTES, state["settings"]["synthesis_tokens"], _input_bytes(payload) - 1)
    maximum = max(1, len(entries) // 2)
    groups, current = [], []

    def candidate(group):
        return _reduce_payload(payload, group, generation, f"{prefix}-{len(groups):04d}")

    def fits(group):
        value, _ = candidate(group)
        return len(value["leaf_ids"]) <= MAX_REFERENCE_NODES and _input_bytes(value) <= limit

    # Split oversized source entries at complete canonical topics only.
    expanded_entries = []
    for entry in entries:
        if fits([entry]):
            expanded_entries.append(entry)
            continue
        atoms = [("items", item) for item in entry["evidence"].get("items", [])]
        atoms += [("omitted", item) for item in entry["evidence"].get("omitted", [])]
        current_atoms, seen = [], set()

        def slice_entry(values):
            evidence = {"items": [], "omitted": []}
            for field, item in values:
                evidence[field].append(copy.deepcopy(item))
            refs = list(dict.fromkeys([ref for item in evidence["items"] for ref in item["evidence_ids"]]
                                     + [item["evidence_id"] for item in evidence["omitted"]]))
            return {"task_id": entry["task_id"], "leaf_ids": refs, "evidence": evidence}

        def append_slice(values):
            part = slice_entry(values)
            if seen.intersection(part["leaf_ids"]):
                raise ValueError("Cannot split overlapping canonical topics")
            seen.update(part["leaf_ids"])
            expanded_entries.append(part)

        for atom in atoms:
            if current_atoms and not fits([slice_entry(current_atoms + [atom])]):
                append_slice(current_atoms)
                current_atoms = []
            current_atoms.append(atom)
            if not fits([slice_entry(current_atoms)]):
                raise ValueError("Indivisible canonical source entry/topic exceeds refinement budget")
        if current_atoms:
            append_slice(current_atoms)
        if seen != set(entry["leaf_ids"]):
            raise ValueError("Canonical topic splitting lost original evidence")
    entries = expanded_entries
    if len(entries) < 2:
        raise ValueError("Cannot refine an indivisible canonical source entry")
    maximum = max(1, len(entries) // 2)

    for entry in entries:
        if current and (len(current) >= maximum or any(e["task_id"] == entry["task_id"] for e in current)
                        or not fits(current + [entry])):
            groups.append(current)
            current = []
        current.append(entry)
        if not fits(current):
            raise ValueError("Indivisible canonical source entry exceeds refinement budget")
    if current:
        groups.append(current)
    if len(groups) < 2 or [e for group in groups for e in group] != entries:
        raise ValueError("Refinement must be an exact canonical source entry partition")
    return groups, [[e["task_id"] for e in group] for group in groups]


def _copy_task_artifacts(previous, fresh):
    old_directory = Path(previous["input"]).parent
    directory = Path(fresh["input"]).parent
    excluded = {"input.json", "output.json", "task.json", "task.md"}
    for path in old_directory.iterdir():
        if path.name in excluded:
            continue
        target = directory / path.name
        if path.is_dir():
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)


def _clone(state, generation, previous, payload):
    fresh = workflow.add_task(state, generation, previous["id"], previous["kind"], payload)
    current_paths = {k: fresh[k] for k in ("input", "output", "instruction", "workspace")}
    fresh.update(copy.deepcopy(previous))
    fresh.update(current_paths, migrated_from=previous["input"])
    directory = Path(fresh["input"]).parent
    shutil.copy2(previous["input"], fresh["input"])
    _copy_task_artifacts(previous, fresh)
    if previous["status"] == "complete":
        shutil.copy2(previous["output"], fresh["output"])
        workflow.validate_task(fresh)
        if workflow.output_hash(fresh) != workflow.output_hash(previous):
            raise ValueError("Cloned output changed")
    workflow.write(directory / "task.json", fresh)


def _writer_payload(payload, old_input_hash):
    if payload.get("coverage_worker_contract") == COVERAGE_WORKER_CONTRACT:
        raise ValueError("Writer already uses the new coverage protocol; retries cannot be reset")
    previous_hashes = payload.get("migrated_from_input_hashes", [])
    if (not isinstance(previous_hashes, list)
            or any(not isinstance(value, str) or not value for value in previous_hashes)):
        raise ValueError("Invalid writer input-hash migration history")
    fresh = copy.deepcopy(payload)
    fresh.update(coverage_worker_contract=COVERAGE_WORKER_CONTRACT,
                 migrated_from_input_hashes=[old_input_hash] + previous_hashes)
    if workflow.digest(fresh) == old_input_hash:
        raise ValueError("Coverage refinement must change the writer input contract")
    return fresh


def _receipt(base, state, task_id):
    record = state["refinements"][task_id]
    return {"generation": state["generation"], "state_path": str(base / "state.json"),
            "refined_task": task_id, "children": record["children"],
            "snapshot": record["snapshot"], "retry_ledger": record["retry_ledger"]}


def refine(result_path, task_id):
    """Replace one blocked task after exactly three exhausted worker attempts."""
    try:
        import fcntl
    except ImportError as exc:
        raise ValueError("Refinement requires POSIX file locking on this host") from exc
    result = workflow.read(result_path)
    base = workflow.workflow_dir(result)
    # Refinements serialize with each other. Other coordinators must be quiescent;
    # an optimistic state check also catches ordinary concurrent updates.
    with (base / ".refinement.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result, base, old = workflow.load(result_path)
        if any(t["status"] == "running" for t in old["tasks"].values()):
            raise ValueError("Wait for all running tasks before refinement")
        if task_id in old.get("refinements", {}):
            if task_id == "write":
                current = old["tasks"].get("write")
                if not current or current["kind"] != "write" or current.get("id") != "write":
                    raise ValueError("Current canonical coverage writer missing")
                _checked_input(current)
            return _receipt(base, old, task_id)
        task = old["tasks"].get(task_id)
        if (not task or task["kind"] not in {"extract", "reduce", "write"} or task["status"] != "blocked"
                or type(task.get("attempts")) is not int or task["attempts"] != 3):
            raise ValueError("Refinement requires a blocked extract/reduce or coverage writer exhausted at 3 attempts")
        writer = task["kind"] == "write"
        if writer and (task_id != "write" or task.get("id") != "write"
                       or not isinstance(task.get("error"), str)
                       or not task["error"].startswith("Coverage blocked")):
            raise ValueError("Writer refinement requires canonical write with a Coverage blocked error")
        active = next((i for i, level in enumerate(old["levels"])
                       if any(old["tasks"][key]["status"] != "complete" for key in level)), None)
        if active is None or task_id not in old["levels"][active]:
            raise ValueError("Refinement requires a task at the existing active level")
        if writer and (old["levels"][active] != ["write"]
                       or any(value["status"] != "complete" for key, value in old["tasks"].items()
                              if key != "write")):
            raise ValueError("Coverage writer refinement requires all other tasks complete")
        payloads = {key: _checked_input(value) for key, value in old["tasks"].items()}
        if any(task_id in payload.get("dependencies", {}) for payload in payloads.values()):
            raise ValueError("Cannot refine a parent with existing downstream tasks")
        if writer and any(key not in old["tasks"]
                          or old["tasks"][key].get("output_hash") != value
                          for key, value in payloads[task_id].get("dependencies", {}).items()):
            raise ValueError("Coverage writer dependency fingerprint changed")
        for value in old["tasks"].values():
            if value["status"] == "complete":
                workflow.validate_task(value)
                if value.get("output_hash") != workflow.output_hash(value):
                    raise ValueError("Completed output fingerprint changed")
        payload = payloads[task_id]
        prefix = f"{task_id}-refine-{workflow.digest([old['generation'], task_id, task['input_hash'], POLICY])[:12]}"
        if writer:
            groups, partition = [_writer_payload(payload, task["input_hash"])], [["write"]]
        elif task["kind"] == "extract":
            groups, partition = _extract_groups(payload, old["settings"])
        else:
            # Path length affects the byte budget; a generation ID is always 20 chars.
            groups, partition = _reduce_groups(payload, old, base / ("0" * 20), prefix)
        children = ["write"] if writer else [f"{prefix}-{i:04d}" for i in range(len(groups))]
        if not writer and set(children).intersection(set(old["tasks"]) | set(old.get("superseded_tasks", {}))):
            raise ValueError("Refinement child ID collision")
        settings = copy.deepcopy(old["settings"])
        if writer or ("writer_format" in settings
                      and not any(value["kind"] == "write" for value in old["tasks"].values())):
            settings["writer_format"] = WRITER_FORMAT
        plan = {"policy": "writer_coverage_protocol_v2" if writer else POLICY,
                "parent_generation": old["generation"], "task_id": task_id,
                "kind": task["kind"], "input_hash": task["input_hash"], "children": children,
                "partition": partition}
        if writer:
            plan["coverage_worker_contract"] = COVERAGE_WORKER_CONTRACT
        else:
            plan.update(max_input_bytes=MAX_INPUT_BYTES, max_reference_nodes=MAX_REFERENCE_NODES)
        settings.setdefault("refinement_plan", []).append(plan)
        generation_id = workflow.digest([old["source_hash"], settings, workflow.PIPELINE_VERSION])[:20]
        generation = base / generation_id
        if generation.exists():
            raise ValueError("Refinement generation already exists without a committed state")
        state = copy.deepcopy(old)
        state.update(generation=generation_id, settings=settings, tasks={}, assembled=None)
        state.setdefault("superseded_tasks", {})[task_id] = copy.deepcopy(task)
        state["levels"][active] = [child for key in old["levels"][active]
                                   for child in (children if key == task_id else [key])]
        archive = base / old["generation"] / "refinements" / generation_id
        record = {"children": children, "snapshot": str(archive / "state-snapshot.json"),
                  "retry_ledger": str(archive / "retry-ledger.json")}
        state.setdefault("refinements", {})[task_id] = record
        try:
            references = base / old["generation"] / "references"
            if writer and references.is_dir():
                shutil.copytree(references, generation / "references")
            for key, previous in old["tasks"].items():
                if key != task_id:
                    _clone(state, generation, previous, payloads[key])
            for key, group in zip(children, groups):
                if task["kind"] == "reduce":
                    child_payload, registry = _reduce_payload(payload, group, generation, key)
                    workflow.write(child_payload["reference_registry"]["path"], registry)
                else:
                    child_payload = group
                fresh = workflow.add_task(state, generation, key, task["kind"], child_payload)
                if writer:
                    # Cache and dispatch bindings stay byte-for-byte intact. The
                    # worker migrates old per-part bindings before any dispatch.
                    _copy_task_artifacts(task, fresh)
                    fresh["migrated_from"] = task["input"]
                    workflow.write(Path(fresh["input"]).with_name("task.json"), fresh)
            workflow.write(generation / "chunks.json", [
                {"task_id": key, "input": state["tasks"][key]["input"],
                 "input_hash": state["tasks"][key]["input_hash"]} for key in state["levels"][0]])
            workflow.write(generation / "result.json", result)
            workflow.write(generation / "refinement-plan.json", settings["refinement_plan"])
            latest_result, latest_base, latest = workflow.load(result_path)
            if latest != old or latest_result != result or latest_base != base:
                raise ValueError("Workflow changed during refinement")
            workflow.write(record["snapshot"], old)
            workflow.write(record["retry_ledger"], {
                "generation": old["generation"], "tasks": old["tasks"],
                "superseded_tasks": old.get("superseded_tasks", {}),
                "artifact_root": str(base / old["generation"])})
            workflow.write(base / old["generation"] / "state-snapshot.json", old)
            workflow.persist(base, state)
        except Exception:
            if generation.exists():
                shutil.rmtree(generation)
            raise
        return _receipt(base, state, task_id)
