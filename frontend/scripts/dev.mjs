import { spawn } from "node:child_process";
import { access } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HOST = "127.0.0.1";
const API_PORT = 5000;
const PREVIEW_PORT = 5001;
const VITE_PORT = 5173;
const API_URL = `http://${HOST}:${API_PORT}`;
const PREVIEW_URL = `http://${HOST}:${PREVIEW_PORT}`;
const VITE_URL = `http://${HOST}:${VITE_PORT}`;
const STARTUP_TIMEOUT_MS = 90_000;
const POLL_INTERVAL_MS = 500;

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(scriptDir, "..");
const rootDir = path.resolve(frontendDir, "..");
const backendDir = path.join(rootDir, "backend");
const pythonPath = path.join(rootDir, ".venv", "Scripts", "python.exe");
const vitePath = path.join(frontendDir, "node_modules", "vite", "bin", "vite.js");

const owned = [];
let shuttingDown = false;

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

async function responds(url, validate = () => true) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2_500);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) return false;
    return validate(response);
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

function spawnOwned(label, argv, options) {
  const child = spawn(argv[0], argv.slice(1), options);
  const service = { label, child, exit: null };
  owned.push(service);
  child.once("error", (error) => {
    service.exit = { error };
  });
  child.once("exit", (code, signal) => {
    service.exit = { code, signal };
    if (!shuttingDown) void fail(`${label} se cerró inesperadamente (${signal || `código ${code}`}).`);
  });
  return service;
}

async function waitForService(service, url, validate = () => true) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (service?.exit) {
      if (service.exit.error) throw new Error(`no se pudo iniciar ${service.label}: ${service.exit.error.message}`);
      throw new Error(`${service.label} terminó antes de estar listo.`);
    }
    if (await responds(url, validate)) return;
    await sleep(POLL_INTERVAL_MS);
  }
  throw new Error(`${service?.label || url} no estuvo listo en 90 segundos.`);
}

function waitForExit(child, timeoutMs = 5_000) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return Promise.race([new Promise((resolve) => child.once("exit", resolve)), sleep(timeoutMs)]);
}

async function terminateOwnedTree(child) {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32") {
    const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
    await waitForExit(killer);
  } else {
    child.kill("SIGTERM");
  }
  await waitForExit(child);
}

async function shutdown(exitCode) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const service of [...owned].reverse()) await terminateOwnedTree(service.child);
  process.exit(exitCode);
}

async function fail(message) {
  console.error(`[dev] Error: ${message}`);
  await shutdown(1);
}

async function ensureExternalOrStart({ label, port, readyUrl, validate, argv, options }) {
  if (await isPortOpen(port)) {
    if (!(await responds(readyUrl, validate))) throw new Error(`el puerto ${port} está ocupado por un servicio que no es ${label}.`);
    console.log(`[dev] ${label} reutilizado en ${readyUrl}`);
    return null;
  }
  console.log(`[dev] Iniciando ${label}...`);
  const service = spawnOwned(label, argv, options);
  await waitForService(service, readyUrl, validate);
  console.log(`[dev] ${label} listo en ${readyUrl}`);
  return service;
}

function viteEnvironment() {
  const warningOption = "--disable-warning=DEP0060";
  const currentOptions = (process.env.NODE_OPTIONS || "").trim();
  return {
    ...process.env,
    NODE_OPTIONS: currentOptions ? `${currentOptions} ${warningOption}` : warningOption,
    VITE_API_PROXY_TARGET: API_URL,
  };
}

async function main() {
  await requireFile(pythonPath, "El intérprete del entorno virtual");
  await requireFile(path.join(backendDir, "app", "asgi.py"), "La entrada ASGI");
  await requireFile(path.join(backendDir, "app", "preview_main.py"), "La entrada de preview");
  await requireFile(vitePath, "El binario local de Vite");

  await ensureExternalOrStart({
    label: "API",
    port: API_PORT,
    readyUrl: `${API_URL}/api/ready`,
    validate: async (response) => (await response.json()).status === "ready",
    argv: [pythonPath, "-m", "uvicorn", "app.asgi:app", "--host", HOST, "--port", String(API_PORT), "--no-proxy-headers"],
    options: { cwd: backendDir, env: process.env, stdio: "inherit" },
  });
  await ensureExternalOrStart({
    label: "preview",
    port: PREVIEW_PORT,
    readyUrl: `${PREVIEW_URL}/bootstrap`,
    argv: [pythonPath, "-m", "app.preview_main"],
    options: { cwd: backendDir, env: process.env, stdio: "inherit" },
  });

  if (await isPortOpen(VITE_PORT)) throw new Error(`el puerto ${VITE_PORT} está ocupado. Libéralo y vuelve a intentarlo.`);
  const vite = spawnOwned("Vite", [process.execPath, vitePath, "--host", HOST, "--port", String(VITE_PORT), "--strictPort"], {
    cwd: frontendDir,
    env: viteEnvironment(),
    stdio: "inherit",
  });
  await waitForService(vite, VITE_URL);
  console.log(`[dev] Studio listo en ${VITE_URL} (API ${API_URL}, preview ${PREVIEW_URL})`);
}

process.once("SIGINT", () => void shutdown(130));
process.once("SIGBREAK", () => void shutdown(130));
process.once("SIGTERM", () => void shutdown(143));
process.once("uncaughtException", (error) => void fail(error.message));
process.once("unhandledRejection", (reason) => void fail(reason instanceof Error ? reason.message : String(reason)));

main().catch((error) => void fail(error.message));
