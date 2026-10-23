export const PREVIEW_ORIGIN = "http://127.0.0.1:5001";

const PREVIEW_PATH_RE = /^\/projects\/([^/]+)\/preview\/?$/;

export function matchPreviewPath(pathname) {
  const match = PREVIEW_PATH_RE.exec(pathname || "");
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch (_error) {
    return null;
  }
}

export function previewOrigin(bootstrapUrl) {
  try {
    return new URL(bootstrapUrl).origin;
  } catch (_error) {
    return PREVIEW_ORIGIN;
  }
}

export function postPreviewGrant(frameWindow, grant, subject) {
  if (!frameWindow || !grant?.nonce || !grant?.preview_id) return false;
  frameWindow.postMessage(
    {
      type: "toolbox-preview-grant",
      grant: {
        nonce: grant.nonce,
        preview_id: grant.preview_id,
        subject_kind: subject.kind,
        subject_id: subject.id,
      },
    },
    previewOrigin(grant.bootstrap_url),
  );
  return true;
}
