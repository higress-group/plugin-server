import hashlib
import importlib.util
import json
import pathlib
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


if __name__ == "__main__":
    unittest.main()
