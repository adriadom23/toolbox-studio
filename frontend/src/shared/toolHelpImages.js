// Step-by-step help guides for the per-tool header "?" tooltips.
//
// Trimmed version of ToolBox's shared/toolHelpImages.js (see design.md
// §2.3/§2.4): same exported shape (``toolHelpGuides``, an object keyed by
// guide id, each value an ordered array of per-step image-path arrays),
// but with an EMPTY guide registry instead of ToolBox's 60+ tool-specific
// entries. HelpTooltip.vue (vendored) reads ``toolHelpGuides[props.guide]``
// and gracefully falls back to text-only when the key is missing — a
// Studio project adds its own entries here as it grows its own guides.
export const toolHelpGuides = {};
