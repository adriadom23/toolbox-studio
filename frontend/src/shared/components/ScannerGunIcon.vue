<template>
  <svg
    class="scanner-gun-icon"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.5"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <!-- Barrel / scan head -->
    <path d="M3 5.4h9v4H3z" />
    <!-- Pistol grip -->
    <path d="M6 9.4l-1.2 7.2a1.3 1.3 0 0 0 2.57.43L9 9.4" />
    <!-- Trigger -->
    <path d="M9 11.4h1.6" />
    <!-- Scan beam (dashed) -->
    <path d="M12 7.4h4.4" stroke-dasharray="1.4 1.4" />
    <!-- QR target -->
    <path d="M17.4 4.4h4.1v4.1h-4.1z" />
    <path d="M19 6h1v1h-1z" fill="currentColor" stroke="none" />
  </svg>
</template>

<script setup>
// Handheld QR/barcode scanner gun icon. Uses ``currentColor`` so it inherits
// the surrounding text colour.
</script>

<style scoped>
.scanner-gun-icon {
  width: 1em;
  height: 1em;
  display: inline-block;
  vertical-align: -0.14em;
}
</style>
