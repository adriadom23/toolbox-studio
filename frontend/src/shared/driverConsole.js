// Deep-link to the AMZL drivers console ("amconsole"). Regla permanente del
// usuario (2026-08-13): todo driver / transporter id visible en la UI debe
// enlazar a https://logistics.amazon.es/amconsole/transporter/<driver id>
// (el id va pegado tras la barra final).
const BASE = "https://logistics.amazon.es/amconsole/transporter/";

export function driverConsoleUrl(driverId) {
  return `${BASE}${encodeURIComponent(String(driverId || "").trim())}`;
}

// Heurística: los transporter ids son alfanuméricos en mayúsculas que
// empiezan por "A" (p. ej. "A1BC2DEF3GH45"). Evita enlazar placeholders
// ("No DA", "—"), nombres de tienda o texto libre que comparten columna.
export function looksLikeDriverId(value) {
  return /^A[A-Z0-9]{6,}$/.test(String(value || "").trim().toUpperCase());
}

// Para tablas genéricas (columnas dinámicas): detecta si una cabecera es de
// driver / transporter id ("Driver ID", "transporter_id", "DA ID",
// "ID del transportista"…). Combinado con looksLikeDriverId por DriverLink,
// solo se enlazan valores que además parecen ids.
export function isDriverColumn(name) {
  const n = String(name || "").toLowerCase().replace(/[^a-z]/g, "");
  return (
    n.includes("driver") ||
    n.includes("transporter") ||
    n.includes("transportista") ||
    n === "da" ||
    n === "daid"
  );
}
