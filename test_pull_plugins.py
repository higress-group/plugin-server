import hashlib
import importlib.util
import json
import pathlib
import copy
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
            return "b" * 64
        pull_plugins.copy_oci_wasm = mismatch
        with self.assertRaisesRegex(ValueError, "Wasm hash"):
            self.materialize()


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
        for case in ("duplicate", "latest", "traversal", "tag-drift", "bad-hash", "unknown-field"):
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
                elif case == "bad-hash":
                    old["wasmSha256"] = "not-a-hash"
                else:
                    old["servedPath"] = "alias"
                self.lock_path.write_text(json.dumps({"schemaVersion": 1, "plugins": broken}))
                with self.assertRaises(ValueError):
                    self.materialize()
                self.assertEqual(self.calls, [])

    def test_managed_unmanaged_path_collision_fails_before_any_download(self):
        self.managed_entry["version"] = "2.0.1"
        self.managed_entry["ociRef"] = "registry.example/plugins/jsonrpc-converter:2.0.1"
        self.managed_entry["consumers"]["pluginServer"]["httpPath"] = "basic-auth"
        self.snapshot.write_text(json.dumps({"schemaVersion": 1, "plugins": [self.managed_entry]}))
        with self.assertRaisesRegex(ValueError, "collide"):
            self.materialize()
        self.assertEqual(self.calls, [])

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
