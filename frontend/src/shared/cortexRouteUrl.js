const CORTEX_ROUTES_BASE = "https://logistics.amazon.co.uk/internal/operations/execution/dv/routes";

export function cortexRouteUrl({ planId, routeCode, date, serviceAreaId } = {}) {
  const plan = String(planId || "").trim();
  const route = String(routeCode || "").trim();
  const day = String(date || "").trim();
  const area = String(serviceAreaId || "").trim();
  if (!plan || !route || !/^\d{4}-\d{2}-\d{2}$/.test(day) || !area) return "";
  const params = new URLSearchParams({
    provider: "ALL_DRIVERS",
    selectedDay: day,
    serviceAreaId: area,
    transporter: "ALL_DRIVERS",
  });
  return `${CORTEX_ROUTES_BASE}/${encodeURIComponent(plan)}-${encodeURIComponent(route)}?${params.toString()}`;
}
