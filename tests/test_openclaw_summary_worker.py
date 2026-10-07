"""Adapter contracts only; these tests do not invoke a model."""
import importlib.util
import json
import stat
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("openclaw_worker", ROOT / "scripts/openclaw_summary_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerAdapterTests(unittest.TestCase):
    def running_writer_task(self, path, required=True):
        worker.write(path / "input.json", {"semantic_review_required": required})
        task = {"id": "write", "kind": "write", "status": "running", "attempts": 1,
                "workspace": str(path), "input": str(path / "input.json"),
                "output": str(path / "output.json"), "input_hash": "offline"}
        worker.write(path / "task.json", task)
        return path / "task.json"

    def test_default_review_writer_isolates_settings_and_cleans_up_on_success_or_failure(self):
        # Private-config lifecycle contracts only; dispatch is mocked, not quality-tested.
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                task_path = self.running_writer_task(home)
                installed = home / ".openclaw/openclaw.json"
                original = {"models": {"providers": {"provider": {"baseUrl": "https://offline.invalid", "apiKey": "fixture",
                                          "models": [{"id": "exact", "contextWindow": 128000, "maxTokens": 6000}]}}},
                            "agents": {"defaults": {"model": {"primary": "provider/exact", "fallbacks": []},
                                                   "contextTokens": 64000}},
                            "tools": {"profile": "coding", "allow": ["read"], "deny": ["exec"]},
                            "other": {"preserve": True}}
                worker.write(installed, original)
                before, mode = installed.read_bytes(), installed.stat().st_mode
                seen = []

                def dispatch(task, model, config, timeout):
                    private = Path(config)
                    seen.append(private)
                    self.assertNotEqual(private.resolve(), installed.resolve())
                    self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
                    expected = {**original, "tools": {**original["tools"], "deny": ["*"]}}
                    self.assertEqual(worker.read(private), expected)
                    self.assertEqual((model, timeout), ("provider/exact", 27))
                    if fail:
                        raise ValueError("Synthetic producer/reviewer failure")
                    return {"ok": True}

                with patch.object(worker.Path, "home", return_value=home), \
                        patch.object(worker, "_run_writer", side_effect=dispatch), \
                        patch.object(worker.subprocess, "Popen") as launch:
                    if fail:
                        with self.assertRaisesRegex(ValueError, "Synthetic"):
                            worker.run(task_path, "provider/exact", timeout=27)
                    else:
                        self.assertTrue(worker.run(task_path, "provider/exact", timeout=27)["ok"])
                    launch.assert_not_called()
                self.assertEqual(len(seen), 1)
                self.assertFalse(seen[0].exists())
                self.assertEqual(installed.read_bytes(), before)
                self.assertEqual(installed.stat().st_mode, mode)

    def test_explicit_unsafe_configs_fail_before_writer_dispatch(self):
        for unsafe in ("global", "global_symlink", "enabled_tools", "string_deny", "exposed"):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                task_path = self.running_writer_task(home)
                installed = home / ".openclaw/openclaw.json"
                worker.write(installed, {"tools": {"deny": ["*"]}})
                installed.chmod(0o600)
                explicit = home / "private.json"
                if unsafe == "global":
                    explicit = installed
                elif unsafe == "global_symlink":
                    explicit.symlink_to(installed)
                else:
                    denied = [] if unsafe == "enabled_tools" else "*" if unsafe == "string_deny" else ["*"]
                    worker.write(explicit, {"tools": {"deny": denied}})
                    explicit.chmod(0o644 if unsafe == "exposed" else 0o600)
                before = installed.read_bytes()
                with patch.object(worker.Path, "home", return_value=home), patch.object(worker, "_run_writer") as dispatch, \
                        patch.object(worker, "invoke") as invoke, patch.object(worker.subprocess, "Popen") as launch:
                    with self.assertRaises(ValueError):
                        worker.run(task_path, "provider/exact", config=str(explicit))
                    dispatch.assert_not_called()
                    invoke.assert_not_called()
                    launch.assert_not_called()
                self.assertEqual(installed.read_bytes(), before)

    def test_explicit_safe_config_is_forwarded_unchanged_and_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            task_path = self.running_writer_task(path)
            config = path / "private.json"
            worker.write(config, {"tools": {"deny": ["*"]}, "models": {"preserve": True}})
            config.chmod(0o600)
            before = config.read_bytes()
            with patch.object(worker, "_run_writer", return_value={"ok": True}) as dispatch:
                worker.run(task_path, "provider/exact", config=str(config))
            self.assertEqual(dispatch.call_args.args[1:3], ("provider/exact", str(config)))
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_legacy_writer_does_not_read_global_or_create_implicit_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            task_path = self.running_writer_task(path, required=False)
            with patch.object(worker.Path, "home", side_effect=AssertionError("Must not inspect global config")), \
                    patch.object(worker.tempfile, "NamedTemporaryFile") as create, \
                    patch.object(worker, "_run_writer", return_value={"ok": True}) as dispatch:
                worker.run(task_path, "provider/exact")
                self.assertIsNone(dispatch.call_args.args[2])
                create.assert_not_called()

    def test_missing_or_invalid_default_config_fails_before_dispatch_without_fallback(self):
        for settings in (None, [], {"tools": "invalid"}):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                task_path = self.running_writer_task(home)
                if settings is not None:
                    worker.write(home / ".openclaw/openclaw.json", settings)
                with patch.object(worker.Path, "home", return_value=home), patch.object(worker, "_run_writer") as dispatch, \
                        patch.object(worker.tempfile, "NamedTemporaryFile") as create:
                    with self.assertRaises((ValueError, OSError)):
                        worker.run(task_path, "provider/exact")
                    dispatch.assert_not_called()
                    create.assert_not_called()

    def test_temporary_config_is_removed_when_serialization_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            task_path = self.running_writer_task(home)
            worker.write(home / ".openclaw/openclaw.json", {"models": {"preserve": True}})
            factory = worker.tempfile.NamedTemporaryFile
            created = []
            def tracked(*args, **kwargs):
                stream = factory(*args, **kwargs)
                created.append(Path(stream.name))
                return stream
            with patch.object(worker.Path, "home", return_value=home), \
                    patch.object(worker.tempfile, "NamedTemporaryFile", side_effect=tracked), \
                    patch.object(worker.json, "dump", side_effect=OSError("Synthetic serialization failure")), \
                    patch.object(worker, "_run_writer") as dispatch:
                with self.assertRaisesRegex(OSError, "serialization failure"):
                    worker.run(task_path, "provider/exact")
                dispatch.assert_not_called()
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0].exists())

    def test_default_private_config_is_shared_by_mocked_producers_and_reviewer(self):
        # Full writer orchestration with synthetic prose, never live quality evidence.
        helper_spec = importlib.util.spec_from_file_location("writer_fixture", ROOT / "tests/test_bounded_writer.py")
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        payload, sources = helper.fixture()
        payload["semantic_review_required"] = True
        seen = []
        def produce(task, workspace, prompt, model, config, timeout, directory):
            seen.append(config)
            self.assertEqual(model, "provider/exact")
            self.assertEqual(worker.read(config)["tools"]["deny"], ["*"])
            self.assertEqual(stat.S_IMODE(Path(config).stat().st_mode), 0o600)
            data = json.loads(prompt.split("\nINPUT DATA:\n", 1)[1])
            if "section" in data:
                value = {"text": "x" * data["target_visible_chars"]}
            elif "leaf_ids" in data:
                value = {"items": [{"evidence_id": key, "section": "详细总结", "reason": "Synthetic claim-specific reason"}
                                    for key in data["leaf_ids"]]}
            else:
                value = {"insights": [{"claim": "synthetic"} for _ in range(6)]}
            return value, {"elapsed_seconds": 1, "estimated_prompt_tokens": 100}
        def audit(task, payload, model, config, timeout, invoke, requests):
            self.assertEqual(model, "provider/exact")
            self.assertEqual(set(seen), {config})
            self.assertTrue(Path(config).exists())
            seen.append(config)
            return {"status": "passed"}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            task_path = self.running_writer_task(home)
            worker.write(home / "input.json", payload)
            installed = home / ".openclaw/openclaw.json"
            worker.write(installed, {"models": {"preserve": True}})
            before = installed.read_bytes()
            with patch.object(worker.Path, "home", return_value=home), \
                    patch.object(worker, "writer_sources", return_value=sources), \
                    patch.object(worker, "invoke", side_effect=produce), \
                    patch("scripts.semantic_review.run_review", side_effect=audit) as reviewer, \
                    patch.object(worker, "validate_task"):
                self.assertTrue(worker.run(task_path, "provider/exact")["ok"])
                reviewer.assert_called_once()
            self.assertGreater(len(seen), 2)
            self.assertEqual(len(set(seen)), 1)
            self.assertFalse(Path(seen[0]).exists())
            self.assertEqual(installed.read_bytes(), before)

    def test_opaque_nodes_boundaries_and_selected_original_knowledge(self):
        # Registry API is mocked here; parent owns its hash/integrity tests.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            leaves = [f"extract-0000:item-{i}" for i in range(6)]
            mapping = {"r0000": leaves[:3], "r0001": leaves[3:]}
            segments = [{"id": f"s{i}", "start": i * 10, "end": i * 10 + 8,
                         "text": f"Original mechanism {i} has a qualification。"} for i in range(6)]
            items = [{"id": f"item-{i}", "claim": f"Original claim {i}",
                      "quote": segments[i]["text"], "segment_ids": [f"s{i}"],
                      "limitations": [f"Original limit {i}"]} for i in range(6)]
            worker.write(path / "chunk.json", {"segments": segments})
            worker.write(path / "extract.json", {"items": items})
            state = {"tasks": {"extract-0000": {"input": str(path / "chunk.json"),
                                                  "output": str(path / "extract.json")}}}
            payload = {"leaf_ids": list(mapping), "reference_registry": {"path": "PRIVATE_REGISTRY", "sha256": "hash"},
                       "source_lookup": {"result": "fixture"}, "section_minimums": {"关键引述": 10}}
            registry = types.SimpleNamespace(load_registry=lambda p: mapping,
                                             expand_refs=lambda p, refs: [leaf for ref in refs for leaf in mapping[ref]])
            with patch.dict("sys.modules", {"reference_registry": registry}), patch("summary_workflow.load", return_value=(None, None, state)):
                sources = worker.writer_sources(payload)
            self.assertEqual(sources["boundaries"], [{"evidence_id": "r0000", "start": 0, "end": 28},
                                                      {"evidence_id": "r0001", "start": 30, "end": 58}])
            self.assertEqual([item["evidence_ids"] for item in sources["knowledge_claims"]], [[leaf] for leaf in leaves])
            self.assertEqual([item["start"] for item in sources["knowledge_boundaries"]], [i * 10 for i in range(6)])
            self.assertEqual([item["claim"] for item in sources["knowledge_claims"]], [f"Original claim {i}" for i in range(6)])
            self.assertTrue(all(q["evidence_id"] in mapping for q in sources["verified_quotations"]))

    def test_reduce_prompt_does_not_transmit_registry_or_expand_node_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            worker.write(path / "input.json", {"leaf_ids": ["r0000"],
                         "reference_registry": {"path": "PRIVATE_REGISTRY", "sha256": "PRIVATE_HASH"}})
            worker.write(path / "task.md", "Reduce nodes.\n\nInput: private path")
            task = {"kind": "reduce", "input": str(path / "input.json"), "instruction": str(path / "task.md"),
                    "output": str(path / "output.json"), "input_hash": "hash"}
            prompt = worker.build_prompt(task)
            self.assertIn("r0000", prompt)
            self.assertNotIn("reference_registry", prompt)
            self.assertNotIn("PRIVATE_REGISTRY", prompt)
            self.assertNotIn("PRIVATE_HASH", prompt)

    def test_complete_excerpt_retains_full_sentence_without_fixed_length_cuts(self):
        raw = "Earlier context。" + "x" * 170 + "actual quotation" + "important qualification。Next sentence。"
        expected = "x" * 170 + "actual quotationimportant qualification。"
        self.assertEqual(worker.complete_excerpt(raw, "actual quotation"), expected)
        self.assertEqual(worker.complete_excerpt("Whole untimed segment without punctuation", "untimed segment"),
                         "Whole untimed segment without punctuation")
        with self.assertRaises(ValueError):
            worker.complete_excerpt(raw, "fabricated")
    def test_quote_locator_uses_matching_segment_not_nearby_time_tolerance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            source = {"segments": [
                {"id": "s0", "start": 0, "end": 2, "text": "Earlier context."},
                {"id": "s1", "start": 2, "end": 4, "text": "More context."},
                {"id": "s2", "start": 4, "end": 6, "text": "Actual important statement. " + "x" * 180}]}
            items = [{"id": "a", "quote": "Actual important statement.", "segment_ids": ["s0", "s1", "s2"]},
                     {"id": "b", "quote": "Earlier context.", "segment_ids": ["s0"]},
                     {"id": "c", "quote": "More context.", "segment_ids": ["s1"]}]
            worker.write(path / "input.json", source)
            worker.write(path / "output.json", {"items": items})
            state = {"tasks": {"extract": {"input": str(path / "input.json"), "output": str(path / "output.json")}}}
            payload = {"source_lookup": {"result": "fixture"}, "leaf_ids": ["extract:a", "extract:b", "extract:c"],
                       "section_minimums": {"关键引述": 10}}
            with patch("summary_workflow.load", return_value=(None, None, state)):
                sources = worker.writer_sources(payload)
            self.assertEqual(sources["verified_quotations"][0]["start"], 4)

    def test_preflight_accepts_legacy_workspace_without_launching_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            generation = Path(tmp) / "generation"
            directory = generation / "extract-0000"
            directory.mkdir(parents=True)
            task = {"kind": "extract", "input": str(directory / "input.json")}
            with patch.object(worker, "build_prompt", return_value="bounded prompt"):
                workspace, _, estimate = worker.preflight(task)
            self.assertEqual(Path(workspace), generation)
            self.assertEqual(estimate, len("bounded prompt"))
            self.assertNotIn("workspace", task)

    def test_preflight_rejects_over_budget_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = {"kind": "extract", "input": str(Path(tmp) / "task/input.json")}
            with patch.object(worker, "build_prompt", return_value="x" * 10001):
                with self.assertRaisesRegex(ValueError, "exceeds 10000"):
                    worker.preflight(task)

    def test_section_repair_preserves_other_sections_and_rejects_injection(self):
        body = "## A\n\nOld A\n\n## B\n\nPreserve B exactly.\n"
        changed = worker.replace_sections(body, {"A": "New A"}, {"A": 1, "B": 1})
        self.assertIn("New A", changed)
        self.assertTrue(changed.endswith("## B\n\nPreserve B exactly.\n"))
        for updates in ({"C": "new"}, {"A": "## B\nInjected"}, {}):
            with self.assertRaises(ValueError):
                worker.replace_sections(body, updates, {"A": 1, "B": 1})

    def test_accepts_json_and_complete_fence_but_not_commentary(self):
        self.assertEqual(worker.parse_response('{"items":[]}'), {"items": []})
        self.assertEqual(worker.parse_response('```json\n{"items":[]}\n```'), {"items": []})
        for text in ('[]', '```json\n{}', 'Here is the result: {}'):
            with self.assertRaises(ValueError):
                worker.parse_response(text)

    def test_only_redundant_closer_is_normalized_without_discarding_data(self):
        notes = []
        self.assertEqual(worker.parse_response('{"items":[{"claim":"source"}]}}', notes),
                         {"items": [{"claim": "source"}]})
        self.assertEqual(notes, ["removed_one_redundant_trailing_object_closer"])
        notes = []
        self.assertEqual(worker.parse_response('{"text":"same"},{"text":"same"}', notes), {"text": "same"})
        self.assertEqual(notes, ["removed_identical_duplicate_complete_json_object"])
        for text in ('{"items":[]', '{"items":[]} {"more":[]}', '{"items":[]}}}',
                     '{"items":[]} ignored', '{"items":[],"items":[1]}', '{"a":{"x":1,"x":2}}'):
            with self.assertRaises(ValueError):
                worker.parse_response(text)
        for text in ('{"text":"first"},{"text":"different"}', '{"value":0},{"value":false}'):
            with self.assertRaises(ValueError):
                worker.parse_response(text)

    def test_extract_prompt_contains_only_own_input_not_source_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            worker.write(path / "input.json", {"segments": [{"text": "source", "start": 0, "end": 1}]})
            worker.write(path / "task.md", "# extract\n\nExtract source.\n\nInput: private-source-path\n")
            task = {"kind": "extract", "input": str(path / "input.json"),
                    "instruction": str(path / "task.md"), "output": str(path / "output.json"), "input_hash": "hash"}
            prompt = worker.build_prompt(task)
            self.assertIn('"0:0": "source"', prompt)
            self.assertNotIn("private-source-path", prompt)
            self.assertIn("Do not call tools", prompt)

    def test_compact_evidence_copies_exact_source_not_model_quotation(self):
        payload = {"id": "chunk", "source_hash": "hash", "segments": [
            {"id": "stable1", "text": "First. Qualification!", "start": 0, "end": 5},
            {"id": "stable2", "text": "Second。", "start": 5, "end": 9}]}
        response = {"covered_indices": [0, 1], "items": [{"id": "one", "claim": "qualified claim",
                    "quote_ref": "0:0", "segment_indices": [0], "quote": "invented"}]}
        expanded = worker.expand_extraction(payload, response)
        self.assertEqual(expanded["items"][0]["quote"], "First. Qualification!")
        self.assertEqual(expanded["covered_segment_ids"], ["stable1", "stable2"])
        literal = {**response, "items": [{**response["items"][0], "id": 1, "quote_ref": "Qualification!"}]}
        expanded_literal = worker.expand_extraction(payload, literal)
        self.assertEqual(expanded_literal["items"][0]["quote"], "Qualification!")
        self.assertEqual(expanded_literal["items"][0]["id"], "1")
        for covered in ([0], [0, 0], [True, 1]):
            with self.assertRaises(ValueError):
                worker.expand_extraction(payload, {**response, "covered_indices": covered})
        for indices, ref in (([1], "0:0"), ([0], "99:0"), ([False], "0:0"), ([[0]], "0:0")):
            bad = {**response, "items": [{**response["items"][0], "segment_indices": indices, "quote_ref": ref}]}
            with self.assertRaises(ValueError):
                worker.expand_extraction(payload, bad)

    def test_source_catalog_slices_are_contiguous_and_cover_all_text(self):
        source = " a。" + "long" * 40 + "！\n End?"
        values = list(worker.source_catalog({"segments": [{"text": source}]}).values())
        self.assertEqual("".join(values), source)
        self.assertTrue(all(len(value) <= 60 for value in values))

    def test_writer_saves_all_files_then_checks_without_claiming_validity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            task = {"kind": "write", "output": str(path / "output.json"), "input_hash": "expected"}
            response = {"body": "## Body", "knowledge": {"insights": []}, "coverage": {"items": []}}
            with patch.object(worker, "validate_task", side_effect=ValueError("invalid evidence")) as check:
                with self.assertRaisesRegex(ValueError, "invalid evidence"):
                    worker.save_response(task, response)
            self.assertTrue(all((path / name).exists() for name in
                                ("body.md", "knowledge.draft.json", "coverage.json", "output.json")))
            self.assertEqual(worker.read(path / "output.json"), {"input_hash": "expected"})
            check.assert_called_once_with(task)

    def test_rejects_missing_writer_fields_before_writing_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = {"kind": "write", "output": str(Path(tmp) / "output.json"), "input_hash": "hash"}
            with self.assertRaises(ValueError):
                worker.save_response(task, {"body": "incomplete"})
            self.assertFalse(Path(task["output"]).exists())

    def test_unstarted_task_cannot_launch_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            worker.write(path, {"status": "complete"})
            with patch.object(worker.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(ValueError, "Coordinator must start"):
                    worker.run(path, "provider/current")
                launch.assert_not_called()

    def test_writer_resume_reuses_valid_parts_without_model_quotation_calls(self):
        # Orchestration only: synthetic text and final validator mock are NOT quality evidence.
        helper_spec = importlib.util.spec_from_file_location("writer_fixture", ROOT / "tests/test_bounded_writer.py")
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        payload, sources = helper.fixture()
        calls = []
        def invoke(task, workspace, prompt, model, config, timeout, directory):
            data = json.loads(prompt.split("\nINPUT DATA:\n", 1)[1].split("\nPrevious part rejection", 1)[0])
            if "section" in data:
                name = data["section"]
                calls.append(name)
                text = "x" * data["target_visible_chars"]
                if name == "内容大纲" and calls.count(name) == 1:
                    text = "short"
                value = {"text": text}
            elif "leaf_ids" in data:
                calls.append("coverage")
                value = {"items": [{"evidence_id": key, "section": "详细总结", "reason": "synthetic"}
                                    for key in data["leaf_ids"]]}
            else:
                calls.append("knowledge")
                value = {"insights": [{"claim": "synthetic"} for _ in range(6)]}
            return value, {"elapsed_seconds": 1, "estimated_prompt_tokens": len(prompt.encode("utf-8"))}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            worker.write(path / "input.json", payload)
            task = {"id": "write", "kind": "write", "attempts": 1, "workspace": tmp,
                    "input": str(path / "input.json"), "input_hash": "synthetic", "output": str(path / "output.json")}
            with patch.object(worker, "writer_sources", return_value=sources), patch.object(worker, "invoke", side_effect=invoke), patch.object(worker, "validate_task") as final_check:
                with self.assertRaisesRegex(ValueError, "Short section"):
                    worker.run_writer(task, "provider/model", None, 600)
                task["attempts"] = 2
                final_check.side_effect = ValueError("Report evidence coverage incomplete")
                with self.assertRaisesRegex(ValueError, "coverage incomplete"):
                    worker.run_writer(task, "provider/model", None, 600)
                final_check.side_effect = None
                task["attempts"] = 3
                self.assertTrue(worker.run_writer(task, "provider/model", None, 600)["ok"])
            self.assertEqual(calls.count("内容摘要"), 1)
            self.assertEqual(calls.count("内容大纲"), 2)
            self.assertNotIn("关键引述", calls)
            self.assertEqual(calls.count("knowledge"), 1)
            self.assertEqual(calls.count("coverage"), 2)
            self.assertTrue((path / "writer-run-attempt-1.json").exists())
            self.assertTrue((path / "writer-run-attempt-2.json").exists())

    def test_required_review_precedes_success_and_native_validation(self):
        # Synthetic prose and mocked validators exercise ordering, not live quality.
        from scripts import bounded_writer
        helper_spec = importlib.util.spec_from_file_location("writer_fixture", ROOT / "tests/test_bounded_writer.py")
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        payload, sources = helper.fixture()
        payload["semantic_review_required"] = True
        events = []

        def produce(task, workspace, prompt, model, config, timeout, directory):
            data = json.loads(prompt.split("\nINPUT DATA:\n", 1)[1])
            if "section" in data:
                value = {"text": "x" * data["target_visible_chars"]}
            elif "leaf_ids" in data:
                value = {"items": [{"evidence_id": key, "section": "详细总结", "reason": "Synthetic claim-specific reason"}
                                    for key in data["leaf_ids"]]}
            else:
                value = {"insights": [{"claim": "synthetic"} for _ in range(6)]}
            return value, {"elapsed_seconds": 1, "estimated_prompt_tokens": 100}

        def audit(task, *args):
            directory = Path(task["output"]).parent
            self.assertTrue((directory / "body.md").exists())
            self.assertTrue((directory / "knowledge.draft.json").exists())
            events.append("review")
            if len(events) == 1:
                # The real gate has separate tests for selective invalidation.
                raise ValueError("Semantic review failed: knowledge source mismatch")
            return {"status": "passed"}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            worker.write(path / "input.json", payload)
            task = {"id": "write", "kind": "write", "attempts": 1, "workspace": tmp,
                    "input": str(path / "input.json"), "input_hash": "synthetic", "output": str(path / "output.json")}
            config = path / "private-config.json"
            worker.write(config, {"tools": {"deny": ["*"]}})
            config.chmod(0o600)
            with patch.object(worker, "writer_sources", return_value=sources), patch.object(worker, "invoke", side_effect=produce), \
                    patch("scripts.semantic_review.run_review", side_effect=audit), \
                    patch.object(worker, "validate_task", side_effect=lambda task: events.append("validate")) as check:
                with self.assertRaisesRegex(ValueError, "Semantic review failed"):
                    worker.run_writer(task, "provider/model", str(config), 600)
                check.assert_not_called()
                # An unclassified semantic failure must not trigger the structural
                # handler's keyword-based knowledge-cache deletion.
                request = next(r for r in bounded_writer.plan(payload, sources) if r["kind"] == "knowledge")
                key = worker.digest([request["kind"], request["name"]])[:16]
                self.assertTrue((path / "parts" / key / "validated.json").exists())
                task["attempts"] = 2
                self.assertTrue(worker.run_writer(task, "provider/model", str(config), 600)["ok"])
                self.assertEqual(events, ["review", "review", "validate"])

    def test_repair_prompt_excludes_valid_sections_and_unchanged_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            payload = {"entries": [], "leaf_ids": ["one"], "section_minimums": {"内容摘要": 20, "详细总结": 10}}
            worker.write(path / "input.json", payload)
            worker.write(path / "body.md", "## 内容摘要\n短\n## 详细总结\nALREADY_VALID_SECTION_CONTENT\n")
            worker.write(path / "repair.json", {"error": "Repair only short/missing section: 内容摘要"})
            worker.write(path / "knowledge.draft.json", {"private_marker": "UNCHANGED_KNOWLEDGE"})
            worker.write(path / "coverage.json", {"items": []})
            task = {"kind": "write", "input": str(path / "input.json"), "output": str(path / "output.json"), "input_hash": "hash"}
            with patch.object(worker, "writer_sources", return_value={"boundaries": [], "verified_quotations": []}):
                prompt = worker.build_prompt(task)
            self.assertIn("REPAIR MODE", prompt)
            self.assertIn('"actual": 1', prompt)
            self.assertNotIn("ALREADY_VALID_SECTION_CONTENT", prompt)
            self.assertNotIn("UNCHANGED_KNOWLEDGE", prompt)


if __name__ == "__main__":
    unittest.main()
