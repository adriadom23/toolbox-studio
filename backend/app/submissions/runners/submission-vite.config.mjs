import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const frontendRoot = fs.realpathSync(path.resolve(process.env.TOOLBOX_FRONTEND_ROOT));
const snapshotRoot = fs.realpathSync(path.resolve(process.env.TOOLBOX_SNAPSHOT_ROOT));
const buildInput = fs.realpathSync(path.resolve(process.env.TOOLBOX_BUILD_INPUT));
const componentPath = fs.realpathSync(path.resolve(process.env.TOOLBOX_COMPONENT_PATH));
const nodeModulesRoot = fs.realpathSync(path.join(frontendRoot, "node_modules"));
const packageLock = JSON.parse(fs.readFileSync(path.join(frontendRoot, "package-lock.json"), "utf8"));
const requireFromFrontend = createRequire(path.join(frontendRoot, "package.json"));
const vue = requireFromFrontend("@vitejs/plugin-vue").default;
const allowedSourceRoots = [snapshotRoot, buildInput];
const allowedRoots = [...allowedSourceRoots, nodeModulesRoot];
const allowedVirtualIds = new Set(["\0vite/modulepreload-polyfill.js"]);

function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function assertNoSymlink(candidate, root) {
  let current = candidate;
  while (isWithin(root, current) && current !== root) {
    if (fs.lstatSync(current).isSymbolicLink()) throw new Error("forbidden symlink import");
    current = path.dirname(current);
  }
}

function checkedRealpath(candidate, roots) {
  const real = fs.realpathSync(candidate);
  const root = roots.find((item) => isWithin(item, real));
  if (!root) throw new Error("import escapes sealed roots");
  assertNoSymlink(real, root);
  return real;
}

function resolveFile(candidate) {
  const attempts = [candidate, `${candidate}.js`, `${candidate}.mjs`, `${candidate}.json`, `${candidate}.vue`, `${candidate}.css`];
  if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
    attempts.push(path.join(candidate, "index.js"), path.join(candidate, "index.vue"));
  }
  const match = attempts.find((item) => fs.existsSync(item) && fs.statSync(item).isFile());
  if (!match) throw new Error("import target is unavailable");
  return match;
}

function packageName(source) {
  const parts = source.split("/");
  return source.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0];
}

function assertLockedPackage(source) {
  const name = packageName(source);
  if (!packageLock.packages?.[`node_modules/${name}`]) throw new Error("package is not present in package-lock.json");
}

function controlledResolver() {
  return {
    name: "toolbox-controlled-resolver",
    enforce: "pre",
    resolveId(source, importer) {
      if (source === "vite" || source === "vite/modulepreload-polyfill") return null;
      if (source === "@toolbox-component") return componentPath;
      if (source.startsWith("\0")) {
        if (allowedVirtualIds.has(source) || source === "\0plugin-vue:export-helper" || source.startsWith("\0vite/") || source.startsWith("\0vite:")) return null;
        throw new Error("forbidden virtual import");
      }
      if (/^(?:https?:|file:|node:|\\\\|\/@fs\/)/i.test(source)) {
        throw new Error("forbidden absolute import");
      }

      const [specifier, query = ""] = source.split("?", 2);
      if (/^[A-Za-z]:[\\/]/.test(specifier) || (path.isAbsolute(specifier) && !specifier.startsWith("/"))) {
        const resolved = checkedRealpath(resolveFile(specifier), allowedRoots);
        return query ? `${resolved}?${query}` : resolved;
      }
      if (specifier.startsWith("/")) {
        const resolved = checkedRealpath(resolveFile(path.join(buildInput, specifier.slice(1))), allowedSourceRoots);
        return query ? `${resolved}?${query}` : resolved;
      }
      if (specifier.startsWith(".")) {
        if (!importer) throw new Error("relative import without importer");
        const importerPath = importer.split("?", 1)[0];
        const importerReal = checkedRealpath(importerPath, allowedRoots);
        const roots = isWithin(nodeModulesRoot, importerReal) ? [nodeModulesRoot] : allowedSourceRoots;
        const resolved = checkedRealpath(resolveFile(path.resolve(path.dirname(importerReal), specifier)), roots);
        return query ? `${resolved}?${query}` : resolved;
      }
      if (path.isAbsolute(specifier)) throw new Error("forbidden absolute import");
      assertLockedPackage(specifier);
      const resolved = checkedRealpath(requireFromFrontend.resolve(specifier), [nodeModulesRoot]);
      return query ? `${resolved}?${query}` : resolved;
    },
  };
}

export default {
  root: buildInput,
  plugins: [controlledResolver(), vue()],
  resolve: { dedupe: ["vue"] },
  server: { fs: { strict: true, allow: allowedRoots } },
  build: { emptyOutDir: false },
};
