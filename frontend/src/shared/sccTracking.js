// Deep-link to the SCC package detail page. Every visible, actionable
// tracking id uses this canonical URL so encoding stays consistent.
const BASE =
  "https://logistics.amazon.co.uk/station/dashboard/search?shareableLink=detailPage%2F";

export function sccTrackingUrl(trackingId) {
  return `${BASE}${encodeURIComponent(String(trackingId || "").trim())}`;
}

// Conservative package-id detection. Besides TBA ids, SCC may expose long
// alphanumeric scannable ids. Driver ids and placeholders must stay plain.
export function looksLikeTrackingId(value) {
  const clean = String(value || "").trim().toUpperCase();
  if (!clean || ["—", "N/A", "NO DA"].includes(clean)) return false;
  if (/^A[A-Z0-9]{6,}$/.test(clean)) return false;
  return /^(TBA\d{9,}|[A-Z0-9]{12,})$/.test(clean);
}

// Dynamic tables combine this header check with looksLikeTrackingId through
// TrackingLink. Explicit exclusions prevent other operational identifiers
// (drivers, routes, VRIDs, logins, postcodes and QR payloads) from linking.
export function isTrackingColumn(name) {
  const normalized = String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  if (
    /(driver|transporter|transportista|associate|employee|route|ruta|vrid|login|postal|postcode|zipcode|qr|payload)/.test(
      normalized,
    )
  ) {
    return false;
  }
  return /(track|scannable|shipment|tba|barcode|package)/.test(normalized);
}
