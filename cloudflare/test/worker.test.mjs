import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";

const hooks = registerHooks({
  resolve(specifier, context, next) {
    if (specifier === "cloudflare:workers") {
      const stub = "export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }";
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(stub)}` };
    }
    return next(specifier, context);
  },
});
const { CaucheRuntime } = await import("../src/worker.mjs");
hooks.deregister();

function runtimeFixture(startedImage, running) {
  const calls = [];
  const port = {
    async fetch(url) {
      calls.push(String(url));
      return Response.json({ database: "OK", worker: "ONLINE" });
    },
  };
  const container = {
    running,
    images: { app: "current-digest" },
    start(options) {
      calls.push(["start", options.image]);
      this.running = true;
    },
    async setInactivityTimeout() { calls.push("inactivity"); },
    getTcpPort() { return port; },
  };
  const storage = {
    async get() { return startedImage; },
    async put(_key, image) { calls.push(["record", image]); },
  };
  return { calls, container, runtime: new CaucheRuntime({ container, storage }, {}) };
}

test("a mismatched or untracked running container gets no traffic or keepalive until it stops", async () => {
  for (const startedImage of ["old-digest", undefined]) {
    const { calls, container, runtime } = runtimeFixture(startedImage, true);
    await assert.rejects(runtime.keepWarm(), /image is draining/);
    await assert.rejects(runtime.fetch(new Request("https://test/")), /image is draining/);
    assert.deepEqual(calls, []);

    container.running = false;
    await runtime.keepWarm();
    assert.deepEqual(calls.slice(0, 3), [
      ["record", "current-digest"], ["start", "current-digest"], "inactivity",
    ]);
  }
});

test("a warm container receives a health request on each scheduled invocation", async () => {
  const { calls, runtime } = runtimeFixture("current-digest", true);
  await runtime.keepWarm();
  const initialCalls = calls.length;
  await runtime.keepWarm();
  assert.deepEqual(calls.slice(initialCalls), ["http://container/api/health"]);
});
