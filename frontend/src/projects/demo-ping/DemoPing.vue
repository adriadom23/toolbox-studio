<template>
  <div class="tool-surface">
    <header class="tool-header">
      <h2>{{ toolTitle }}</h2>
      <p class="tool-subtitle">
        Scaffolded by Toolbox Studio. Replace this with your tool's UI.
      </p>
    </header>

    <!-- Example: the vendored connections pattern (useIdentity + IdentifyPanel).
         Delete this block if your tool doesn't need an external session. -->
    <CollapsedSection :title="$t('common.connectionsTitle') || 'Conexiones'" start-open>
      <IdentifyPanel :identity="identity" />
    </CollapsedSection>

    <!-- Example: a vendored building block (table sorting + a simple list). -->
    <section class="tool-panel">
      <table class="tb-table">
        <thead>
          <tr>
            <SortTh v-for="col in columns" :key="col.key" :label="col.label" />
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>{{ row.id }}</td>
            <td>{{ row.value }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length" class="tool-empty">No data yet — wire up {{ pingPath }}.</p>
    </section>

    <button class="tb-button tb-button--primary" @click="ping">Ping backend</button>
    <p v-if="pingResult">{{ pingResult }}</p>
  </div>
</template>

<script setup>
import { ref } from "vue";
import CollapsedSection from "../../shared/components/CollapsedSection.vue";
import IdentifyPanel from "../../shared/components/IdentifyPanel.vue";
import SortTh from "../../shared/components/SortTh.vue";
import { useIdentity } from "../../shared/stores/useIdentity";
import { request } from "../../shared/api/http";

const toolTitle = "DemoPing";
const pingPath = "/projects/demo-ping/app/ping";

// One vendored connection, as an example. Add more sources from
// shared/stores/globalConnections.js, or remove this entirely if your
// tool doesn't need an external session.
//
// NOTE: useIdentity() self-starts — it probes every source automatically
// on mount (and again on view re-activation) via its own internal
// onMounted(start) hook. You do NOT need to call anything in your own
// onMounted to kick off the first probe; useIdentity has no `probeAll()`
// method. If you want to force a fresh probe later (e.g. after a user
// action), call `identity.refreshAll()`.
//
// EXPECTED IN THIS DEMO: this "scc" source will show as "disconnected" on
// any machine without a live Amazon internal network + a real Firefox
// profile Midway/SSO session (e.g. this scaffold's own dev/CI sandbox).
// That is the vendored probe degrading gracefully, by design — not a bug
// in this project, not a bug in Toolbox Studio. On a developer's real
// workstation with a live Firefox session it reflects the real status.
const identity = useIdentity([{ id: "scc", label: "SCC" }]);

const columns = [
  { key: "id", label: "ID" },
  { key: "value", label: "Value" },
];
// Extension: demo-specific placeholder rows, so the table isn't empty when
// you open this project. Replace with your own data/fetch.
const rows = ref([
  { id: 1, value: "pong" },
  { id: 2, value: "hello from demo-ping" },
]);
const pingResult = ref("");

async function ping() {
  try {
    const data = await request(pingPath);
    pingResult.value = JSON.stringify(data);
  } catch (err) {
    pingResult.value = `Error: ${err.message}`;
  }
}
</script>

<style scoped>
.tool-surface {
  max-width: 1320px;
  margin: 0 auto;
  padding: 24px;
}
.tool-header h2 {
  color: var(--toolbox-text);
}
.tool-subtitle {
  color: var(--toolbox-muted);
}
</style>
