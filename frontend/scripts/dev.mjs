import { spawn } from "node:child_process";
import { access } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Thin dev-server launcher, mirroring ToolBox's own
// frontend/scripts/dev.mjs: boots the Flask backend (if not already
// running and healthy), then starts Vite on a fixed port with the API
// proxy wired to the backend. See ToolboxStudio/frontend/vite.config.js
// for the proxy target.

const HOST = "127.0.0.1";
const BACKEND_PORT = 5000;
const VITE_PORT = 5173;
const BACKEND_URL = `http://${HOST}:${BACKEND_PORT}`;
const STARTUP_TIMEOUT_MS = 90_000;
const REQUEST_TIMEOUT_MS = 2_500;
const POLL_INTERVAL_MS = 500;

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(scriptDir, "..");
const rootDir = path.resolve(frontendDir, "..");
const backendDir = path.join(rootDir, "backend");
const pythonPath = path.join(rootDir, ".venv", "Scripts", "python.exe");
const vitePath = path.join(frontendDir, "node_modules", "vite", "bin", "vite.js");

let backendChild = null;
let viteChild = null;
let shuttingDown = false;
let backendExit = null;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function requireFile(filePath, label) {
  try {
    await access(filePath);
  } catch {
    throw new Error(`${label} no encontrado: ${filePath}`);
  }
}

function isPortOpen(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: HOST, port });
    const finish = (open) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(open);
    };
    socket.setTimeout(1_000);
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

async function getJson(route) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`${BACKEND_URL}${route}`, {
      headers: { accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function backendIsHealthy() {
  const health = await getJson("/api/health");
  return health?.status === "ok";
}

function waitForExit(child, timeoutMs = 5_000) {
  if (!child || child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve();
  }
  return Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    sleep(timeoutMs),
  ]);
}

async function terminateOwnedTree(child) {
  if (!child?.pid) return;

  if (process.platform === "win32") {
    const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true,
    });
    await waitForExit(killer);
  } else if (child.exitCode === null && child.signalCode === null) {
    child.kill("SIGTERM");
  }
  await waitForExit(child);
}

async function shutdown(exitCode) {
  if (shuttingDown) return;
  shuttingDown = true;

  await terminateOwnedTree(viteChild);
  if (backendChild) await terminateOwnedTree(backendChild);
  process.exit(exitCode);
}

async function fail(message) {
  console.error(`[dev] Error: ${message}`);
  await shutdown(1);
}

function spawnBackend() {
  backendChild = spawn(pythonPath, ["-m", "app.main"], {
    cwd: backendDir,
    env: process.env,
    stdio: "inherit",
  });
  backendChild.once("error", (error) => {
    backendExit = { error };
  });
  backendChild.once("exit", (code, signal) => {
    backendExit = { code, signal };
    if (viteChild && !shuttingDown) {
      void fail(`el backend se cerró inesperadamente (${signal || `código ${code}`}).`);
    }
  });
}

async function waitForBackend() {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (backendExit) {
      if (backendExit.error) {
        throw new Error(`no se pudo iniciar Python: ${backendExit.error.message}`);
      }
      throw new Error(`el backend terminó antes de estar listo (${backendExit.signal || `código ${backendExit.code}`}).`);
    }
    if (await backendIsHealthy()) return;
    await sleep(POLL_INTERVAL_MS);
  }
  throw new Error("el backend no respondió como saludable en 90 segundos. Revisa su salida anterior.");
}

function viteEnvironment() {
  const warningOption = "--disable-warning=DEP0060";
  const currentOptions = (process.env.NODE_OPTIONS || "").trim();
  return {
    ...process.env,
    NODE_OPTIONS: currentOptions ? `${currentOptions} ${warningOption}` : warningOption,
    VITE_API_PROXY_TARGET: BACKEND_URL,
  };
}

function spawnVite() {
  viteChild = spawn(process.execPath, [vitePath, "--host", HOST, "--port", String(VITE_PORT), "--strictPort"], {
    cwd: frontendDir,
    env: viteEnvironment(),
    stdio: "inherit",
  });
  viteChild.once("error", (error) => {
    void fail(`no se pudo iniciar Vite: ${error.message}`);
  });
  viteChild.once("exit", (code, signal) => {
    if (!shuttingDown) {
      const exitCode = code === 0 || signal === "SIGINT" ? 0 : 1;
      void shutdown(exitCode);
    }
  });
}

async function main() {
  await requireFile(pythonPath, "El intérprete del entorno virtual");
  await requireFile(path.join(backendDir, "app", "main.py"), "La entrada del backend");
  await requireFile(vitePath, "El binario local de Vite");

  if (await isPortOpen(VITE_PORT)) {
    throw new Error("el puerto 5173 está ocupado. Libera ese puerto y vuelve a intentarlo.");
  }

  if (await isPortOpen(BACKEND_PORT)) {
    if (!(await backendIsHealthy())) {
      throw new Error("el puerto 5000 está ocupado por un servicio que no es un backend de Studio saludable. Libera ese puerto y vuelve a intentarlo.");
    }
    console.log("[dev] Backend saludable reutilizado en http://127.0.0.1:5000");
  } else {
    console.log("[dev] Iniciando backend...");
    spawnBackend();
    await waitForBackend();
    console.log("[dev] Backend listo en http://127.0.0.1:5000");
  }

  console.log("[dev] Iniciando Vite...");
  spawnVite();
}

process.once("SIGINT", () => void shutdown(130));
process.once("SIGBREAK", () => void shutdown(130));
process.once("SIGTERM", () => void shutdown(143));
process.once("uncaughtException", (error) => void fail(error.message));
process.once("unhandledRejection", (reason) => {
  const message = reason instanceof Error ? reason.message : String(reason);
  void fail(message);
});

main().catch((error) => void fail(error.message));
