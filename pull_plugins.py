#!/usr/bin/env python3
"""Materialize plugin-server content from legacy properties or an immutable snapshot.

Snapshot mode is deliberately strict: only entries with an explicit
pluginServer mapping are downloaded, each OCI manifest is addressed by digest,
and logical aliases are copied from the verified Wasm bytes. Legacy properties
remain supported only for the pre-existing unmanaged/default inventory.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile


WASM_MEDIA_TYPE = "application/vnd.module.wasm.content.layer.v1+wasm"
TAR_MEDIA_TYPES = {
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
    "application/vnd.oci.image.layer.v1.tar+gzip",
}
DIGEST_PREFIX = "sha256:"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid_wasm(path):
    if not os.path.isfile(path) or os.path.getsize(path) < 8:
        return False
    with open(path, "rb") as source:
        return source.read(4) == b"\x00asm"


def read_properties(path):
    properties = {}
    with open(path, encoding="utf-8") as source:
        for raw in source:
            line = raw.strip()
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                value = value.strip()
                properties[key.strip()] = value[6:] if value.startswith("oci://") else value
    return properties


def read_unmanaged_lock(path):
    with open(path, encoding="utf-8") as source:
        lock = json.load(source)
    if lock.get("schemaVersion") != 1 or not isinstance(lock.get("plugins"), dict):
        raise ValueError("unsupported unmanaged plugin lock schema")
    return lock["plugins"]


def parse_tag_reference(reference):
    repository, separator, tag = reference.rpartition(":")
    if not separator or "/" not in repository or not tag or "@" in reference:
        raise ValueError("properties reference must be a repository:tag")
    return repository, tag


def validate_unmanaged_lock(properties, managed, lock):
    unmanaged = set(properties) - managed
    if set(lock) != unmanaged:
        missing = sorted(unmanaged - set(lock))
        extra = sorted(set(lock) - unmanaged)
        raise ValueError("unmanaged lock inventory mismatch: missing=%s extra=%s" % (missing, extra))
    for name in sorted(unmanaged):
        item = lock[name]
        repository, tag = parse_tag_reference(properties[name])
        required = ("logicalKey", "servedPath", "servedVersion", "repository", "tag", "digest", "wasmSha256")
        if any(not item.get(field) for field in required):
            raise ValueError("unmanaged lock for %s is incomplete" % name)
        if item["logicalKey"] != name or item["servedPath"] != name or item["servedVersion"] != tag:
            raise ValueError("unmanaged lock for %s changes its served identity" % name)
        if item["repository"] != repository or item["tag"] != tag:
            raise ValueError("unmanaged lock for %s drifts from properties" % name)
        if not valid_digest(item["digest"]) or not valid_hash(item["wasmSha256"]):
            raise ValueError("unmanaged lock for %s has invalid immutable hashes" % name)


def valid_digest(value):
    return value.startswith(DIGEST_PREFIX) and len(value) == 71 and all(char in "0123456789abcdef" for char in value[len(DIGEST_PREFIX):])


def valid_hash(value):
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def safe_extract_wasm(archive, destination):
    with tarfile.open(archive, "r:gz") as bundle:
        candidates = [member for member in bundle.getmembers() if member.isfile() and member.name.endswith(".wasm")]
        if len(candidates) != 1:
            raise ValueError("OCI tar layer must contain exactly one Wasm file")
        source = bundle.extractfile(candidates[0])
        if source is None:
            raise ValueError("cannot read Wasm member")
        with open(destination, "wb") as target:
            shutil.copyfileobj(source, target)


def copy_oci_wasm(reference, expected_digest, destination):
    """Download an OCI manifest by digest and return the verified Wasm SHA-256."""
    if expected_digest and (not expected_digest.startswith(DIGEST_PREFIX) or len(expected_digest) != 71):
        raise ValueError("snapshot digest must be sha256:<64 lowercase hex characters>")
    if expected_digest and any(char not in "0123456789abcdef" for char in expected_digest[len(DIGEST_PREFIX):]):
        raise ValueError("snapshot digest must be lowercase hexadecimal")
    with tempfile.TemporaryDirectory(prefix="plugin-oci-") as layout:
        source = reference + "@" + expected_digest if expected_digest else reference
        subprocess.run(["oras", "cp", source, "--to-oci-layout", layout], check=True)
        with open(os.path.join(layout, "index.json"), encoding="utf-8") as source:
            manifests = json.load(source).get("manifests", [])
        if len(manifests) != 1 or (expected_digest and manifests[0].get("digest") != expected_digest):
            raise ValueError("OCI layout manifest digest does not equal snapshot digest")
        manifest_digest = manifests[0].get("digest", "")
        if not manifest_digest.startswith(DIGEST_PREFIX):
            raise ValueError("OCI layout manifest digest is missing")
        manifest_blob = os.path.join(layout, "blobs", "sha256", manifest_digest.split(":", 1)[1])
        with open(manifest_blob, encoding="utf-8") as source:
            manifest = json.load(source)
        layers = manifest.get("layers", [])
        wasm_layers = [layer for layer in layers if layer.get("mediaType") == WASM_MEDIA_TYPE]
        tar_layers = [layer for layer in layers if layer.get("mediaType") in TAR_MEDIA_TYPES]
        if len(wasm_layers) + len(tar_layers) != 1:
            raise ValueError("OCI manifest must contain exactly one supported Wasm layer")
        layer = (wasm_layers + tar_layers)[0]
        digest = layer.get("digest", "")
        if not digest.startswith(DIGEST_PREFIX):
            raise ValueError("OCI layer digest is missing")
        blob = os.path.join(layout, "blobs", "sha256", digest.split(":", 1)[1])
        if sha256_file(blob) != digest.split(":", 1)[1]:
            raise ValueError("OCI layer content digest mismatch")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if wasm_layers:
            shutil.copyfile(blob, destination)
        else:
            safe_extract_wasm(blob, destination)
    if not is_valid_wasm(destination):
        raise ValueError("extracted artifact is not a Wasm module")
    return sha256_file(destination)


def snapshot_entries(snapshot):
    if snapshot.get("schemaVersion") != 1:
        raise ValueError("unsupported snapshot schema")
    seen = set()
    for entry in snapshot.get("plugins", []):
        mapping = entry.get("consumers", {}).get("pluginServer")
        if not mapping:
            continue
        required = ("logicalId", "image", "version", "ociRef", "digest")
        if any(not entry.get(key) for key in required):
            raise ValueError("snapshot plugin lacks immutable identity")
        key = mapping.get("inventoryKey")
        path = mapping.get("httpPath")
        if not key or not path or key in seen:
            raise ValueError("snapshot plugin-server mapping is missing or ambiguous")
        seen.add(key)
        if "/" not in entry["ociRef"] or not entry["ociRef"].endswith(":" + entry["version"]):
            raise ValueError("snapshot OCI tag and version disagree")
        yield entry, mapping


def materialize_snapshot(snapshot_path, output, properties_path, lock_path):
    with open(snapshot_path, encoding="utf-8") as source:
        snapshot = json.load(source)
    written = {}
    for entry, mapping in snapshot_entries(snapshot):
        target = os.path.join(output, mapping["httpPath"], entry["version"], "plugin.wasm")
        content_hash = copy_oci_wasm(entry["ociRef"].rsplit(":", 1)[0], entry["digest"], target)
        for alias in [mapping["inventoryKey"]] + mapping.get("aliases", []):
            alias_target = os.path.join(output, alias, entry["version"], "plugin.wasm")
            if os.path.abspath(alias_target) != os.path.abspath(target):
                os.makedirs(os.path.dirname(alias_target), exist_ok=True)
                shutil.copyfile(target, alias_target)
            written[alias] = {"version": entry["version"], "digest": entry["digest"], "wasmSha256": content_hash}
    if not written:
        raise ValueError("snapshot has no plugin-server managed entries")
    with open(os.path.join(output, "snapshot-inventory.json"), "w", encoding="utf-8") as target:
        json.dump(dict(sorted(written.items())), target, indent=2, sort_keys=True)
        target.write("\n")
    # Preserve source-owned unmanaged C++/compatibility entries. Properties
    # retain their public served identity; only the sibling lock supplies the
    # immutable pull reference. Snapshot-managed keys are never duplicated.
    properties = read_properties(properties_path)
    lock = read_unmanaged_lock(lock_path)
    validate_unmanaged_lock(properties, set(written), lock)
    for name in sorted(lock):
        item = lock[name]
        target = os.path.join(output, item["servedPath"], item["servedVersion"], "plugin.wasm")
        content_hash = copy_oci_wasm(item["repository"], item["digest"], target)
        if content_hash != item["wasmSha256"]:
            raise ValueError("unmanaged plugin %s Wasm hash does not match source-owned lock" % name)


def materialize_legacy(properties_path, output):
    for name, reference in read_properties(properties_path).items():
        if "@" in reference:
            image, digest = reference.split("@", 1)
            version = digest
            copy_oci_wasm(image, digest, os.path.join(output, name, version, "plugin.wasm"))
        else:
            image, version = reference.rsplit(":", 1)
            # Legacy mode cannot claim immutable provenance; it is retained only
            # for local compatibility and must not be used by production builds.
            print("legacy properties mode is non-production", file=sys.stderr)
            copy_oci_wasm(reference, "", os.path.join(output, name, version, "plugin.wasm"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", help="canonical Higress snapshot JSON")
    parser.add_argument("--output", default="plugins", help="plugin root")
    parser.add_argument("--properties", default="plugins.properties", help="legacy inventory (not production)")
    parser.add_argument("--unmanaged-lock", default="unmanaged-plugins.lock.json", help="immutable lock for source-owned unmanaged inventory")
    args = parser.parse_args()
    try:
        if args.snapshot:
            materialize_snapshot(args.snapshot, args.output, args.properties, args.unmanaged_lock)
        else:
            materialize_legacy(args.properties, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print("plugin materialization failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
