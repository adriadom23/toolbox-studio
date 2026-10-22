// Single source of truth for the Component Catalog (design.md §5.3).
//
// One entry PER vendored building block (34 components from §2.6), plus the
// useIdentity/globalConnections composable pair documented with its REAL,
// corrected API (refresh/refreshAll/identify/reconnect — NO probeAll, per
// §2.3/§5.3's closing note). ComponentCatalog.vue renders each entry's
// description, a copy-pasteable import+template snippet, and — where safe —
// a live instance with placeholder props.
//
// ``component`` holds the actual imported SFC so the catalog can mount a live
// instance; ``example`` is the literal snippet a developer copies into their
// own project file. ``liveProps`` (optional) are placeholder props/attrs the
// catalog binds on the live instance; ``liveSlot`` (optional) is placeholder
// default-slot text. Entries without a safe live instance set ``live:false``
// (still listed, still copy-pasteable).

import BarChart from "../shared/components/BarChart.vue";
import ChannelPickerModal from "../shared/components/ChannelPickerModal.vue";
import ChipListField from "../shared/components/ChipListField.vue";
import CollapsedSection from "../shared/components/CollapsedSection.vue";
import ConnStatusChips from "../shared/components/ConnStatusChips.vue";
import CopyBlocksButtons from "../shared/components/CopyBlocksButtons.vue";
import CortexRouteLink from "../shared/components/CortexRouteLink.vue";
import DataSourceCard from "../shared/components/DataSourceCard.vue";
import DriverLink from "../shared/components/DriverLink.vue";
import DspFilterPills from "../shared/components/DspFilterPills.vue";
import ExtractMiniButton from "../shared/components/ExtractMiniButton.vue";
import FieldInfoTooltip from "../shared/components/FieldInfoTooltip.vue";
import FileDropzone from "../shared/components/FileDropzone.vue";
import HelpTooltip from "../shared/components/HelpTooltip.vue";
import IdentifyButton from "../shared/components/IdentifyButton.vue";
import IdentifyPanel from "../shared/components/IdentifyPanel.vue";
import LanguageSwitcher from "../shared/components/LanguageSwitcher.vue";
import LoadingBar from "../shared/components/LoadingBar.vue";
import Modal from "../shared/components/Modal.vue";
import MultiLineChart from "../shared/components/MultiLineChart.vue";
import MultiSelect from "../shared/components/MultiSelect.vue";
import PieChart from "../shared/components/PieChart.vue";
import PlanManifestPanel from "../shared/components/PlanManifestPanel.vue";
import ScannerGunIcon from "../shared/components/ScannerGunIcon.vue";
import SortTh from "../shared/components/SortTh.vue";
import SparkLineChart from "../shared/components/SparkLineChart.vue";
import StemConnectionPanel from "../shared/components/StemConnectionPanel.vue";
import SvgLineChart from "../shared/components/SvgLineChart.vue";
import TextEditField from "../shared/components/TextEditField.vue";
import ThemeSwitcher from "../shared/components/ThemeSwitcher.vue";
import TokenChipInput from "../shared/components/TokenChipInput.vue";
import ToolboxFileStatus from "../shared/components/ToolboxFileStatus.vue";
import ToolPopupWindow from "../shared/components/ToolPopupWindow.vue";
import TrackingLink from "../shared/components/TrackingLink.vue";

export const componentCatalog = [
  {
    name: "BarChart",
    importPath: "../../shared/components/BarChart.vue",
    description: "Barras horizontales a partir de un array de filas {label, value}.",
    component: BarChart,
    live: true,
    liveProps: {
      rows: [
        { label: "A", value: 12 },
        { label: "B", value: 28 },
        { label: "C", value: 19 },
      ],
    },
    example:
      '<BarChart :rows="[{ label: \'A\', value: 12 }, { label: \'B\', value: 28 }]" />',
  },
  {
    name: "ChannelPickerModal",
    importPath: "../../shared/components/ChannelPickerModal.vue",
    description: "Modal para elegir canales/destinos de envío. Controlado por v-model.",
    component: ChannelPickerModal,
    live: false,
    example:
      '<ChannelPickerModal v-model="showPicker" :channels="channels" @confirm="onConfirm" />',
  },
  {
    name: "ChipListField",
    importPath: "../../shared/components/ChipListField.vue",
    description: "Editor de lista de valores como chips (modo single/multi).",
    component: ChipListField,
    live: true,
    liveProps: { modelValue: "alpha beta", title: "Valores" },
    example: '<ChipListField v-model="values" title="Valores" />',
  },
  {
    name: "CollapsedSection",
    importPath: "../../shared/components/CollapsedSection.vue",
    description: "Sección plegable con barra (título, badge, resumen, acciones).",
    component: CollapsedSection,
    live: true,
    liveProps: { label: "Paso 1 — Cargar datos", startOpen: true },
    liveSlot: "Contenido del paso.",
    example:
      '<CollapsedSection label="Paso 1 — Cargar datos" start-open>\n  <p>Contenido del paso.</p>\n</CollapsedSection>',
  },
  {
    name: "ConnStatusChips",
    importPath: "../../shared/components/ConnStatusChips.vue",
    description: "Chips de estado por fuente (conectado/desconectado), solo lectura.",
    component: ConnStatusChips,
    live: true,
    needsIdentity: true,
    example: '<ConnStatusChips :identity="identity" />',
  },
  {
    name: "CopyBlocksButtons",
    importPath: "../../shared/components/CopyBlocksButtons.vue",
    description: "Botón(es) para copiar una lista al portapapeles en bloques.",
    component: CopyBlocksButtons,
    live: true,
    liveProps: { items: ["TID-1", "TID-2", "TID-3"], label: "Copiar TIDs" },
    example:
      '<CopyBlocksButtons :items="trackingIds" label="Copiar TIDs" />',
  },
  {
    name: "CortexRouteLink",
    importPath: "../../shared/components/CortexRouteLink.vue",
    description: "Enlace a una ruta en Cortex a partir de su código.",
    component: CortexRouteLink,
    live: true,
    liveProps: { routeCode: "CX-123" },
    example: '<CortexRouteLink route-code="CX-123" />',
  },
  {
    name: "DataSourceCard",
    importPath: "../../shared/components/DataSourceCard.vue",
    description: "Tarjeta de una fuente de datos/sesión con estado y acción de login.",
    component: DataSourceCard,
    live: true,
    liveProps: { title: "SCC", ready: false },
    example: '<DataSourceCard title="SCC" :ready="connected" @login="onLogin" />',
  },
  {
    name: "DriverLink",
    importPath: "../../shared/components/DriverLink.vue",
    description: "Enlace a la consola de drivers a partir de un driver id.",
    component: DriverLink,
    live: true,
    liveProps: { id: "A1B2C3D4" },
    example: '<DriverLink :id="row.driverId" />',
  },
  {
    name: "DspFilterPills",
    importPath: "../../shared/components/DspFilterPills.vue",
    description: "Píldoras de filtro por DSP (selección única o múltiple).",
    component: DspFilterPills,
    live: true,
    liveProps: { dsps: ["CDOR", "WANKEL", "AVERY"], modelValue: "" },
    example: '<DspFilterPills :dsps="dsps" v-model="selected" />',
  },
  {
    name: "ExtractMiniButton",
    importPath: "../../shared/components/ExtractMiniButton.vue",
    description: "Botón compacto (solo icono) para disparar una extracción.",
    component: ExtractMiniButton,
    live: true,
    liveProps: { label: "Actualizar" },
    example: '<ExtractMiniButton label="Actualizar" @click="refresh" />',
  },
  {
    name: "FieldInfoTooltip",
    importPath: "../../shared/components/FieldInfoTooltip.vue",
    description: "Icono de ayuda (?) con tooltip explicando la lógica de un campo.",
    component: FieldInfoTooltip,
    live: true,
    liveProps: { title: "SPR", text: "Shipments per route." },
    example: '<FieldInfoTooltip title="SPR" text="Shipments per route." />',
  },
  {
    name: "FileDropzone",
    importPath: "../../shared/components/FileDropzone.vue",
    description: "Zona de arrastrar-soltar / seleccionar archivo, con estado cargado.",
    component: FileDropzone,
    live: true,
    liveProps: {
      title: "Plan Manifest",
      hint: "Suelta el CSV aquí o haz click para elegirlo.",
      selectedLabel: "",
    },
    example:
      '<FileDropzone title="Plan Manifest" hint="Suelta el CSV aquí" :selected-label="fileName" @file="onFile" />',
  },
  {
    name: "HelpTooltip",
    importPath: "../../shared/components/HelpTooltip.vue",
    description: "Tooltip de ayuda genérico anclado a un icono.",
    component: HelpTooltip,
    live: true,
    liveProps: { text: "Texto de ayuda para el usuario." },
    example: '<HelpTooltip text="Texto de ayuda para el usuario." />',
  },
  {
    name: "IdentifyButton",
    importPath: "../../shared/components/IdentifyButton.vue",
    description: "Botón único 'Reconectar' atado a un useIdentity() (reconecta lo caído).",
    component: IdentifyButton,
    live: true,
    needsIdentity: true,
    example: '<IdentifyButton :identity="identity" />',
  },
  {
    name: "IdentifyPanel",
    importPath: "../../shared/components/IdentifyPanel.vue",
    description:
      "Contenedor de 'Conexiones' con chips de estado, 'Reconectar todo' y (opcional) 'Extraer'. Espera un useIdentity() en :identity.",
    component: IdentifyPanel,
    live: true,
    needsIdentity: true,
    identityProps: { showStatus: true },
    example:
      '<IdentifyPanel :identity="identity" show-status>\n  <!-- opcional: tarjetas de fuente -->\n</IdentifyPanel>',
  },
  {
    name: "LanguageSwitcher",
    importPath: "../../shared/components/LanguageSwitcher.vue",
    description: "Selector de idioma (es/en/fr/de/it) atado al motor i18n compartido.",
    component: LanguageSwitcher,
    live: true,
    example: "<LanguageSwitcher />",
  },
  {
    name: "LoadingBar",
    importPath: "../../shared/components/LoadingBar.vue",
    description: "Barra de progreso (determinada con :percent o indeterminada).",
    component: LoadingBar,
    live: true,
    liveProps: { percent: 42, label: "Procesando" },
    example: '<LoadingBar :percent="progress" label="Procesando" />',
  },
  {
    name: "Modal",
    importPath: "../../shared/components/Modal.vue",
    description: "Modal accesible (teleport, focus-trap, Esc). Controlado por v-model.",
    component: Modal,
    live: true,
    isModal: true,
    liveProps: { title: "Título del modal" },
    liveSlot: "Cuerpo del modal.",
    example:
      '<Modal v-model="open" title="Título del modal">\n  <p>Cuerpo del modal.</p>\n  <template #footer>\n    <button class="tb-button" @click="open = false">Cerrar</button>\n  </template>\n</Modal>',
  },
  {
    name: "MultiLineChart",
    importPath: "../../shared/components/MultiLineChart.vue",
    description: "Gráfico de varias series de líneas sobre ejes compartidos.",
    component: MultiLineChart,
    live: false,
    example: '<MultiLineChart :series="series" :labels="labels" />',
  },
  {
    name: "MultiSelect",
    importPath: "../../shared/components/MultiSelect.vue",
    description: "Desplegable de selección múltiple con búsqueda.",
    component: MultiSelect,
    live: true,
    liveProps: {
      options: [
        { value: "a", label: "Opción A" },
        { value: "b", label: "Opción B" },
      ],
      modelValue: "",
    },
    example: '<MultiSelect :options="options" v-model="selected" />',
  },
  {
    name: "PieChart",
    importPath: "../../shared/components/PieChart.vue",
    description: "Gráfico circular a partir de un array de porciones {label, value}.",
    component: PieChart,
    live: true,
    liveProps: {
      slices: [
        { label: "OK", value: 70 },
        { label: "KO", value: 30 },
      ],
    },
    example: '<PieChart :slices="[{ label: \'OK\', value: 70 }, { label: \'KO\', value: 30 }]" />',
  },
  {
    name: "PlanManifestPanel",
    importPath: "../../shared/components/PlanManifestPanel.vue",
    description:
      "Panel completo de Plan Manifest (ciclos, cookies, subida/auto-fetch). Usa /api/manifest/*.",
    component: PlanManifestPanel,
    live: false,
    example: '<PlanManifestPanel :station-code="stationCode" @loaded="onManifest" />',
  },
  {
    name: "ScannerGunIcon",
    importPath: "../../shared/components/ScannerGunIcon.vue",
    description: "Icono SVG de pistola de escaneo (decorativo).",
    component: ScannerGunIcon,
    live: true,
    example: "<ScannerGunIcon />",
  },
  {
    name: "SortTh",
    importPath: "../../shared/components/SortTh.vue",
    description:
      "Cabecera <th> clicable para tablas ordenadas con useTableSort(). Pasa :sorter y k.",
    component: SortTh,
    live: true,
    needsSorter: true,
    wrapInTable: true,
    liveSlot: "Columna",
    example:
      'import { useTableSort } from "../../shared/useTableSort";\nconst sorter = useTableSort({ id: (r) => r.id });\n// ...\n<SortTh :sorter="sorter" k="id">ID</SortTh>',
  },
  {
    name: "SparkLineChart",
    importPath: "../../shared/components/SparkLineChart.vue",
    description: "Mini gráfico de tendencia (sparkline) a partir de una serie de {value}.",
    component: SparkLineChart,
    live: true,
    liveProps: {
      series: [{ value: 3 }, { value: 5 }, { value: 2 }, { value: 8 }, { value: 6 }, { value: 9 }],
    },
    example: '<SparkLineChart :series="[{ value: 3 }, { value: 5 }, { value: 2 }]" />',
  },
  {
    name: "StemConnectionPanel",
    importPath: "../../shared/components/StemConnectionPanel.vue",
    description: "Panel de conexión STEM (estado + reconectar). Usa el backend STEM.",
    component: StemConnectionPanel,
    live: false,
    example: '<StemConnectionPanel :source="source" @refresh="refreshStem" />',
  },
  {
    name: "SvgLineChart",
    importPath: "../../shared/components/SvgLineChart.vue",
    description: "Gráfico de líneas en SVG: :categories (eje X) + :series [{name, color, data}].",
    component: SvgLineChart,
    live: true,
    liveProps: {
      categories: ["L", "M", "X", "J", "V", "S"],
      series: [{ name: "Serie", color: "#1d4ed8", data: [10, 14, 9, 18, 22, 17] }],
    },
    example:
      '<SvgLineChart :categories="[\'L\',\'M\',\'X\']" :series="[{ name: \'Serie\', data: [10, 14, 9] }]" />',
  },
  {
    name: "TextEditField",
    importPath: "../../shared/components/TextEditField.vue",
    description: "Campo de texto editable en línea (click para editar).",
    component: TextEditField,
    live: true,
    liveProps: { modelValue: "Haz click para editar" },
    example: '<TextEditField v-model="value" />',
  },
  {
    name: "ThemeSwitcher",
    importPath: "../../shared/components/ThemeSwitcher.vue",
    description: "Alternar tema claro/oscuro, atado al theme compartido.",
    component: ThemeSwitcher,
    live: true,
    example: "<ThemeSwitcher />",
  },
  {
    name: "TokenChipInput",
    importPath: "../../shared/components/TokenChipInput.vue",
    description: "Entrada de tokens como chips (p. ej. estaciones, códigos).",
    component: TokenChipInput,
    live: true,
    liveProps: { modelValue: "DCZ4 DCT7" },
    example: '<TokenChipInput v-model="codes" />',
  },
  {
    name: "ToolboxFileStatus",
    importPath: "../../shared/components/ToolboxFileStatus.vue",
    description: "Indicador del estado del archivo diario compartido de la Toolbox.",
    component: ToolboxFileStatus,
    live: false,
    example: '<ToolboxFileStatus :status="fileStatus" />',
  },
  {
    name: "ToolPopupWindow",
    importPath: "../../shared/components/ToolPopupWindow.vue",
    description: "Ventana flotante tipo popup (maximizar/restaurar/cerrar). v-model.",
    component: ToolPopupWindow,
    live: false,
    example:
      '<ToolPopupWindow v-model="open" title="Mi ventana">\n  <p>Contenido flotante.</p>\n</ToolPopupWindow>',
  },
  {
    name: "TrackingLink",
    importPath: "../../shared/components/TrackingLink.vue",
    description: "Enlace al detalle de un tracking id en SCC.",
    component: TrackingLink,
    live: true,
    liveProps: { id: "TBA123456789" },
    example: '<TrackingLink :id="row.trackingId" />',
  },
];

// Documentation-only entry for the connections composable pair. It is NOT a
// renderable component, so the catalog shows it as a code/doc card. Documents
// the REAL, corrected API (design.md §2.3/§5.3): refresh / refreshAll /
// identify / reconnect — there is NO probeAll() method; useIdentity() probes
// every source automatically on mount via its own internal onMounted(start).
export const connectionsDoc = {
  name: "useIdentity / globalConnections",
  importPath: "../../shared/stores/useIdentity",
  description:
    "Gestor de sesiones externas (SCC, Mercury, QuickSight, PerfectMile). useIdentity(sources) se auto-arranca: sondea cada fuente en onMounted — NO hay probeAll(). Métodos reales: refresh(id), refreshAll(), identify() (reconecta todo lo caído), reconnect(id). Lee allConnected.value / anyReconnecting.value y statusOf(id).",
  example:
    'import { useIdentity } from "../../shared/stores/useIdentity";\n\n// Se auto-sondea en onMounted — NO llames a nada para el primer probe.\n// NO existe identity.probeAll().\nconst identity = useIdentity([{ id: "scc", label: "SCC" }]);\n\n// Para forzar un re-sondeo tras una acción del usuario:\n// identity.refreshAll();\n// Reconectar solo lo que esté caído: identity.identify();\n// Reconectar una fuente: identity.reconnect("scc");',
};
