import hashlib
import importlib.util
import json
import pathlib
import copy
import subprocess
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("pull_plugins.py")
SPEC = importlib.util.spec_from_file_location("pull_plugins", MODULE_PATH)
pull_plugins = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pull_plugins)


DIGEST = "sha256:" + "a" * 64
WASM = b"\x00asm\x01\x00\x00\x00test"
WASM_HASH = hashlib.sha256(WASM).hexdigest()


def entry(logical="json-converter", aliases=None):
    return {
        "logicalId": logical,
        "image": "plugins/jsonrpc-converter",
        "version": "2.0.0",
        "ociRef": "registry.example/plugins/jsonrpc-converter:2.0.0",
        "digest": DIGEST,
        "consumers": {"pluginServer": {"inventoryKey": logical, "httpPath": logical, "aliases": aliases or []}},
    }


def lock_item(name="basic-auth"):
    return {"logicalKey": name, "servedPath": name, "servedVersion": "2.0.0", "repository": "registry.example/plugins/" + name,
            "tag": "2.0.0", "digest": DIGEST, "wasmSha256": WASM_HASH}


class SnapshotEntriesTest(unittest.TestCase):
    def test_accepts_asymmetric_json_converter_alias_and_mcp(self):
        snapshot = {"schemaVersion": 1, "plugins": [entry(aliases=["jsonrpc-converter"]), entry("mcp-server")]}
        parsed = list(pull_plugins.snapshot_entries(snapshot))
        self.assertEqual([mapping["inventoryKey"] for _, mapping in parsed], ["json-converter", "mcp-server"])
        self.assertEqual(parsed[0][1]["aliases"], ["jsonrpc-converter"])

    def test_rejects_duplicate_inventory_key(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            list(pull_plugins.snapshot_entries({"schemaVersion": 1, "plugins": [entry(), entry()]}))

    def test_rejects_tag_version_drift(self):
        broken = entry(); broken["ociRef"] = "registry.example/plugins/jsonrpc-converter:1.0.0"
        with self.assertRaisesRegex(ValueError, "disagree"):
            list(pull_plugins.snapshot_entries({"schemaVersion": 1, "plugins": [broken]}))

    def test_dockerfile_uses_fixed_snapshot_and_unmanaged_lock(self):
        dockerfile = (pathlib.Path(__file__).with_name("Dockerfile")).read_text()
        self.assertIn("COPY pull_plugins.py plugins.properties unmanaged-plugins.lock.json snapshot.json ./", dockerfile)
        self.assertIn("sha256sum snapshot.json", dockerfile)
        self.assertIn("io.higress.plugin-snapshot-sha256", dockerfile)


class MaterializeSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.snapshot = self.root / "snapshot.json"
        self.properties = self.root / "plugins.properties"
        self.lock = self.root / "unmanaged.json"
        self.output = self.root / "plugins"
        self.snapshot.write_text(json.dumps({"schemaVersion": 1, "plugins": [entry(aliases=["jsonrpc-converter"])]}))
        self.properties.write_text("json-converter=oci://registry.example/plugins/jsonrpc-converter:2.0.0\nbasic-auth=oci://registry.example/plugins/basic-auth:2.0.0\n")
        self.lock.write_text(json.dumps({"schemaVersion": 1, "plugins": {"basic-auth": lock_item()}}))
        self.calls = []
        self.original_copy = pull_plugins.copy_oci_wasm

        def fake_copy(repository, digest, target):
            self.calls.append((repository, digest, pathlib.Path(target)))
            pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(target).write_bytes(WASM)
            return WASM_HASH
        pull_plugins.copy_oci_wasm = fake_copy

    def tearDown(self):
        pull_plugins.copy_oci_wasm = self.original_copy
        self.temp.cleanup()

    def materialize(self):
        pull_plugins.materialize_snapshot(str(self.snapshot), str(self.output), str(self.properties), str(self.lock))

    def test_managed_replacement_unmanaged_survival_and_alias_paths(self):
        self.materialize()
        self.assertEqual((self.output / "json-converter" / "2.0.0" / "plugin.wasm").read_bytes(), WASM)
        self.assertEqual((self.output / "jsonrpc-converter" / "2.0.0" / "plugin.wasm").read_bytes(), WASM)
        self.assertEqual((self.output / "basic-auth" / "2.0.0" / "plugin.wasm").read_bytes(), WASM)
        self.assertEqual(self.calls[0][:2], ("registry.example/plugins/jsonrpc-converter", DIGEST))
        self.assertEqual(self.calls[1][:2], ("registry.example/plugins/basic-auth", DIGEST))
        inventory = json.loads((self.output / "snapshot-inventory.json").read_text())
        self.assertIn("json-converter", inventory)
        self.assertIn("jsonrpc-converter", inventory)
        self.assertNotIn("basic-auth", inventory)
        expected = {"version": "2.0.0", "digest": DIGEST, "wasmSha256": WASM_HASH}
        self.assertEqual(inventory, {"json-converter": expected, "jsonrpc-converter": expected})

    def test_missing_extra_and_drifting_lock_fail_before_download(self):
        for plugins, expected in [({}, "missing"), ({"basic-auth": lock_item(), "extra": lock_item("extra")}, "extra")]:
            self.lock.write_text(json.dumps({"schemaVersion": 1, "plugins": plugins}))
            with self.assertRaisesRegex(ValueError, expected):
                self.materialize()
        item = lock_item(); item["tag"] = "1.0.0"
        self.lock.write_text(json.dumps({"schemaVersion": 1, "plugins": {"basic-auth": item}}))
        with self.assertRaisesRegex(ValueError, "drifts"):
            self.materialize()

    def test_unmanaged_wasm_digest_mismatch_propagates(self):
        def mismatch(repository, digest, target):
            pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(target).write_bytes(WASM)
            return "b" * 64 if repository.endswith("/basic-auth") else WASM_HASH
        pull_plugins.copy_oci_wasm = mismatch
        with self.assertRaisesRegex(ValueError, "Wasm hash"):
            self.materialize()
        self.assertFalse((self.output / "snapshot-inventory.json").exists())


HOTFIX_IDS = ("basic-auth", "bot-detect", "custom-response", "hmac-auth", "jwt-auth", "key-auth", "key-rate-limit", "oauth", "request-block")


def synthetic_artifact(name, version):
    payload = WASM + (name + "@" + version).encode()
    return {
        "servedVersion": version,
        "repository": "registry.example/plugins/" + name,
        "tag": version,
        "digest": "sha256:" + hashlib.sha256(b"manifest:" + payload).hexdigest(),
        "wasmSha256": hashlib.sha256(payload).hexdigest(),
    }, payload


def managed_artifact(version):
    artifact, payload = synthetic_artifact("jsonrpc-converter", version)
    return {
        "version": version,
        "ociRef": artifact["repository"] + ":" + version,
        "digest": artifact["digest"],
        "wasmSha256": artifact["wasmSha256"],
        "provenanceMode": "public",
    }, payload


class ManagedHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.snapshot = self.root / "snapshot.json"
        self.properties = self.root / "plugins.properties"
        self.lock_path = self.root / "unmanaged.json"
        self.output = self.root / "plugins"
        self.managed_entry = entry(aliases=["jsonrpc-converter"])
        self.managed_entry["consumers"]["pluginServer"]["httpPath"] = "converter-http"
        self.payloads = {("registry.example/plugins/basic-auth", DIGEST): WASM}
        artifacts = []
        for version in ("2.0.0", "3.0.0", "1.0.0"):
            artifact, payload = managed_artifact(version)
            artifacts.append(artifact)
            self.payloads[(artifact["ociRef"].rsplit(":", 1)[0], artifact["digest"])] = payload
        self.managed_entry.update({key: value for key, value in artifacts[0].items() if key != "provenanceMode"})
        self.managed_entry["history"] = artifacts[1:]
        self.properties.write_text("json-converter=oci://registry.example/plugins/jsonrpc-converter:2.0.0\nbasic-auth=oci://registry.example/plugins/basic-auth:2.0.0\n")
        self.lock = {"basic-auth": lock_item()}
        self.write_lock()
        self.write_snapshot()
        self.calls = []
        self.original_copy = pull_plugins.copy_oci_wasm

        def fake_copy(repository, digest, target):
            self.calls.append((repository, digest, pathlib.Path(target)))
            payload = self.payloads[(repository, digest)]
            pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(target).write_bytes(payload)
            return hashlib.sha256(payload).hexdigest()
        self.fake_copy = fake_copy
        pull_plugins.copy_oci_wasm = fake_copy

    def tearDown(self):
        pull_plugins.copy_oci_wasm = self.original_copy
        self.temp.cleanup()

    def write_snapshot(self, plugins=None):
        self.snapshot.write_text(json.dumps({"schemaVersion": 1, "plugins": plugins if plugins is not None else [self.managed_entry]}))

    def write_lock(self):
        self.lock_path.write_text(json.dumps({"schemaVersion": 1, "plugins": self.lock}))

    def materialize(self):
        pull_plugins.materialize_snapshot(str(self.snapshot), str(self.output), str(self.properties), str(self.lock_path))

    def assert_rejected_before_download(self, plugins=None):
        self.write_snapshot(plugins)
        with self.assertRaises(ValueError):
            self.materialize()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.output / "snapshot-inventory.json").exists())

    def test_materializes_current_and_two_history_versions_in_snapshot_order(self):
        self.materialize()
        expected_records = []
        for artifact in [self.managed_entry] + self.managed_entry["history"]:
            payload = self.payloads[(artifact["ociRef"].rsplit(":", 1)[0], artifact["digest"])]
            for path in ("converter-http", "json-converter", "jsonrpc-converter"):
                target = self.output / path / artifact["version"] / "plugin.wasm"
                self.assertEqual(target.read_bytes(), payload)
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), artifact["wasmSha256"])
            expected_records.append({field: artifact[field] for field in ("version", "digest", "wasmSha256")})
        expected = dict(expected_records[0], history=expected_records[1:])
        inventory = json.loads((self.output / "snapshot-inventory.json").read_text())
        self.assertEqual(inventory, {path: expected for path in ("converter-http", "json-converter", "jsonrpc-converter")})
        self.assertEqual([record["version"] for record in expected["history"]], ["3.0.0", "1.0.0"])
        self.assertEqual([call[:2] for call in self.calls], [
            (artifact["ociRef"].rsplit(":", 1)[0], artifact["digest"])
            for artifact in [self.managed_entry] + self.managed_entry["history"]
        ] + [("registry.example/plugins/basic-auth", DIGEST)])

    def test_absent_and_empty_history_keep_current_only_inventory(self):
        for case in ("absent", "empty"):
            with self.subTest(case=case):
                self.output = self.root / case
                self.managed_entry.pop("history", None)
                if case == "empty":
                    self.managed_entry["history"] = []
                self.write_snapshot()
                self.materialize()
                inventory = json.loads((self.output / "snapshot-inventory.json").read_text())
                expected = {field: self.managed_entry[field] for field in ("version", "digest", "wasmSha256")}
                self.assertEqual(inventory, {path: expected for path in ("converter-http", "json-converter", "jsonrpc-converter")})

    def test_current_wasm_hash_is_optional(self):
        del self.managed_entry["wasmSha256"]
        self.write_snapshot()
        self.materialize()
        inventory = json.loads((self.output / "snapshot-inventory.json").read_text())
        payload = self.payloads[("registry.example/plugins/jsonrpc-converter", self.managed_entry["digest"])]
        self.assertEqual(inventory["json-converter"]["wasmSha256"], hashlib.sha256(payload).hexdigest())

    def test_current_wasm_hash_mismatch_does_not_write_inventory(self):
        self.managed_entry["wasmSha256"] = "b" * 64
        self.write_snapshot()
        with self.assertRaisesRegex(ValueError, "Wasm hash"):
            self.materialize()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.output / "snapshot-inventory.json").exists())

    def test_invalid_current_hashes_fail_before_download(self):
        for value in (None, 123, [], {}, "", "a" * 63, "A" * 64, DIGEST):
            with self.subTest(value=value):
                self.managed_entry["wasmSha256"] = value
                self.assert_rejected_before_download()

    def test_invalid_history_lists_and_entries_fail_before_download(self):
        for value in (None, {}, "history", 1, [None], [[]], ["artifact"], [1]):
            with self.subTest(history=value):
                self.managed_entry["history"] = value
                self.assert_rejected_before_download()

    def test_history_missing_required_fields_fail_before_download(self):
        original = copy.deepcopy(self.managed_entry)
        for field in ("version", "ociRef", "digest", "wasmSha256", "provenanceMode"):
            with self.subTest(field=field):
                self.managed_entry = copy.deepcopy(original)
                del self.managed_entry["history"][-1][field]
                self.assert_rejected_before_download()

    def test_invalid_history_identities_fail_before_download(self):
        original = copy.deepcopy(self.managed_entry)
        cases = {
            "unknown-field": {"servedPath": "override"},
            "current-version": {"version": "2.0.0", "ociRef": original["ociRef"]},
            "duplicate-version": {"version": "3.0.0", "ociRef": original["history"][0]["ociRef"]},
            "current-oci-identity": {"digest": original["digest"]},
            "duplicate-oci-identity": {"digest": original["history"][0]["digest"]},
            "latest": {"version": "latest", "ociRef": "registry.example/plugins/jsonrpc-converter:latest"},
            "traversal": {"version": "../escape", "ociRef": "registry.example/plugins/jsonrpc-converter:../escape"},
            "backslash": {"version": "..\\escape"},
            "nul": {"version": "bad\x00version"},
            "dot": {"version": "."},
            "version-type": {"version": []},
            "tag-drift": {"ociRef": "registry.example/plugins/jsonrpc-converter:0.0.1"},
            "repository-drift": {"ociRef": "other.example/plugins/jsonrpc-converter:1.0.0"},
            "image-drift": {"ociRef": "registry.example/plugins/other:1.0.0"},
            "reference-type": {"ociRef": {}},
            "digest-reference": {"ociRef": "registry.example/plugins/jsonrpc-converter@" + DIGEST},
            "bare-digest": {"digest": "a" * 64},
            "digest-length": {"digest": "sha256:" + "a" * 63},
            "digest-uppercase": {"digest": "sha256:" + "A" * 64},
            "digest-type": {"digest": None},
            "oci-digest-as-wasm-hash": {"wasmSha256": DIGEST},
            "hash-length": {"wasmSha256": "a" * 63},
            "hash-uppercase": {"wasmSha256": "A" * 64},
            "hash-type": {"wasmSha256": []},
            "provenance-mode": {"provenanceMode": "bootstrap-public"},
            "provenance-type": {"provenanceMode": None},
        }
        for case, changes in cases.items():
            with self.subTest(case=case):
                self.managed_entry = copy.deepcopy(original)
                self.managed_entry["history"][-1].update(changes)
                self.assert_rejected_before_download()
                print("T03 invalid managed history %s: copy_calls=0" % case)

    def candidate_history(self):
        artifact = self.managed_entry["history"][-1]
        artifact.update({
            "provenanceMode": "candidate", "sourceCommit": "c" * 40, "inputHash": "sha256:" + "d" * 64,
            "candidateRef": "registry.example/candidates/jsonrpc-converter:1.0.0@" + artifact["digest"],
        })
        return artifact

    def test_candidate_history_accepts_optional_strict_import_reference(self):
        artifact = self.candidate_history()
        for imported in (False, True):
            with self.subTest(imported=imported):
                self.output = self.root / ("imported" if imported else "candidate")
                if imported:
                    artifact["candidateImport"] = {"path": "plugins/release/import.json", "sha256": "e" * 64}
                self.write_snapshot()
                self.materialize()
                inventory = json.loads((self.output / "snapshot-inventory.json").read_text())
                self.assertEqual(inventory["json-converter"]["history"][-1], {field: artifact[field] for field in ("version", "digest", "wasmSha256")})

    def test_public_history_rejects_candidate_provenance(self):
        original = copy.deepcopy(self.managed_entry)
        for field, value in {"sourceCommit": "c" * 40, "inputHash": DIGEST, "candidateRef": "ref", "candidateImport": None}.items():
            with self.subTest(field=field):
                self.managed_entry = copy.deepcopy(original)
                self.managed_entry["history"][-1][field] = value
                self.assert_rejected_before_download()

    def test_candidate_history_rejects_missing_or_invalid_provenance(self):
        self.candidate_history()
        original = copy.deepcopy(self.managed_entry)
        for field in ("sourceCommit", "inputHash", "candidateRef"):
            with self.subTest(missing=field):
                self.managed_entry = copy.deepcopy(original)
                del self.managed_entry["history"][-1][field]
                self.assert_rejected_before_download()
        for field, values in {
            "sourceCommit": (None, "c" * 39, "C" * 40),
            "inputHash": (None, "d" * 64, "sha256:" + "D" * 64),
            "candidateRef": (None, "registry.example/candidates/plugin:1.0.0", "registry.example/candidates/plugin:1.0.0@" + DIGEST,
                             "registry.example/candidates/plugin:latest@" + original["history"][-1]["digest"]),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.managed_entry = copy.deepcopy(original)
                    self.managed_entry["history"][-1][field] = value
                    self.assert_rejected_before_download()

    def test_candidate_history_rejects_invalid_import_reference(self):
        self.candidate_history()
        original = copy.deepcopy(self.managed_entry)
        proofs = [None, [], {}, {"path": "proof.json"}, {"sha256": "e" * 64},
                  {"path": "proof.json", "sha256": "e" * 64, "extra": True}]
        proofs += [{"path": path, "sha256": "e" * 64} for path in (None, "", "/proof.json", "../proof.json", "proof/../file.json", "proof\\file.json", "proof\x00.json")]
        proofs += [{"path": "proof.json", "sha256": value} for value in (None, DIGEST, "E" * 64)]
        for proof in proofs:
            with self.subTest(proof=proof):
                self.managed_entry = copy.deepcopy(original)
                self.managed_entry["history"][-1]["candidateImport"] = proof
                self.assert_rejected_before_download()

    def test_history_does_not_require_semver_or_temporal_order(self):
        self.managed_entry["history"][0].update({"version": "release_older", "ociRef": "registry.example/plugins/jsonrpc-converter:release_older"})
        self.write_snapshot()
        self.materialize()
        self.assertTrue((self.output / "converter-http" / "release_older" / "plugin.wasm").exists())

    def test_managed_path_collisions_fail_before_download(self):
        for field in ("httpPath", "inventoryKey", "aliases"):
            for historical in (False, True):
                with self.subTest(field=field, historical=historical):
                    other = entry("other")
                    other["version"] = "3.0.0"
                    other["ociRef"] = "registry.example/plugins/jsonrpc-converter:3.0.0"
                    if historical:
                        other["version"] = "4.0.0"
                        other["ociRef"] = "registry.example/plugins/jsonrpc-converter:4.0.0"
                        other["history"] = [copy.deepcopy(self.managed_entry["history"][0])]
                    other["consumers"]["pluginServer"][field] = ["converter-http"] if field == "aliases" else "converter-http"
                    self.assert_rejected_before_download([self.managed_entry, other])

    def test_managed_history_collides_with_unmanaged_current_and_history(self):
        original = copy.deepcopy(self.managed_entry)
        for historical in (False, True):
            with self.subTest(unmanaged_history=historical):
                self.managed_entry = copy.deepcopy(original)
                self.managed_entry.update({"version": "4.0.0", "ociRef": "registry.example/plugins/jsonrpc-converter:4.0.0"})
                self.managed_entry["consumers"]["pluginServer"]["httpPath"] = "basic-auth"
                if historical:
                    old, _ = synthetic_artifact("basic-auth", "1.0.0")
                    self.lock["basic-auth"]["history"] = [old]
                else:
                    self.managed_entry["history"][0].update({"version": "2.0.0", "ociRef": original["ociRef"]})
                self.write_lock()
                self.write_snapshot()
                with self.assertRaisesRegex(ValueError, "collide"):
                    self.materialize()
                self.assertEqual(self.calls, [])

    def test_managed_key_remaining_in_lock_is_rejected(self):
        self.lock["json-converter"] = lock_item("json-converter")
        self.write_lock()
        self.assert_rejected_before_download()

    def test_unsafe_paths_and_current_image_drift_fail_before_download(self):
        original = copy.deepcopy(self.managed_entry)
        for field, value in (("httpPath", "../escape"), ("inventoryKey", "../escape"), ("aliases", ["../escape"])):
            with self.subTest(field=field):
                self.managed_entry = copy.deepcopy(original)
                self.managed_entry["consumers"]["pluginServer"][field] = value
                self.assert_rejected_before_download()
        for image in ("plugins/other", "../plugins/jsonrpc-converter", 1):
            with self.subTest(image=image):
                self.managed_entry = copy.deepcopy(original)
                self.managed_entry["image"] = image
                self.assert_rejected_before_download()

    def test_duplicate_mapping_names_for_one_artifact_are_folded(self):
        self.managed_entry["consumers"]["pluginServer"].update({"httpPath": "json-converter", "aliases": ["json-converter", "jsonrpc-converter", "jsonrpc-converter"]})
        self.write_snapshot()
        self.materialize()
        self.assertEqual(len(self.calls), 4)
        inventory = json.loads((self.output / "snapshot-inventory.json").read_text())
        self.assertEqual(set(inventory), {"json-converter", "jsonrpc-converter"})

    def test_history_wasm_hash_mismatch_does_not_write_inventory(self):
        self.managed_entry["history"][-1]["wasmSha256"] = "b" * 64
        self.write_snapshot()
        with self.assertRaisesRegex(ValueError, "Wasm hash"):
            self.materialize()
        self.assertEqual(len(self.calls), 3)
        self.assertFalse((self.output / "snapshot-inventory.json").exists())

    def test_missing_invalid_or_failed_history_bytes_do_not_write_inventory(self):
        for case in ("missing", "non-wasm", "truncated", "returned-hash", "blob-error"):
            with self.subTest(case=case):
                self.output = self.root / case
                self.calls.clear()

                def broken_copy(repository, digest, target):
                    if digest != self.managed_entry["history"][0]["digest"]:
                        return self.fake_copy(repository, digest, target)
                    self.calls.append((repository, digest, pathlib.Path(target)))
                    if case == "blob-error":
                        raise subprocess.CalledProcessError(1, ["oras", "cp"])
                    if case != "missing":
                        pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
                        pathlib.Path(target).write_bytes(b"not-wasm" if case == "non-wasm" else b"\x00asm" if case == "truncated" else WASM)
                    return "b" * 64
                pull_plugins.copy_oci_wasm = broken_copy
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.materialize()
                self.assertEqual(len(self.calls), 2)
                self.assertFalse((self.output / "snapshot-inventory.json").exists())

    def test_shared_managed_oci_identity_is_downloaded_once(self):
        other = entry("other")
        other.update({field: self.managed_entry[field] for field in ("version", "ociRef", "digest", "wasmSha256")})
        self.write_snapshot([self.managed_entry, other])
        self.materialize()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual((self.output / "other" / "2.0.0" / "plugin.wasm").read_bytes(),
                         (self.output / "converter-http" / "2.0.0" / "plugin.wasm").read_bytes())

    def test_cached_artifact_still_checks_each_expected_wasm_hash(self):
        other = entry("other")
        other.update({field: self.managed_entry[field] for field in ("version", "ociRef", "digest")})
        other["wasmSha256"] = "b" * 64
        self.write_snapshot([self.managed_entry, other])
        with self.assertRaisesRegex(ValueError, "Wasm hash"):
            self.materialize()
        self.assertEqual(len(self.calls), 3)
        self.assertFalse((self.output / "snapshot-inventory.json").exists())


class UnmanagedHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.snapshot = self.root / "snapshot.json"
        self.properties = self.root / "plugins.properties"
        self.lock_path = self.root / "unmanaged.json"
        self.output = self.root / "plugins"
        self.managed_entry = entry()
        self.snapshot.write_text(json.dumps({"schemaVersion": 1, "plugins": [self.managed_entry]}))
        properties = ["json-converter=oci://registry.example/plugins/jsonrpc-converter:2.0.0"]
        properties += [name + "=oci://registry.example/plugins/" + name + ":2.0.1" for name in HOTFIX_IDS]
        self.properties.write_text("\n".join(properties) + "\n")
        self.payloads = {}
        self.lock = {}
        for name in HOTFIX_IDS:
            current, current_payload = synthetic_artifact(name, "2.0.1")
            history, history_payload = synthetic_artifact(name, "2.0.0")
            current.update({"logicalKey": name, "servedPath": name, "history": [history]})
            self.lock[name] = current
            self.payloads[(current["repository"], current["digest"])] = current_payload
            self.payloads[(history["repository"], history["digest"])] = history_payload
        self.write_lock()
        self.calls = []
        self.original_copy = pull_plugins.copy_oci_wasm

        def fake_copy(repository, digest, target):
            self.calls.append((repository, digest, pathlib.Path(target)))
            payload = self.payloads.get((repository, digest), WASM)
            pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(target).write_bytes(payload)
            return hashlib.sha256(payload).hexdigest()
        pull_plugins.copy_oci_wasm = fake_copy

    def tearDown(self):
        pull_plugins.copy_oci_wasm = self.original_copy
        self.temp.cleanup()

    def write_lock(self):
        self.lock_path.write_text(json.dumps({"schemaVersion": 1, "plugins": self.lock}))

    def materialize(self):
        pull_plugins.materialize_snapshot(str(self.snapshot), str(self.output), str(self.properties), str(self.lock_path))

    def test_materializes_eighteen_distinct_paths_with_exact_hash_oracles(self):
        self.materialize()
        unmanaged_calls = [call for call in self.calls if call[0] != "registry.example/plugins/jsonrpc-converter"]
        self.assertEqual(len(unmanaged_calls), 18)
        expected_calls = set()
        for name in HOTFIX_IDS:
            item = self.lock[name]
            for artifact in [item] + item["history"]:
                expected_calls.add((artifact["repository"], artifact["digest"]))
                target = self.output / name / artifact["servedVersion"] / "plugin.wasm"
                self.assertEqual(target.read_bytes(), self.payloads[(artifact["repository"], artifact["digest"])])
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), artifact["wasmSha256"])
        self.assertEqual({call[:2] for call in unmanaged_calls}, expected_calls)

    def test_invalid_history_fails_before_any_download(self):
        for case in ("duplicate", "latest", "traversal", "tag-drift", "bad-hash", "unknown-field", "tag-129", "tag-256", "tag-leading-dash", "tag-non-ascii"):
            with self.subTest(case=case):
                broken = copy.deepcopy(self.lock)
                old = broken["basic-auth"]["history"][0]
                if case == "duplicate":
                    old.update({"servedVersion": "2.0.1", "tag": "2.0.1"})
                elif case == "latest":
                    old.update({"servedVersion": "latest", "tag": "latest"})
                elif case == "traversal":
                    old.update({"servedVersion": "../escape", "tag": "../escape"})
                elif case == "tag-drift":
                    old["tag"] = "2.0.2"
                elif case in ("tag-129", "tag-256"):
                    length = int(case.split("-")[1])
                    old.update({"servedVersion": "a" * length, "tag": "a" * length})
                elif case == "tag-leading-dash":
                    old.update({"servedVersion": "-invalid", "tag": "-invalid"})
                elif case == "tag-non-ascii":
                    old.update({"servedVersion": "2.0.0-é", "tag": "2.0.0-é"})
                elif case == "bad-hash":
                    old["wasmSha256"] = "not-a-hash"
                else:
                    old["servedPath"] = "alias"
                self.lock_path.write_text(json.dumps({"schemaVersion": 1, "plugins": broken}))
                with self.assertRaises(ValueError):
                    self.materialize()
                self.assertEqual(self.calls, [])
                print("S01 invalid history %s: copy_calls=0" % case)

    def test_managed_unmanaged_path_collision_fails_before_any_download(self):
        self.managed_entry["version"] = "2.0.1"
        self.managed_entry["ociRef"] = "registry.example/plugins/jsonrpc-converter:2.0.1"
        self.managed_entry["consumers"]["pluginServer"]["httpPath"] = "basic-auth"
        self.snapshot.write_text(json.dumps({"schemaVersion": 1, "plugins": [self.managed_entry]}))
        with self.assertRaisesRegex(ValueError, "collide"):
            self.materialize()
        self.assertEqual(self.calls, [])

    def test_invalid_current_tag_fails_before_any_download(self):
        invalid = "a" * 256
        broken = copy.deepcopy(self.lock)
        broken["basic-auth"].update({"servedVersion": invalid, "tag": invalid})
        self.lock_path.write_text(json.dumps({"schemaVersion": 1, "plugins": broken}))
        properties = self.properties.read_text().replace("basic-auth=oci://registry.example/plugins/basic-auth:2.0.1", "basic-auth=oci://registry.example/plugins/basic-auth:" + invalid)
        self.properties.write_text(properties)
        with self.assertRaisesRegex(ValueError, "repository:tag"):
            self.materialize()
        self.assertEqual(self.calls, [])
        print("S01 invalid current tag-256: copy_calls=0")

    def test_s01_before_after_256_character_history_tag(self):
        baseline = "8a550170345752d92b715f239f642083d8e9a772"
        legacy_source = subprocess.check_output(["git", "show", baseline + ":pull_plugins.py"], cwd=MODULE_PATH.parent)
        legacy_path = self.root / "legacy_pull_plugins.py"
        legacy_path.write_bytes(legacy_source)
        legacy_spec = importlib.util.spec_from_file_location("legacy_pull_plugins", legacy_path)
        legacy = importlib.util.module_from_spec(legacy_spec)
        legacy_spec.loader.exec_module(legacy)
        broken = copy.deepcopy(self.lock)
        old = broken["basic-auth"]["history"][0]
        old.update({"servedVersion": "a" * 256, "tag": "a" * 256})
        self.lock_path.write_text(json.dumps({"schemaVersion": 1, "plugins": broken}))
        legacy.copy_oci_wasm = pull_plugins.copy_oci_wasm
        with self.assertRaises(OSError):
            legacy.materialize_snapshot(str(self.snapshot), str(self.output), str(self.properties), str(self.lock_path))
        self.assertGreater(len(self.calls), 0)
        print("S01 RED old history tag-256: copy_calls=%d" % len(self.calls))
        self.calls.clear()
        with self.assertRaises(ValueError):
            self.materialize()
        self.assertEqual(self.calls, [])
        print("S01 GREEN new history tag-256: copy_calls=0")

    def test_oci_tag_ascii_boundaries(self):
        self.assertTrue(pull_plugins.valid_oci_tag("a" * 128))
        self.assertTrue(pull_plugins.valid_oci_tag("_valid.Tag-1"))
        for invalid in ("", "a" * 129, ".invalid", "-invalid", "é", "tag/escape"):
            self.assertFalse(pull_plugins.valid_oci_tag(invalid))

    def test_checked_in_cpp_200_identities_remain_available_for_history(self):
        expected = {
            "basic-auth": ("f6eca46f214f54737bb0fa9075cce88f8a35465373526a265a9a4f7e13809806", "6d16d0dc50aae8ba7e6f5b47f531dd4395ab64b3f424e3ab8ba74b9825f04258"),
            "bot-detect": ("b53f2fb3802419bbec5959eafeda9e3a70aa8e290bc323667ccfcfc8b045ab22", "b452cd5a41d7679acffbe255deee34ad20db6ad20ae67fe288777e73f08c7a07"),
            "custom-response": ("25fef1380fc8f22d0bcaeddc53ef1cf980946d150954c27b67a37675e86910eb", "6367d27db56f8d4a742c61ce9512faebbe715160658f1a708248b0d7f6229299"),
            "hmac-auth": ("0914d43d0a283f8670b9ee784cac96c09318c15a06b44ff2ed8055e5d421aa7a", "137ae603047beab51a614fb683d0581eb0aede22250003d9139fb39a5315adfe"),
            "jwt-auth": ("f080c7b2af4cef01009da5992732e01210a11a4535e2179eccc6f8281832160d", "a5f02f0f7036a4a8c167498c506d5e924f9ea1545d9b13e0b559963798e3f1ce"),
            "key-auth": ("c9cddda9b7dc36b8260afd10d12a089556a070dba28b0b17691e18efc0ca545c", "19ec6a090a2515bbb8df50bbf3786c056a217352647793f6a185fb381f4b4df9"),
            "key-rate-limit": ("367bd28d56034a987f3265b3ef3d84343820dcb7d70c5d44f97db9585e6b0bf4", "094192da73ede867eac11edf3f2bd2d4463d05d1cbfb1976f9b062ece809d998"),
            "oauth": ("05166e398b1c94d95b43eea8e4936e108b38deb2fc9871a3a856c3c23b18b36c", "99671c07ee2978dab95c17280321e608b4832eb251b1b63a95b39bf187b4f1ec"),
            "request-block": ("9ac8e3766a40a5415d0a41f282e8d040295804883917439d47235f5efe7c0db7", "d6f92128712592f456d3381169b128fa00ae971c7f34b1531dbf1f619c3d12bd"),
        }
        checked_in = json.loads((pathlib.Path(__file__).with_name("unmanaged-plugins.lock.json")).read_text())["plugins"]
        for name, hashes in expected.items():
            item = checked_in[name]
            candidates = [item] + item.get("history", [])
            old = [candidate for candidate in candidates if candidate["servedVersion"] == "2.0.0"]
            self.assertEqual(len(old), 1)
            self.assertEqual((old[0]["digest"], old[0]["wasmSha256"]), ("sha256:" + hashes[0], hashes[1]))


if __name__ == "__main__":
    unittest.main()
