<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { onBeforeRouteLeave } from "vue-router";
import ConfigFileAlerts from "@/components/Settings/ConfigFileAlerts.vue";
import SettingsSaveBar from "@/components/Settings/SettingsSaveBar.vue";
import SettingsSection from "@/components/Settings/SettingsSection.vue";
import SettingsToggleRow from "@/components/Settings/SettingsToggleRow.vue";
import { useSnackbar } from "@/composables/useSnackbar";
import { useUnloadGuard } from "@/composables/useUnloadGuard";
import { RBtn, RSpinner, RTextField } from "@/lib";
import api from "@/services/api";
import configApi from "@/services/api/config";
import storeAuth from "@/stores/auth";
import storeConfig from "@/stores/config";

const { t } = useI18n();
const snackbar = useSnackbar();
const configStore = storeConfig();
const authStore = storeAuth();
const config = computed(() => configStore.config);

interface Form {
  enabled: boolean;
  qbittorrentUrl: string;
  qbittorrentUsername: string;
  qbittorrentPassword: string;
  metadataTimeoutSeconds: number;
  scanAfterDownload: boolean;
}

const rawDownload = computed(() => (config.value as any)?.DOWNLOAD ?? {});

function configToForm(): Form {
  return {
    enabled: rawDownload.value?.enabled ?? true,
    qbittorrentUrl: rawDownload.value?.qbittorrent_url ?? "",
    qbittorrentUsername: rawDownload.value?.qbittorrent_username ?? "",
    qbittorrentPassword: rawDownload.value?.qbittorrent_password ?? "",
    metadataTimeoutSeconds: rawDownload.value?.metadata_timeout_seconds ?? 60,
    scanAfterDownload: rawDownload.value?.scan_after_download ?? true,
  };
}

const form = reactive<Form>(configToForm());

const savedSnapshot = ref(JSON.stringify(form));
const testing = ref(false);
const dirty = computed(() => JSON.stringify(form) !== savedSnapshot.value);

const canEdit = computed(
  () =>
    authStore.scopes.includes("platforms.write") &&
    (config.value as any)?.CONFIG_FILE_WRITABLE,
);

function resetForm() {
  Object.assign(form, configToForm());
  savedSnapshot.value = JSON.stringify(form);
}

async function save() {
  try {
    await configApi.updateDownloadSettings({
      enabled: form.enabled,
      qbittorrent_url: form.qbittorrentUrl,
      qbittorrent_username: form.qbittorrentUsername,
      qbittorrent_password: form.qbittorrentPassword,
      metadata_timeout_seconds: form.metadataTimeoutSeconds,
      scan_after_download: form.scanAfterDownload,
    });
    await configStore.fetchConfig();
    resetForm();
    snackbar.success(t("common.saved"));
  } catch (err) {
    snackbar.error("Failed to save download settings");
  }
}

async function testConnection() {
  testing.value = true;
  try {
    await api.post("/download/minerva/test-connection", {
      qbittorrent_url: form.qbittorrentUrl,
      qbittorrent_username: form.qbittorrentUsername,
      qbittorrent_password: form.qbittorrentPassword,
    });
    snackbar.success("qBittorrent connection successful");
  } catch (err) {
    snackbar.error("qBittorrent connection failed");
  } finally {
    testing.value = false;
  }
}

useUnloadGuard(() => dirty.value);

watch(
  () => config.value,
  () => resetForm(),
  { deep: true },
);
</script>

<template>
  <div class="r-page">
    <ConfigFileAlerts />
    <SettingsSection :title="t('settings.download-settings')">
      <SettingsToggleRow
        v-model="form.enabled"
        :title="'Enable downloads'"
        :description="'Show the Download page and allow queued downloads.'"
      />
      <RTextField
        v-model="form.qbittorrentUrl"
        label="qBittorrent URL"
        hint="Base URL of the qBittorrent Web UI used to fetch Minerva torrents."
      />
      <RTextField
        v-model="form.qbittorrentUsername"
        label="qBittorrent username"
        hint="qBittorrent Web UI username."
      />
      <RTextField
        v-model="form.qbittorrentPassword"
        label="qBittorrent password"
        type="password"
        hint="qBittorrent Web UI password."
      />
      <RBtn :loading="testing" variant="outlined" @click="testConnection">
        Test connection
      </RBtn>
      <RTextField
        v-model="form.metadataTimeoutSeconds as any"
        label="Metadata timeout (seconds)"
        type="number"
        hint="How long to wait for torrent metadata before marking an item failed."
      />
      <SettingsToggleRow
        v-model="form.scanAfterDownload"
        title="Scan library after download"
        description="Run a library scan once downloads finish."
      />
    </SettingsSection>
    <SettingsSaveBar
      :visible="dirty"
      label="Unsaved changes"
      :save-disabled="!canEdit"
      @save="save"
      @discard="resetForm"
    />
  </div>
</template>
EOF
