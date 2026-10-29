<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useSnackbar } from "@/composables/useSnackbar";
import { useSocketEvent } from "@/composables/useSocketEvent";
import { RBtn, RChip, RIcon, RSpinner, RTextField } from "@/lib";
import api from "@/services/api";
import { formatBytes } from "@/utils";

const { t } = useI18n();
const snackbar = useSnackbar();

const query = ref("");
const loading = ref(false);
const results = ref<any[]>([]);
const selected = ref<Set<number>>(new Set());
const platforms = ref<any[]>([]);
const platformSlug = ref<string>("");
const downloading = ref(false);

const downloadQueue = ref<any[]>([]);
const searchController = ref<AbortController | null>(null);

const downloadProgress = ref<
  Record<string, { progress: number; status: string }>
>({});

const groups = ref<string[]>([]);
const consoles = ref<string[]>([]);
const regions = ref<string[]>([]);
const manufacturers = ref<string[]>([]);

const groupFilter = ref<string>("");
const consoleFilter = ref<string>("");
const regionFilter = ref<string>("");
const manufacturerFilter = ref<string>("");

async function loadFilters() {
  try {
    const { data } = await api.get<any>("/download/minerva/filters");
    groups.value = data?.groups ?? [];
    consoles.value = data?.consoles ?? [];
    regions.value = data?.regions ?? [];
    manufacturers.value = data?.manufacturers ?? [];
  } catch {
    groups.value = [];
    consoles.value = [];
    regions.value = [];
    manufacturers.value = [];
  }
}

const excludedPatterns =
  /\((?:beta|proto|demo|alpha|debug|lodgeNet| virtual console)[^)]*\)/i;

const visibleResults = computed(() =>
  results.value.filter((r) => {
    const name = String(r?.full_path ?? r?.file_name ?? "");
    return !excludedPatterns.test(name);
  }),
);

function iconForFile(name: string | undefined): string {
  const ext =
    String(name ?? "")
      .split(".")
      .pop()
      ?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    zip: "mdi-folder-zip-outline",
    "7z": "mdi-folder-zip-outline",
    rar: "mdi-folder-zip-outline",
    nes: "mdi-nintendo-game-boy",
    smc: "mdi-controller-classic",
    sfc: "mdi-controller-classic",
    gba: "mdi-gamepad-variant",
    gcn: "mdi-disc",
    iso: "mdi-disc",
    bin: "mdi-disc",
    cue: "mdi-disc",
    rom: "mdi-chip",
    adf: "mdi-floppy",
    chd: "mdi-disc",
  };
  return map[ext] ?? "mdi-file-download-outline";
}

function selectVisible() {
  visibleResults.value
    .filter((r) => !r?.exists)
    .forEach((r) => selected.value.add(r.id));
}

function clearSelection() {
  selected.value = new Set();
}

async function loadPlatforms() {
  try {
    const { data } = await api.get<any[]>("/platforms/supported");
    platforms.value = data ?? [];
    if (platforms.value.length && !platformSlug.value) {
      platformSlug.value =
        platforms.value[0].fs_slug ?? platforms.value[0].slug;
    }
  } catch (err) {
    const e = err as { message?: string };
    snackbar.error(e?.message ?? "Failed to load platforms");
  }
}

async function search() {
  if (!query.value.trim()) return;
  if (searchController.value) searchController.value.abort();
  const controller = new AbortController();
  searchController.value = controller;
  loading.value = true;
  results.value = [];
  selected.value = new Set();
  try {
    const { data } = await api.get<any[]>("/download/minerva/search", {
      params: {
        query: query.value.trim(),
        group: groupFilter.value || undefined,
        console: consoleFilter.value || undefined,
        region: regionFilter.value || undefined,
        manufacturer: manufacturerFilter.value || undefined,
      },
      signal: controller.signal,
    });
    const rows = (data ?? []) as any[];
    const enriched = await Promise.allSettled(
      rows.map(async (r) => {
        try {
          const { data: detail } = await api.get<any>("/download/minerva/rom", {
            params: { id: r.id },
            signal: controller.signal,
          });
          return { ...r, ...detail };
        } catch {
          return r;
        }
      }),
    );
    if (controller.signal.aborted) return;
    results.value = enriched.map((e) =>
      e.status === "fulfilled" ? e.value : (e.reason ?? {}),
    );
  } catch (err) {
    if (
      (err as { name?: string })?.name === "CanceledError" ||
      (err as { name?: string })?.name === "AbortError"
    ) {
      return;
    }
    const e = err as {
      response?: { data?: { detail?: string } };
      message?: string;
    };
    snackbar.error(e?.response?.data?.detail || e?.message || "Search failed");
  } finally {
    if (searchController.value === controller) searchController.value = null;
    loading.value = false;
  }
}

function cancelSearch() {
  searchController.value?.abort();
  searchController.value = null;
  loading.value = false;
  results.value = [];
  selected.value = new Set();
}

async function cancelDownload(item: any) {
  const id = item?.id;
  const fileName = item?.file_name;
  downloadQueue.value = downloadQueue.value.filter(
    (q) => q !== item && q.id !== item.id,
  );
  const key = String(fileName ?? "");
  if (key) downloadProgress.value[key] = { progress: 0, status: "cancelled" };
  try {
    await api.post("/download/minerva/cancel", {
      ids: id != null ? [id] : [],
      file_names: fileName ? [fileName] : [],
    });
  } catch {
    // Best effort; backend loop may still finish the item.
  }
}

function toggle(id: number) {
  const row = results.value.find((r) => r.id === id);
  if (row?.exists) return;
  if (selected.value.has(id)) selected.value.delete(id);
  else selected.value.add(id);
}

async function downloadSelected() {
  const ids = [...selected.value];
  if (!ids.length || !platformSlug.value) return;
  const chosen = results.value.filter((r) => selected.value.has(r.id));
  const items = chosen.map((r) => ({
    id: r.id,
    full_path: r.full_path,
    file_name: r.file_name ?? String(r.full_path).split("/").pop(),
    magnet: r.magnet,
    so_id: r.so_id,
    size: r.size,
    sha256: r.sha256,
    cover_url: r.cover_url,
    console: r.console,
  }));
  downloading.value = true;
  items.forEach((it) => {
    const key = String(it.file_name ?? "");
    if (key) downloadProgress.value[key] = { progress: 0, status: "queued" };
  });
  // Move selected cards into the Download Queue section.
  downloadQueue.value = [
    ...downloadQueue.value,
    ...chosen.map((r) => ({
      id: r.id,
      file_name: r.file_name ?? String(r.full_path).split("/").pop(),
      full_path: r.full_path,
      console: r.console,
      size: r.size,
      cover_url: r.cover_url,
    })),
  ];
  try {
    await api.post("/download/minerva/download", {
      platform_fs_slug: platformSlug.value,
      items,
    });
    snackbar.success(`Scheduled ${items.length} download(s)`);
    // Clear out the search so the queue is the focus.
    results.value = [];
    selected.value = new Set();
    query.value = "";
  } catch (err) {
    const e = err as {
      response?: { data?: { detail?: string } };
      message?: string;
    };
    snackbar.error(
      e?.response?.data?.detail || e?.message || "Download failed",
    );
  } finally {
    downloading.value = false;
  }
}

const selectedCount = computed(() => selected.value.size);

useSocketEvent<any>("download:progress", (payload) => {
  const key = String(payload?.file_name ?? payload?.id ?? "");
  if (!key) return;
  downloadProgress.value[key] = {
    progress: Number(payload?.progress ?? 0),
    status: String(payload?.status ?? "downloading"),
  };
});

useSocketEvent<any>("scan:done", () => {
  downloadQueue.value = downloadQueue.value.filter((r) => {
    const key = String(r?.file_name ?? "");
    return downloadProgress.value[key]?.status !== "done";
  });
});

async function fetchQueue() {
  try {
    const { data } = await api.get<any>("/download/minerva/queue");
    downloadQueue.value = data?.queue ?? [];
    downloadProgress.value = data?.progress ?? {};
  } catch {
    /* best effort */
  }
}

watch(consoleFilter, (value) => {
  if (!value) return;
  const needle = value.toLowerCase();
  const match = platforms.value.find((p) => {
    const candidates = [p.name, p.custom_name, p.fs_slug, p.slug]
      .filter(Boolean)
      .map((v: string) => String(v).toLowerCase());
    return candidates.some(
      (c) => c === needle || c.includes(needle) || needle.includes(c),
    );
  });
  if (match) {
    platformSlug.value = match.fs_slug ?? match.slug;
  }
});

onMounted(() => {
  loadPlatforms();
  loadFilters();
  fetchQueue();
});
</script>

<template>
  <div class="r-page">
    <div v-if="downloadQueue.length" class="r-queue-section">
      <div class="r-section-title">Download Queue</div>
      <div class="r-card-grid">
        <div
          v-for="(r, i) in downloadQueue"
          :key="`${r.id}-${i}`"
          class="r-download-card r-download-card--queue"
        >
          <div
            v-if="downloadProgress[String(r.file_name)]"
            class="r-download-card__progress"
            :style="{
              height: `${Math.round((downloadProgress[String(r.file_name)]?.progress ?? 0) * 100)}%`,
            }"
          ></div>
          <div
            v-if="downloadProgress[String(r.file_name)]?.status === 'done'"
            class="r-download-card__done"
          >
            <RIcon
              icon="mdi-check-circle"
              size="x-large"
              class="r-download-card__done-icon"
            />
          </div>
          <div class="r-download-card__icon">
            <img
              v-if="r.cover_url"
              :src="r.cover_url"
              alt=""
              class="r-download-card__cover"
            />
            <RIcon v-else :icon="iconForFile(r.file_name)" size="large" />
          </div>
          <div class="r-download-card__body">
            <div class="r-download-card__title">{{ r.file_name }}</div>
            <div class="r-download-card__meta">
              {{ r.console || "Unknown console" }}
            </div>
            <div class="r-download-card__sub">{{ r.full_path }}</div>
          </div>
          <div class="r-download-card__footer">
            <RChip size="small" variant="outlined">{{
              formatBytes(r.size ?? 0)
            }}</RChip>
            <span class="r-queue-status">
              {{ downloadProgress[String(r.file_name)]?.status ?? "queued" }}
              {{
                Math.round(
                  (downloadProgress[String(r.file_name)]?.progress ?? 0) * 100,
                )
              }}%
            </span>
            <RBtn size="x-small" variant="text" @click.stop="cancelDownload(r)">
              <RIcon icon="mdi-close" />
            </RBtn>
          </div>
        </div>
      </div>
    </div>

    <div class="r-filter-wrap">
      <div class="r-row r-filter-row">
        <select v-model="groupFilter" class="r-select">
          <option value="">All groups</option>
          <option v-for="g in groups" :key="g" :value="g">{{ g }}</option>
        </select>
        <select v-model="consoleFilter" class="r-select">
          <option value="">All consoles</option>
          <option v-for="c in consoles" :key="c" :value="c">{{ c }}</option>
        </select>
        <select v-model="regionFilter" class="r-select">
          <option value="">All regions</option>
          <option v-for="r in regions" :key="r" :value="r">{{ r }}</option>
        </select>
        <select v-model="manufacturerFilter" class="r-select">
          <option value="">All manufacturers</option>
          <option v-for="m in manufacturers" :key="m" :value="m">
            {{ m }}
          </option>
        </select>
      </div>
    </div>

    <div class="r-search-wrap">
      <div
        class="r-row"
        style="
          display: flex !important;
          flex-direction: column !important;
          align-items: center !important;
        "
      >
        <RTextField
          v-model="query"
          :placeholder="'Search Minerva Archive'"
          style="max-width: 600px; width: 100%"
          @keyup.enter="search"
        />
        <RBtn v-if="!loading" @click="search">
          <RIcon icon="mdi-magnify" />
          {{ t("common.search") }}
        </RBtn>
        <RBtn v-else variant="outlined" @click="cancelSearch">
          <RIcon icon="mdi-close" />
          Cancel
        </RBtn>
      </div>
    </div>

    <div
      class="r-row"
      v-if="results.length"
      style="
        display: flex;
        justify-content: center;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
      "
    >
      <select v-model="platformSlug" class="r-select">
        <option
          v-for="p in platforms"
          :key="p.fs_slug ?? p.slug"
          :value="p.fs_slug ?? p.slug"
        >
          {{ p.name ?? p.fs_slug ?? p.slug }}
        </option>
      </select>
      <RBtn
        :disabled="selectedCount === 0 || downloading"
        :loading="downloading"
        @click="downloadSelected"
      >
        Download {{ selectedCount ? `(${selectedCount})` : "" }}
      </RBtn>
    </div>

    <div v-if="loading" class="r-center">
      <RSpinner />
    </div>

    <div class="r-row r-source-row">
      <RBtn size="small" variant="text" @click="selectVisible">Select all</RBtn>
      <RBtn size="small" variant="text" @click="clearSelection">Clear</RBtn>
    </div>

    <div class="r-card-grid">
      <div
        v-for="r in visibleResults"
        :key="r.id"
        class="r-download-card"
        :class="{
          'r-download-card--selected': selected.has(r.id),
          'r-download-card--exists': r.exists,
        }"
        @click="toggle(r.id)"
      >
        <div
          v-if="downloadProgress[String(r.file_name)]"
          class="r-download-card__progress"
          :style="{
            height: `${Math.round((downloadProgress[String(r.file_name)]?.progress ?? 0) * 100)}%`,
          }"
        ></div>
        <div
          v-if="downloadProgress[String(r.file_name)]?.status === 'done'"
          class="r-download-card__done"
        >
          <RIcon
            icon="mdi-check-circle"
            size="x-large"
            class="r-download-card__done-icon"
          />
        </div>
        <div class="r-download-card__icon">
          <img
            v-if="r.cover_url"
            :src="r.cover_url"
            alt=""
            class="r-download-card__cover"
          />
          <RIcon v-else :icon="iconForFile(r.file_name)" size="large" />
        </div>
        <div class="r-download-card__body">
          <div class="r-download-card__title">{{ r.file_name }}</div>
          <div class="r-download-card__meta">
            {{ r.console || "Unknown console" }}
            <span v-if="r.exists" class="r-download-card__exists">
              Already in library
            </span>
          </div>
          <div class="r-download-card__sub">{{ r.full_path }}</div>
        </div>
        <div class="r-download-card__footer">
          <RChip size="small" variant="outlined">{{
            formatBytes(r.size ?? 0)
          }}</RChip>
          <RIcon
            :icon="
              selected.has(r.id) ? 'mdi-check-circle' : 'mdi-circle-outline'
            "
            :class="{ 'r-download-card__check--selected': selected.has(r.id) }"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.r-search-wrap {
  display: flex;
  justify-content: center;
  margin: 0 auto 12px;
  max-width: 1200px;
}
.r-search-wrap .r-row {
  width: 100%;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.r-search-wrap .r-row .r-btn {
  margin: 4px 0;
}
.r-search-wrap .r-row .r-text-field,
.r-search-wrap .r-row input {
  max-width: 480px;
  width: 100%;
}
.r-search-wrap .r-row > * {
  flex: 0 1 auto;
  margin: 0 4px;
}
.r-filter-wrap {
  display: flex;
  justify-content: center;
  margin-bottom: 12px;
}
.r-filter-row {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: center;
  padding: 12px 16px;
  margin: 0 auto 12px;
  width: fit-content;
  max-width: 100%;
  border-radius: 8px;
  background: var(--r-color-surface-2, var(--r-color-surface, #1e1e1e));
  border: 1px solid var(--r-color-border, #333);
}

.r-page {
  zoom: 1.15;
}
.r-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
  margin-top: 16px;
}
.r-download-card {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px;
  border: 1px solid var(--r-color-border, #333);
  border-radius: 8px;
  background: var(--r-color-surface, #1a1a1a);
  cursor: pointer;
  transition:
    transform 0.1s ease,
    box-shadow 0.1s ease;
}
.r-download-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
}
.r-download-card--selected {
  border-color: var(--r-color-brand-primary, #4caf50);
  background: color-mix(
    in srgb,
    var(--r-color-brand-primary, #4caf50) 10%,
    var(--r-color-surface, #1a1a1a)
  );
}
.r-download-card__title {
  font-weight: 600;
  word-break: break-word;
}
.r-download-card__meta {
  font-size: 0.875rem;
  color: var(--r-color-fg-muted, #aaa);
}
.r-download-card__sub {
  font-family: monospace;
  font-size: 0.75rem;
  color: var(--r-color-fg-muted, #aaa);
  word-break: break-all;
}
.r-download-card__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}
.r-download-card__progress {
  position: absolute;
  left: 0;
  bottom: 0;
  width: 100%;
  background: color-mix(
    in srgb,
    var(--r-color-brand-primary, #4caf50) 35%,
    transparent
  );
  transition: height 0.3s ease;
  pointer-events: none;
  z-index: 0;
}
.r-download-card__done {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: color-mix(
    in srgb,
    var(--r-color-brand-primary, #4caf50) 25%,
    transparent
  );
  pointer-events: none;
  z-index: 2;
}
.r-download-card__done-icon {
  color: var(--r-color-brand-primary, #4caf50);
  font-size: 4rem;
}
.r-download-card
  > *:not(.r-download-card__progress):not(.r-download-card__done) {
  position: relative;
  z-index: 1;
}
.r-download-card__cover {
  width: 100%;
  height: 120px;
  object-fit: cover;
  border-radius: 6px;
}
.r-download-card--exists {
  opacity: 0.55;
  cursor: not-allowed;
  filter: grayscale(0.35);
}
.r-download-card__exists {
  margin-left: 6px;
  font-size: 0.75rem;
  color: var(--r-color-fg-muted, #aaa);
  font-style: italic;
}
.r-queue-section {
  margin: 8px auto 16px;
  max-width: 1200px;
}
.r-section-title {
  font-size: 1.1rem;
  font-weight: 600;
  margin: 0 0 8px;
}
.r-queue-status {
  font-size: 0.8rem;
  color: var(--r-color-fg-muted, #aaa);
  text-transform: capitalize;
}
.r-download-card--queue {
  padding: 10px;
  gap: 6px;
}
.r-download-card--queue .r-download-card__cover {
  height: 80px;
}
.r-download-card--queue .r-download-card__title {
  font-size: 0.85rem;
}
.r-download-card--queue .r-download-card__meta,
.r-download-card--queue .r-download-card__sub {
  font-size: 0.7rem;
}
.r-download-card--queue .r-card-grid {
  gap: 8px;
}
.r-queue-section .r-card-grid {
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px;
}
</style>
PY echo created
