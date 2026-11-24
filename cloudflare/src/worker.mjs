import { DurableObject } from "cloudflare:workers";
import { authorize, configured, containerRequest, denied, privateResponse } from "./admission.mjs";

const PORT = 8000;
const IDLE_TIMEOUT_MS = 5 * 60 * 1000;
const STARTUP_TIMEOUT_MS = 75 * 1000;
const STARTED_IMAGE_KEY = "started-image";

async function runtimeReady(port) {
  const probe = await port.fetch("http://container/api/health", {
    signal: AbortSignal.timeout(1500),
  });
  if (!probe.ok) {
    await probe.body?.cancel();
    return false;
  }
  const state = await probe.json();
  return state.database === "OK" && state.worker === "ONLINE";
}

export class CaucheRuntime extends DurableObject {
  starting;
  ready = false;

  async readyPort() {
    const container = this.ctx.container;
    if (!container.running) this.ready = false;
    if (!this.ready) {
      this.starting ??= this.waitUntilReady().finally(() => { this.starting = undefined; });
      await this.starting;
    }
    return container.getTcpPort(PORT);
  }

  async waitUntilReady() {
    const container = this.ctx.container;
    const image = container.images.app;
    if (!image) throw new Error("Runtime image is unavailable");
    if (container.running) {
      const startedImage = await this.ctx.storage.get(STARTED_IMAGE_KEY);
      if (startedImage !== image) throw new Error("Runtime image is draining");
    } else {
      const envVars = {
        APP_ORIGIN: this.env.APP_ORIGIN,
        CAUCHE_OWNER_EMAIL: this.env.CAUCHE_OWNER_EMAIL,
        DATABASE_URL: this.env.DATABASE_URL,
        CAUCHE_DB_CA_PEM: this.env.CAUCHE_DB_CA_PEM,
        CAUCHE_MASTER_KEY: this.env.CAUCHE_MASTER_KEY,
        LOG_RETENTION_DAYS: this.env.LOG_RETENTION_DAYS ?? "90",
      };
      if (this.env.DEEPSEEK_API_KEY) envVars.DEEPSEEK_API_KEY = this.env.DEEPSEEK_API_KEY;
      if (this.env.CAUCHE_BOOTSTRAP_PASSWORD) {
        envVars.CAUCHE_BOOTSTRAP_PASSWORD = this.env.CAUCHE_BOOTSTRAP_PASSWORD;
      }
      await this.ctx.storage.put(STARTED_IMAGE_KEY, image);
      container.start({
        image,
        instance: "standard-1",
        enableInternet: true,
        env: envVars,
      });
    }
    await container.setInactivityTimeout(IDLE_TIMEOUT_MS);
    const port = container.getTcpPort(PORT);
    const deadline = Date.now() + STARTUP_TIMEOUT_MS;
    while (Date.now() < deadline) {
      try {
        if (await runtimeReady(port)) {
          this.ready = true;
          return;
        }
      } catch {
        // Initialization may not yet have opened the application port.
      }
      await scheduler.wait(500);
    }
    throw new Error("Runtime readiness timed out");
  }

  async keepWarm() {
    const port = await this.readyPort();
    try {
      if (await runtimeReady(port)) return;
    } catch {
      // The provider could have restarted the container or the database.
    }
    this.ready = false;
    throw new Error("Runtime health probe failed");
  }

  async fetch(request) {
    const port = await this.readyPort();
    // Return the original upstream response so WebSocket upgrades remain intact.
    return port.fetch(containerRequest(request));
  }
}

function runtime(env) {
  return env.CAUCHE_RUNTIME.getByName("cauche-pulse");
}

function hasRuntimeSecrets(env) {
  return Boolean(env.DATABASE_URL && env.CAUCHE_DB_CA_PEM && env.CAUCHE_MASTER_KEY);
}

export default {
  async fetch(request, env, ctx) {
    const rejection = await authorize(request, env, ctx.access);
    if (rejection) return rejection;
    if (!hasRuntimeSecrets(env)) return denied(503);
    try {
      return privateResponse(await runtime(env).fetch(request), env.APP_ORIGIN);
    } catch {
      return denied(503);
    }
  },

  async scheduled(_event, env) {
    if (env.CAUCHE_CONTINUOUS !== "1" || !configured(env) || !hasRuntimeSecrets(env)) return;
    await runtime(env).keepWarm();
  },
};
