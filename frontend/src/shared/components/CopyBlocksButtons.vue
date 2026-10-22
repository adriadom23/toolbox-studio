<template>
  <div class="copy-blocks-buttons" :class="buttonClass">
    <button
      v-for="(block, index) in blocks"
      :key="index"
      type="button"
      :class="buttonClasses"
      :disabled="disabled || !block.length"
      @click="copyBlock(block, index)"
    >
      {{ buttonLabel(block, index) }}
    </button>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref } from "vue";

const props = defineProps({
  items: { type: Array, default: () => [] },
  maxItems: { type: Number, default: 4999 },
  itemText: { type: Function, default: null },
  getText: { type: Function, default: null },
  label: { type: String, required: true },
  variant: { type: String, default: "ghost" },
  buttonClass: { type: [String, Array, Object], default: "" },
  disabled: { type: Boolean, default: false },
});

const emit = defineEmits(["copy", "copied", "error"]);

const copiedIndex = ref(-1);
let resetTimer = null;

const blockSize = computed(() => {
  const value = Math.floor(Number(props.maxItems));
  const requested = Number.isFinite(value) && value > 0 ? value : 4999;
  return Math.min(requested, 4999);
});

const blocks = computed(() => {
  const result = [];
  for (let index = 0; index < props.items.length; index += blockSize.value) {
    result.push(props.items.slice(index, index + blockSize.value));
  }
  return result.length ? result : [[]];
});

const buttonClasses = computed(() => [
  "tb-button",
  props.variant ? `tb-button--${props.variant}` : "",
  props.buttonClass,
]);

function buttonLabel(block, index) {
  const prefix = copiedIndex.value === index ? "✓ " : "";
  if (blocks.value.length <= 1) return `${prefix}${props.label}`;
  return `${prefix}${props.label} · ${index + 1}/${blocks.value.length} · ${block.length}`;
}

function textFor(item, index) {
  const mapper = props.getText || props.itemText;
  return mapper ? mapper(item, index) : String(item ?? "");
}

async function writeClipboard(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (_error) {
      // Fall through to the legacy clipboard path.
    }
  }

  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.left = "-9999px";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } finally {
    document.body.removeChild(area);
  }
  if (!copied) throw new Error("clipboard_copy_failed");
}

async function copyBlock(block, index) {
  const start = index * blockSize.value;
  const text = block.map((item, itemIndex) => textFor(item, start + itemIndex)).join("\n");
  const detail = {
    index,
    blockNumber: index + 1,
    totalBlocks: blocks.value.length,
    count: block.length,
    items: block,
    text,
  };
  emit("copy", detail);
  try {
    await writeClipboard(text);
    copiedIndex.value = index;
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => {
      copiedIndex.value = -1;
    }, 1800);
    emit("copied", detail);
  } catch (error) {
    emit("error", { ...detail, error });
  }
}

onBeforeUnmount(() => clearTimeout(resetTimer));
</script>

<style scoped>
.copy-blocks-buttons {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.copy-btn {
  min-height: 32px;
  font-size: 12px;
}

.tracking-copy-tab {
  flex: 0 0 auto;
  font-size: 12px;
  white-space: nowrap;
}

.enrich__copy,
.aa-enrich__copy {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 38px;
}

.dd-enrich-btn {
  margin-left: auto;
}

.tracking-box__copy {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
</style>
