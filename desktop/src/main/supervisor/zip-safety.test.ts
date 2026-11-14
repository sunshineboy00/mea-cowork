import assert from "node:assert/strict";
import { test } from "node:test";

import { isSafeEntryPath, detectSingleRoot } from "./runtime-extractor";

test("isSafeEntryPath rejects absolute and traversal paths", () => {
  assert.equal(isSafeEntryPath("/etc/passwd"), false);
  assert.equal(isSafeEntryPath("../../etc/passwd"), false);
  assert.equal(isSafeEntryPath("..\\..\\windows"), false);
  assert.equal(isSafeEntryPath("a/../../b"), false);
});

test("isSafeEntryPath allows normal relative paths", () => {
  assert.equal(isSafeEntryPath("runtime/bin/python3"), true);
  assert.equal(isSafeEntryPath("packages/foo/bar.py"), true);
});

test("detectSingleRoot finds the MEA Cowork-<plat> wrapper", () => {
  assert.equal(
    detectSingleRoot(["64/launch.py", "64/runtime/bin/python3"]),
    "64",
  );
  assert.equal(detectSingleRoot(["launch.py", "runtime/bin/python3"]), null);
  assert.equal(detectSingleRoot(["a/x", "b/y"]), null);
});
