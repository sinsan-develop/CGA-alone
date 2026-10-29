import { apiRequest, apiSameOriginRequest, formatApiErrorDetail } from "@/lib/api";
import { type AnswerMode } from "@/lib/answer-options";
import { type BotVersionSettings, type ConfigurationScoringConfig, type StudioBotDataJson, type VectorConnectionsConfig } from "@/lib/bot-settings";
import { type LlmModelKey, type LlmProvider } from "@/lib/llm-options";
import { type SupportedLanguage } from "@/lib/language";
import { type NluModelKey, type NluType } from "@/lib/nlu-options";
import { normalizeVersionDocument, type VersionAssetCounts, type VersionDocument } from "@/lib/version-document";

export type StudioTalkTemplateItem = {
  id: string;
  channel_code: string;
  channel_name: string;
  name: string;
  renderer_type: string;
  item_count: number;
  item_types: string;
  description?: string | null;
  status: "active" | "inactive";
  status_label: string;
  updated_at: string | null;
  data_json?: Record<string, unknown>;
};

export type StudioTalkTemplatesResponse = {
  items: StudioTalkTemplateItem[];
  total: number;
};
export type StudioBotVersionApiItem = {
  id: string;
  bot_id: string;
  version_no: number;
  name: string;
  description: string | null;
  status: string;
  comment: string | null;
  version_json?: VersionDocument;
  system_config?: VersionDocument["system_config"];
  asset_counts?: VersionAssetCounts;
  scenario_validation?: {
    status?: string;
    error_count?: number;
    warning_count?: number;
    save_blocking_error_count?: number;
    blocked_dialog_ids?: string[];
    save_blocked_dialog_ids?: string[];
    save_blocking_items?: Array<Record<string, unknown>>;
    items?: Array<Record<string, unknown>>;
  };
  is_active: boolean;
  copied_from_version_id: string | null;
  activated_at: string | null;
  is_trained?: boolean;
  nlu_training?: Record<string, unknown>;
  updated_by_login_id?: string | null;
  updated_at: string | null;
  created_at: string | null;
};

export type StudioBotApiItem = {
  id: string;
  organization_id: string;
  group_id: string;
  group_code: string | null;
  group_name: string | null;
  name: string;
  description: string | null;
  status: string;
  data_json?: StudioBotDataJson;
  active_version_id: string | null;
  active_version?: StudioBotVersionApiItem | null;
  version_count: number;
  created_by?: string | null;
  creator_login_id?: string | null;
  creator_name?: string | null;
  updated_at: string;
  created_at: string;
};

export type StudioHubMember = {
  id: string;
  bot_id: string;
  name: string | null;
  display_name: string | null;
  sort_order: number;
  use_as_small_talk: boolean;
  has_operating_version: boolean;
  active_version_id: string | null;
};

export type StudioHubCallRule = {
  id: string;
  name: string;
  expression: string;
  enabled: boolean;
};

export type StudioHub = {
  id: string;
  name: string;
  description: string | null;
  status: string;
  active_version_id: string | null;
  active_version_name: string | null;
  profile_key: "gray" | "accent" | "outline";
  profile_image_url: string | null;
  language: SupportedLanguage;
  introduction: string | null;
  call_method: "button" | "natural";
  button_match_mode: "exact" | "contains";
  greeting_message: string | null;
  intent_cutoff_score: number;
  similar_intent_score: number;
  max_intent_candidates: number;
  show_members_in_greeting: boolean;
  unrecognized_message: string | null;
  multiple_candidates_message: string | null;
  runtime_error_message: string | null;
  conversation_in_progress_message: string | null;
  timeout_seconds: number | null;
  apply_timeout_to_push: boolean;
  timeout_message: string | null;
  no_bot_label: string | null;
  no_bot_message: string | null;
  hub_call_rules: StudioHubCallRule[];
  members: StudioHubMember[];
  created_at: string | null;
  updated_at: string | null;
};

export type UpdateStudioHubMembersPayload = {
  members: Array<{ bot_id: string; display_name?: string; use_as_small_talk: boolean }>;
};

export type UpdateStudioHubSettingsPayload = Partial<Omit<StudioHub, "id" | "name" | "description" | "status" | "profile_key" | "language" | "introduction" | "members" | "created_at" | "updated_at">>;
export type StudioWorkspaceContextApiResponse = {
  bot: StudioBotApiItem;
  versions: StudioBotVersionApiItem[];
  version: StudioBotVersionApiItem | null;
  counts: VersionAssetCounts;
  permissions: {
    can_edit: boolean;
    can_train: boolean;
    can_publish: boolean;
  };
  health: {
    has_scenario_error: boolean;
    has_training_error: boolean;
  };
};

export type EditLockArea = "start" | "flow" | "settings";

export type EditLockInfo = {
  lock_id: string;
  bot_id: string;
  version_id: string;
  dialog_id: string;
  area: string;
  owner: {
    user_id: string;
    login_id: string;
    name?: string | null;
  };
  expires_at: string;
  last_seen_at: string;
  released_at?: string | null;
  active: boolean;
};

export type EditLockResponse = {
  status: "locked_by_me" | "locked_by_other" | "available" | "released" | "not_found" | "force_released";
  mode?: "edit" | "view_only_available";
  released?: boolean;
  lock?: EditLockInfo | null;
};

export type EditLockTarget = {
  bot_id: string;
  version_id: string;
  dialog_id: string;
  area: EditLockArea;
};

export type StudioVersionDialogsApiResponse = {
  bot_id: string;
  version_id: string;
  section: string;
  version: StudioBotVersionApiItem;
  items: VersionDocument["dialogs"];
  asset_counts: VersionAssetCounts;
  scenario_validation?: StudioBotVersionApiItem["scenario_validation"];
  updated_at: string | null;
};

export type StudioVersionEntitiesApiResponse = Omit<StudioVersionDialogsApiResponse, "items"> & {
  items: VersionDocument["entities"];
};

export type StudioVersionDictionaryApiResponse = Omit<StudioVersionDialogsApiResponse, "items"> & {
  items: VersionDocument["dictionary"];
};

export type StudioVersionApisApiResponse = Omit<StudioVersionDialogsApiResponse, "items"> & {
  items: VersionDocument["apis"];
};

export type StudioGroupApisApiResponse = {
  items: VersionDocument["apis"];
  total: number;
};

export type StudioVersionReferencesApiResponse = {
  bot_id: string;
  version_id: string;
  version: StudioBotVersionApiItem;
  dialogs: VersionDocument["dialogs"];
  dialog_flow_graphs: VersionDocument["dialog_flow_graphs"];
  rules: VersionDocument["rules"];
  updated_at: string | null;
};

export type StudioVersionDialogFlowApiResponse = {
  bot_id: string;
  version_id: string;
  dialog_id: string;
  version: StudioBotVersionApiItem;
  dialog: Record<string, unknown>;
  dialogs: VersionDocument["dialogs"];
  dialog_flow_graphs: VersionDocument["dialog_flow_graphs"];
  entities: VersionDocument["entities"];
  apis: VersionDocument["apis"];
  system_config: VersionDocument["system_config"];
  asset_counts: VersionAssetCounts;
  scenario_validation?: StudioBotVersionApiItem["scenario_validation"];
  updated_at: string | null;
};

export type StudioVersionConfigureApiResponse = {
  bot_id: string;
  version_id: string;
  version: StudioBotVersionApiItem;
  dialogs: VersionDocument["dialogs"];
  dialog_flow_graphs: VersionDocument["dialog_flow_graphs"];
  dictionary: VersionDocument["dictionary"];
  entities: VersionDocument["entities"];
  system_config: VersionDocument["system_config"];
  asset_counts: VersionAssetCounts;
  scenario_validation?: StudioBotVersionApiItem["scenario_validation"];
  updated_at: string | null;
};

export type StudioVersionRetrainingApiResponse = {
  bot_id: string;
  version_id: string;
  version: StudioBotVersionApiItem;
  dialogs: VersionDocument["dialogs"];
  system_config: VersionDocument["system_config"];
  asset_counts: VersionAssetCounts;
  scenario_validation?: StudioBotVersionApiItem["scenario_validation"];
  updated_at: string | null;
};

const STUDIO_BOT_LIST_CACHE_TTL_MS = 120_000;
const WORKSPACE_CONTEXT_CACHE_TTL_MS = 120_000;
const SETTINGS_CONTEXT_CACHE_TTL_MS = 120_000;
const VERSION_SECTION_CACHE_TTL_MS = 120_000;

type StudioBotListCacheEntry = {
  expiresAt: number;
  data: StudioBotApiItem[];
};

type WorkspaceContextCacheEntry = {
  expiresAt: number;
  data: StudioWorkspaceContextApiResponse;
};

type VersionSectionCacheEntry<T> = {
  expiresAt: number;
  data: T;
};

const workspaceContextCache = new Map<string, WorkspaceContextCacheEntry>();
const workspaceContextRequests = new Map<string, Promise<StudioWorkspaceContextApiResponse>>();
const settingsContextCache = new Map<string, WorkspaceContextCacheEntry>();
const settingsContextRequests = new Map<string, Promise<StudioWorkspaceContextApiResponse>>();
const studioBotListCache = new Map<string, StudioBotListCacheEntry>();
const studioBotListRequests = new Map<string, Promise<StudioBotApiItem[]>>();
const versionSectionCache = new Map<string, VersionSectionCacheEntry<unknown>>();
const versionSectionRequests = new Map<string, Promise<unknown>>();
const EMPTY_ASSET_COUNTS: VersionAssetCounts = {
  dialogs: 0,
  intents: 0,
  modules: 0,
  dialog_flow_graphs: 0,
  entities: 0,
  dictionary: 0,
  qa: 0,
  apis: 0,
  floating_buttons: 0,
  rules: 0,
  small_talk: 0,
  blacklists: 0,
  retraining_records: 0,
};

const WORKSPACE_CONTEXT_STORAGE_PREFIX = "aidot.workspaceContext.";
const WORKSPACE_CONTEXT_USER_STORAGE_PREFIX = "aidot.workspaceContextByUser.";

function tokenFingerprint(token: string) {
  let hash = 0;
  for (let index = 0; index < token.length; index += 1) {
    hash = (hash * 31 + token.charCodeAt(index)) >>> 0;
  }
  return hash.toString(36);
}

type WorkspacePrefetchSection = "dialogs";

function workspaceContextCacheKey(
  token: string,
  botId: string,
  versionScope: string,
  includeDocument = false,
  prefetchSection?: WorkspacePrefetchSection,
) {
  return [
    tokenFingerprint(token),
    botId.trim().toLowerCase(),
    versionScope.trim().toLowerCase() || "v1",
    includeDocument ? "full" : "summary",
    prefetchSection ?? "none",
  ].join(":");
}

function workspaceContextUserCacheKey(
  userLoginId: string | undefined,
  botId: string,
  versionScope: string,
  includeDocument = true,
  prefetchSection?: WorkspacePrefetchSection,
) {
  const normalizedLoginId = String(userLoginId ?? "").trim().toLowerCase();
  if (!normalizedLoginId) {
    return null;
  }
  return [
    normalizedLoginId,
    botId.trim().toLowerCase(),
    versionScope.trim().toLowerCase() || "v1",
    includeDocument ? "full" : "summary",
    prefetchSection ?? "none",
  ].join(":");
}

function workspaceContextStorageKey(cacheKey: string) {
  return `${WORKSPACE_CONTEXT_STORAGE_PREFIX}${cacheKey}`;
}

function workspaceContextUserStorageKey(cacheKey: string) {
  return `${WORKSPACE_CONTEXT_USER_STORAGE_PREFIX}${cacheKey}`;
}

function readStoredWorkspaceContext(storage: Storage, storageKey: string, allowExpired: boolean) {
  const raw = storage.getItem(storageKey);
  if (!raw) {
    return null;
  }
  const parsed = JSON.parse(raw) as WorkspaceContextCacheEntry;
  if (!parsed || typeof parsed !== "object" || !parsed.data) {
    storage.removeItem(storageKey);
    return null;
  }
  if (!allowExpired && Number(parsed.expiresAt || 0) <= Date.now()) {
    storage.removeItem(storageKey);
    return null;
  }
  return parsed;
}

function loadStoredWorkspaceContext(cacheKey: string, allowExpired: boolean) {
  if (typeof window === "undefined") {
    return null;
  }
  const storageKey = workspaceContextStorageKey(cacheKey);
  try {
    return readStoredWorkspaceContext(window.sessionStorage, storageKey, allowExpired);
  } catch {
    window.sessionStorage.removeItem(storageKey);
    return null;
  }
}

function loadStoredUserWorkspaceContext(cacheKey: string, allowExpired: boolean) {
  if (typeof window === "undefined") {
    return null;
  }
  const storageKey = workspaceContextUserStorageKey(cacheKey);
  try {
    return readStoredWorkspaceContext(window.localStorage, storageKey, allowExpired);
  } catch {
    window.localStorage.removeItem(storageKey);
    return null;
  }
}

function storeWorkspaceContext(cacheKey: string, entry: WorkspaceContextCacheEntry) {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.sessionStorage.setItem(workspaceContextStorageKey(cacheKey), JSON.stringify(entry));
  } catch {
    // Cache persistence is a performance hint only.
  }
}

function storeUserWorkspaceContext(cacheKey: string | null, entry: WorkspaceContextCacheEntry) {
  if (!cacheKey || typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(workspaceContextUserStorageKey(cacheKey), JSON.stringify(entry));
  } catch {
    // Cache persistence is a performance hint only.
  }
}

function settingsContextCacheKey(token: string, botId: string, versionScope: string) {
  return [
    tokenFingerprint(token),
    botId.trim().toLowerCase(),
    versionScope.trim().toLowerCase() || "v1",
    "settings",
  ].join(":");
}

function versionSectionCacheKey(token: string, botId: string, versionId: string, section: string) {
  return [
    tokenFingerprint(token),
    botId.trim().toLowerCase(),
    versionId.trim().toLowerCase(),
    section.trim().toLowerCase(),
  ].join(":");
}

export function clearStudioWorkspaceContextCache() {
  workspaceContextCache.clear();
  workspaceContextRequests.clear();
  settingsContextCache.clear();
  settingsContextRequests.clear();
  if (typeof window !== "undefined") {
    try {
      Object.keys(window.sessionStorage)
        .filter((key) => key.startsWith(WORKSPACE_CONTEXT_STORAGE_PREFIX))
        .forEach((key) => window.sessionStorage.removeItem(key));
      Object.keys(window.localStorage)
        .filter((key) => key.startsWith(WORKSPACE_CONTEXT_USER_STORAGE_PREFIX))
        .forEach((key) => window.localStorage.removeItem(key));
    } catch {
      // Ignore cache cleanup failures.
    }
  }
}

export function clearStudioBotListCache() {
  studioBotListCache.clear();
  studioBotListRequests.clear();
}

export function clearStudioVersionSectionCache() {
  versionSectionCache.clear();
  versionSectionRequests.clear();
}

export function clearStudioClientCaches() {
  clearStudioWorkspaceContextCache();
  clearStudioBotListCache();
  clearStudioVersionSectionCache();
}

export function getCachedStudioBots(token: string) {
  const cacheKey = tokenFingerprint(token);
  const cached = studioBotListCache.get(cacheKey);
  if (!cached || cached.expiresAt <= Date.now()) {
    studioBotListCache.delete(cacheKey);
    return null;
  }
  return cached.data;
}

export function prefetchStudioBots(token: string) {
  void fetchStudioBots(token).catch(() => undefined);
}

export function getCachedStudioWorkspaceContext(
  token: string,
  botId: string,
  versionScope: string,
  includeDocument = false,
  allowExpired = false,
  prefetchSection?: WorkspacePrefetchSection,
  userLoginId?: string,
  allowFullFallback = true,
) {
  const reusableFullKeys = !includeDocument && allowFullFallback
    ? [workspaceContextCacheKey(token, botId, versionScope, true)]
    : [];
  const cacheKeys = [
    workspaceContextCacheKey(token, botId, versionScope, includeDocument, prefetchSection),
    ...reusableFullKeys,
  ];
  const reusableFullUserKeys = !includeDocument && allowFullFallback
    ? [workspaceContextUserCacheKey(userLoginId, botId, versionScope, true)]
    : [];
  const userCacheKeys = [
    workspaceContextUserCacheKey(userLoginId, botId, versionScope, includeDocument, prefetchSection),
    ...reusableFullUserKeys,
  ].filter((key): key is string => Boolean(key));
  const cacheKey = [...cacheKeys, ...userCacheKeys].find((key) => workspaceContextCache.has(key)) ?? cacheKeys[0];
  const cached = cacheKey ? workspaceContextCache.get(cacheKey) : undefined;
  const stored = !cached && cacheKey ? loadStoredWorkspaceContext(cacheKey, allowExpired) : null;
  const userStored = !cached && !stored
    ? userCacheKeys.map((key) => [key, loadStoredUserWorkspaceContext(key, allowExpired)] as const).find(([, entry]) => Boolean(entry))
    : undefined;
  const entry = cached ?? stored ?? userStored?.[1] ?? undefined;
  if (!entry) {
    return null;
  }
  if (!allowExpired && entry.expiresAt <= Date.now()) {
    workspaceContextCache.delete(cacheKey!);
    return null;
  }
  if (stored && cacheKey) {
    workspaceContextCache.set(cacheKey, stored);
  }
  if (userStored) {
    workspaceContextCache.set(userStored[0], userStored[1]!);
  }
  return entry.data;
}

export function hasFreshCachedStudioWorkspaceContext(
  token: string,
  botId: string,
  versionScope: string,
  includeDocument = false,
  prefetchSection?: WorkspacePrefetchSection,
  userLoginId?: string,
  allowFullFallback = true,
) {
  const reusableFullKeys = !includeDocument && allowFullFallback
    ? [workspaceContextCacheKey(token, botId, versionScope, true)]
    : [];
  const cacheKeys = [
    workspaceContextCacheKey(token, botId, versionScope, includeDocument, prefetchSection),
    ...reusableFullKeys,
  ];
  const reusableFullUserKeys = !includeDocument && allowFullFallback
    ? [workspaceContextUserCacheKey(userLoginId, botId, versionScope, true)]
    : [];
  const userCacheKeys = [
    workspaceContextUserCacheKey(userLoginId, botId, versionScope, includeDocument, prefetchSection),
    ...reusableFullUserKeys,
  ].filter((key): key is string => Boolean(key));
  const cacheKey = [...cacheKeys, ...userCacheKeys].find((key) => workspaceContextCache.has(key)) ?? cacheKeys[0];
  const cached = cacheKey ? workspaceContextCache.get(cacheKey) : undefined;
  const stored = !cached && cacheKey ? loadStoredWorkspaceContext(cacheKey, false) : null;
  const userStored = !cached && !stored
    ? userCacheKeys.map((key) => [key, loadStoredUserWorkspaceContext(key, false)] as const).find(([, entry]) => Boolean(entry))
    : undefined;
  const entry = cached ?? stored ?? userStored?.[1] ?? undefined;
  if (!entry) {
    return false;
  }
  if (entry.expiresAt <= Date.now()) {
    workspaceContextCache.delete(cacheKey!);
    return false;
  }
  if (stored && cacheKey) {
    workspaceContextCache.set(cacheKey, stored);
  }
  if (userStored) {
    workspaceContextCache.set(userStored[0], userStored[1]!);
  }
  return true;
}

function seedWorkspaceContextCacheFromBotList(token: string, bots: StudioBotApiItem[]) {
  const expiredAt = Date.now() - 1;
  bots.forEach((bot) => {
    const activeVersion = bot.active_version ?? null;
    if (!activeVersion) {
      return;
    }

    const versionScopes = new Set([
      activeVersion.id,
      activeVersion.name,
      `v${activeVersion.version_no}`,
      String(activeVersion.version_no),
    ]);
    const context: StudioWorkspaceContextApiResponse = {
      bot,
      versions: [activeVersion],
      version: activeVersion,
      counts: {
        ...EMPTY_ASSET_COUNTS,
        ...(activeVersion.asset_counts ?? {}),
      },
      permissions: {
        can_edit: true,
        can_train: true,
        can_publish: true,
      },
      health: {
        has_scenario_error: Number(activeVersion.scenario_validation?.error_count ?? 0) > 0,
        has_training_error: false,
      },
    };

    versionScopes.forEach((versionScope) => {
      const normalizedScope = String(versionScope || "").trim();
      if (!normalizedScope) {
        return;
      }
      [undefined, "dialogs" as const].forEach((prefetchSection) => {
        const cacheKey = workspaceContextCacheKey(token, bot.id, normalizedScope, false, prefetchSection);
        workspaceContextCache.set(
          cacheKey,
          { data: context, expiresAt: expiredAt },
        );
      });
    });
  });
}

function getCachedVersionSection<T>(token: string, botId: string, versionId: string, section: string) {
  const cacheKey = versionSectionCacheKey(token, botId, versionId, section);
  const cached = versionSectionCache.get(cacheKey) as VersionSectionCacheEntry<T> | undefined;
  if (!cached || cached.expiresAt <= Date.now()) {
    versionSectionCache.delete(cacheKey);
    return null;
  }
  return cached.data;
}

export function getCachedStudioBotVersionDialogs(token: string, botId: string, versionId: string) {
  return getCachedVersionSection<StudioVersionDialogsApiResponse>(token, botId, versionId, "dialogs");
}

function fetchStudioVersionSection<T>(
  token: string,
  botId: string,
  versionId: string,
  section: string,
  path: string,
) {
  const cacheKey = versionSectionCacheKey(token, botId, versionId, section);
  const cached = getCachedVersionSection<T>(token, botId, versionId, section);
  if (cached) {
    return Promise.resolve(cached);
  }

  const pending = versionSectionRequests.get(cacheKey) as Promise<T> | undefined;
  if (pending) {
    return pending;
  }

  const request = apiRequest<T>(path, {}, token)
    .then((data) => {
      versionSectionCache.set(cacheKey, {
        data,
        expiresAt: Date.now() + VERSION_SECTION_CACHE_TTL_MS,
      });
      return data;
    })
    .finally(() => {
      versionSectionRequests.delete(cacheKey);
    });
  versionSectionRequests.set(cacheKey, request);
  return request;
}

function versionWithWorkspaceCounts(
  version: StudioBotVersionApiItem | null | undefined,
  counts: VersionAssetCounts | null | undefined,
): StudioBotVersionApiItem | null {
  return version
    ? {
        ...version,
        asset_counts: {
          ...EMPTY_ASSET_COUNTS,
          ...(version.asset_counts ?? {}),
          ...(counts ?? {}),
        },
      }
    : null;
}

export function botWithWorkspaceContext(context: StudioWorkspaceContextApiResponse): StudioBotApiItem {
  const activeVersion = versionWithWorkspaceCounts(context.version, context.counts);
  return {
    ...context.bot,
    active_version: activeVersion,
    active_version_id: activeVersion?.id ?? context.bot.active_version_id,
    updated_at: activeVersion?.updated_at ?? context.bot.updated_at,
  };
}

export function isStudioBotVersionTrained(version?: Pick<StudioBotVersionApiItem, "is_trained" | "nlu_training"> | null) {
  if (!version) {
    return false;
  }

  if (version.is_trained === true) {
    return true;
  }

  return version.nlu_training?.status === "success" || typeof version.nlu_training?.trained_at === "string";
}

export function getScenarioTrainingDisabledReason(version?: Pick<StudioBotVersionApiItem, "scenario_validation"> | null) {
  const validation = version?.scenario_validation;
  const errorCount = Number(validation?.error_count ?? 0);
  if (errorCount <= 0) {
    return "";
  }
  const firstError = validation?.items?.find((item) => String(item.severity ?? "error") === "error");
  const dialogName = typeof firstError?.dialog_name === "string" ? firstError.dialog_name.trim() : "";
  const nodeTitle = typeof firstError?.node_title === "string" ? firstError.node_title.trim() : "";
  const message = typeof firstError?.message === "string" ? firstError.message.trim() : "";
  const location = [dialogName, nodeTitle].filter(Boolean).join(" / ");
  const detail = location && message ? `${location}: ${message}` : message || "대화 설계 오류가 있습니다.";
  return `설계 오류 ${errorCount}건이 있어 학습할 수 없습니다. ${detail}`;
}

export type CreateBotPayload = {
  name: string;
  description?: string;
  bot_kind: "bot" | "hub";
  bot_mode: "text" | "voice";
  profile_key: "gray" | "accent" | "outline";
  profile_image_data?: string;
  language: SupportedLanguage;
  nlu_engine?: NluModelKey;
  nlu_type?: NluType;
  nlu_model?: NluModelKey;
  answer_mode?: AnswerMode;
  llm_provider?: LlmProvider;
  llm_model?: LlmModelKey;
  llm_base_url?: string;
  vector_connections?: VectorConnectionsConfig;
  configuration_scoring?: ConfigurationScoringConfig;
  introduction?: string;
  hub_call_method?: "button" | "natural";
};

export type UpdateBotPayload = {
  name?: string;
  description?: string;
  status?: string;
  bot_kind?: "bot" | "hub";
  bot_mode?: "text" | "voice";
  profile_key?: "gray" | "accent" | "outline";
  profile_image_data?: string | null;
  language?: SupportedLanguage;
  nlu_engine?: NluModelKey;
  nlu_type?: NluType;
  nlu_model?: NluModelKey;
  answer_mode?: AnswerMode;
  llm_provider?: LlmProvider;
  llm_model?: LlmModelKey;
  llm_base_url?: string;
  vector_connections?: VectorConnectionsConfig;
  configuration_scoring?: ConfigurationScoringConfig;
  introduction?: string;
  settings_scope?: string;
  settings_json?: Partial<BotVersionSettings>;
};

type UpdateBotOptions = {
  responseMode?: "full" | "summary" | "settings";
  versionScope?: string;
};

export type UpdateVersionPayload = {
  name?: string;
  description?: string;
  status?: string;
  comment?: string;
  version_json?: VersionDocument;
};

export async function fetchStudioTalkTemplates(token: string, channelCode?: string) {
  const params = new URLSearchParams();
  if (channelCode) params.set("channel_code", channelCode);
  const search = params.toString() ? `?${params.toString()}` : "";
  return apiRequest<StudioTalkTemplatesResponse>(`/api/v1/bots/talk-templates${search}`, {}, token);
}

export async function acquireEditLock(token: string, target: EditLockTarget) {
  return apiRequest<EditLockResponse>(
    "/api/v1/edit-locks/acquire",
    {
      method: "POST",
      body: JSON.stringify(target),
    },
    token,
  );
}

export async function heartbeatEditLock(token: string, lockId: string) {
  return apiRequest<EditLockResponse>(
    "/api/v1/edit-locks/heartbeat",
    {
      method: "POST",
      body: JSON.stringify({ lock_id: lockId }),
    },
    token,
  );
}

export async function releaseEditLock(token: string, lockId: string) {
  return apiRequest<EditLockResponse>(
    "/api/v1/edit-locks/release",
    {
      method: "POST",
      body: JSON.stringify({ lock_id: lockId }),
    },
    token,
  );
}

export async function forceReleaseEditLock(token: string, lockId: string) {
  return apiRequest<EditLockResponse>(
    "/api/v1/edit-locks/force-release",
    {
      method: "POST",
      body: JSON.stringify({ lock_id: lockId }),
    },
    token,
  );
}

export async function fetchStudioBots(token: string, includeDocument = false) {
  const cacheKey = `${tokenFingerprint(token)}:${includeDocument ? "full" : "summary"}`;
  const cached = getCachedStudioBots(token);
  if (cached && !includeDocument) {
    return cached;
  }

  const pending = studioBotListRequests.get(cacheKey);
  if (pending) {
    return pending;
  }

  const search = includeDocument ? "?include_document=true" : "?include_document=false";
  const request = apiRequest<StudioBotApiItem[]>(`/api/v1/bots${search}`, {}, token)
    .then((data) => {
      if (!includeDocument) {
        studioBotListCache.set(tokenFingerprint(token), {
          data,
          expiresAt: Date.now() + STUDIO_BOT_LIST_CACHE_TTL_MS,
        });
        seedWorkspaceContextCacheFromBotList(token, data);
      }
      return data;
    })
    .finally(() => {
      studioBotListRequests.delete(cacheKey);
    });
  studioBotListRequests.set(cacheKey, request);
  return request;
}

export async function fetchStudioHubs(token: string) {
  return apiRequest<StudioHub[]>("/api/v1/hubs", {}, token);
}

export type StudioHubCandidate = {
  id: string;
  name: string;
  active_version_id: string | null;
  is_trained: boolean;
};

export async function fetchStudioHubCandidates(token: string, hubId: string) {
  return apiRequest<StudioHubCandidate[]>(`/api/v1/hubs/${encodeURIComponent(hubId)}/candidates`, {}, token);
}

export async function fetchStudioHub(token: string, hubId: string) {
  return apiRequest<StudioHub>(`/api/v1/hubs/${encodeURIComponent(hubId)}`, {}, token);
}

export async function updateStudioHubMembers(token: string, hubId: string, payload: UpdateStudioHubMembersPayload) {
  const result = await apiRequest<StudioHub>(
    `/api/v1/hubs/${encodeURIComponent(hubId)}/members`,
    { method: "PUT", body: JSON.stringify(payload) },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function updateStudioHubSettings(token: string, hubId: string, payload: UpdateStudioHubSettingsPayload) {
  const result = await apiRequest<StudioHub>(
    `/api/v1/hubs/${encodeURIComponent(hubId)}/settings`,
    { method: "PATCH", body: JSON.stringify(payload) },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export type StudioHubRetrainingCandidate = {
  queue_event_id: string;
  member_bot_id: string | null;
  member_bot_name: string | null;
  user_utterance: string;
  intent_name: string | null;
  channel_type: string;
  status: string;
  result_kind: "intent_classified" | "bot_unclassified" | "hub_unclassified" | "error";
  error_message: string | null;
  created_at: string | null;
};

export type StudioHubRetrainingCandidatesResponse = {
  hub_id: string;
  items: StudioHubRetrainingCandidate[];
  total: number;
  page: number;
  page_size: number;
  members: Array<{ bot_id: string; display_name: string | null }>;
};

export async function fetchStudioHubRetrainingCandidates(
  token: string,
  hubId: string,
  filters: {
    memberBotId?: string;
    channelType?: string;
    resultKind?: StudioHubRetrainingCandidate["result_kind"];
    page?: number;
    pageSize?: number;
  } = {},
) {
  const search = new URLSearchParams();
  if (filters.memberBotId) search.set("member_bot_id", filters.memberBotId);
  if (filters.channelType) search.set("channel_type", filters.channelType);
  if (filters.resultKind) search.set("result_kind", filters.resultKind);
  if (filters.page) search.set("page", String(filters.page));
  if (filters.pageSize) search.set("page_size", String(filters.pageSize));
  const query = search.size > 0 ? `?${search.toString()}` : "";
  return apiRequest<StudioHubRetrainingCandidatesResponse>(
    `/api/v1/hubs/${encodeURIComponent(hubId)}/retraining-candidates${query}`,
    {},
    token,
  );
}

export async function fetchStudioGroupApiBots(token: string) {
  return apiRequest<StudioBotApiItem[]>("/api/v1/bots/api-catalog", {}, token);
}

export async function fetchStudioGroupApis(token: string) {
  return apiRequest<StudioGroupApisApiResponse>("/api/v1/bots/group-apis", {}, token);
}

export async function updateStudioGroupApis(token: string, items: VersionDocument["apis"]) {
  const result = await apiRequest<StudioGroupApisApiResponse>(
    "/api/v1/bots/group-apis",
    {
      method: "PATCH",
      body: JSON.stringify({ items }),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function fetchStudioBot(token: string, botId: string) {
  return apiRequest<StudioBotApiItem>(`/api/v1/bots/${encodeURIComponent(botId)}`, {}, token);
}

export async function createStudioBot(token: string, payload: CreateBotPayload) {
  const result = await apiRequest<StudioBotApiItem>(
    "/api/v1/bots",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function updateStudioBot(token: string, botId: string, payload: UpdateBotPayload, options?: UpdateBotOptions) {
  const search = new URLSearchParams();
  if (options?.responseMode) {
    search.set("response_mode", options.responseMode);
  }
  if (options?.versionScope) {
    search.set("response_version_scope", options.versionScope);
  }
  const query = search.size > 0 ? `?${search.toString()}` : "";
  const result = await apiRequest<StudioBotApiItem>(
    `/api/v1/bots/${botId}${query}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function deleteStudioBot(token: string, botId: string) {
  const result = await apiRequest<{ message: string }>(
    `/api/v1/bots/${botId}`,
    {
      method: "DELETE",
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function updateStudioBotVersion(
  token: string,
  botId: string,
  versionId: string,
  payload: UpdateVersionPayload,
) {
  const result = await apiRequest<StudioBotVersionApiItem>(
    `/api/v1/bots/${botId}/versions/${versionId}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function fetchStudioBotVersions(token: string, botId: string, includeDocument = false) {
  const search = includeDocument ? "?include_document=true" : "";
  return apiRequest<StudioBotVersionApiItem[]>(`/api/v1/bots/${botId}/versions${search}`, {}, token);
}

export async function fetchStudioBotVersion(token: string, botId: string, versionId: string) {
  return apiRequest<StudioBotVersionApiItem>(`/api/v1/bots/${botId}/versions/${versionId}`, {}, token);
}

export async function fetchStudioBotVersionDialogs(token: string, botId: string, versionId: string) {
  return fetchStudioVersionSection<StudioVersionDialogsApiResponse>(
    token,
    botId,
    versionId,
    "dialogs",
    `/api/v1/bots/${botId}/versions/${versionId}/dialogs`,
  );
}

export async function updateStudioBotVersionDialogs(
  token: string,
  botId: string,
  versionId: string,
  items: VersionDocument["dialogs"],
) {
  const result = await apiRequest<StudioVersionDialogsApiResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/dialogs`,
    {
      method: "PATCH",
      body: JSON.stringify({ items }),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function fetchStudioBotVersionEntities(token: string, botId: string, versionId: string) {
  return fetchStudioVersionSection<StudioVersionEntitiesApiResponse>(
    token,
    botId,
    versionId,
    "entities",
    `/api/v1/bots/${botId}/versions/${versionId}/entities`,
  );
}

export async function updateStudioBotVersionEntities(
  token: string,
  botId: string,
  versionId: string,
  items: VersionDocument["entities"],
) {
  const result = await apiRequest<StudioVersionEntitiesApiResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/entities`,
    {
      method: "PATCH",
      body: JSON.stringify({ items }),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function fetchStudioBotVersionDictionary(token: string, botId: string, versionId: string) {
  return fetchStudioVersionSection<StudioVersionDictionaryApiResponse>(
    token,
    botId,
    versionId,
    "dictionary",
    `/api/v1/bots/${botId}/versions/${versionId}/dictionary`,
  );
}

export async function updateStudioBotVersionDictionary(
  token: string,
  botId: string,
  versionId: string,
  items: VersionDocument["dictionary"],
) {
  const result = await apiRequest<StudioVersionDictionaryApiResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/dictionary`,
    {
      method: "PATCH",
      body: JSON.stringify({ items }),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function fetchStudioBotVersionApis(token: string, botId: string, versionId: string) {
  return fetchStudioVersionSection<StudioVersionApisApiResponse>(
    token,
    botId,
    versionId,
    "apis",
    `/api/v1/bots/${botId}/versions/${versionId}/apis`,
  );
}

export async function updateStudioBotVersionApis(
  token: string,
  botId: string,
  versionId: string,
  items: VersionDocument["apis"],
) {
  const result = await apiRequest<StudioVersionApisApiResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/apis`,
    {
      method: "PATCH",
      body: JSON.stringify({ items }),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function fetchStudioBotVersionReferences(token: string, botId: string, versionId: string) {
  return fetchStudioVersionSection<StudioVersionReferencesApiResponse>(
    token,
    botId,
    versionId,
    "references",
    `/api/v1/bots/${botId}/versions/${versionId}/references`,
  );
}

export async function fetchStudioBotVersionDialogFlow(
  token: string,
  botId: string,
  versionId: string,
  dialogId: string,
) {
  return fetchStudioVersionSection<StudioVersionDialogFlowApiResponse>(
    token,
    botId,
    versionId,
    `dialog-flow:${dialogId}`,
    `/api/v1/bots/${botId}/versions/${versionId}/dialogs/${dialogId}/flow`,
  );
}

export async function updateStudioBotVersionDialogFlow(
  token: string,
  botId: string,
  versionId: string,
  dialogId: string,
  payload: Pick<StudioVersionDialogFlowApiResponse, "dialog"> & {
    graph: Record<string, unknown>;
  },
) {
  const result = await apiRequest<StudioVersionDialogFlowApiResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/dialogs/${dialogId}/flow`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function fetchStudioBotVersionConfigure(
  token: string,
  botId: string,
  versionId: string,
  includeDialogFlowGraphs = true,
) {
  const search = includeDialogFlowGraphs ? "" : "?include_dialog_flow_graphs=false";
  return fetchStudioVersionSection<StudioVersionConfigureApiResponse>(
    token,
    botId,
    versionId,
    includeDialogFlowGraphs ? "configure" : "configure:no-flow",
    `/api/v1/bots/${botId}/versions/${versionId}/configure${search}`,
  );
}

export async function updateStudioBotVersionConfigure(
  token: string,
  botId: string,
  versionId: string,
  payload: Pick<StudioVersionConfigureApiResponse, "dialogs" | "dialog_flow_graphs">,
) {
  const result = await apiRequest<StudioVersionConfigureApiResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/configure`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function fetchStudioBotVersionRetraining(token: string, botId: string, versionId: string) {
  return fetchStudioVersionSection<StudioVersionRetrainingApiResponse>(
    token,
    botId,
    versionId,
    "retraining",
    `/api/v1/bots/${botId}/versions/${versionId}/retraining`,
  );
}

export function prefetchStudioBotVersionCoreSections(token: string, botId: string, versionId: string) {
  void Promise.allSettled([
    fetchStudioBotVersionDialogs(token, botId, versionId),
    fetchStudioBotVersionEntities(token, botId, versionId),
    fetchStudioBotVersionDictionary(token, botId, versionId),
  ]);
}

export async function updateStudioBotVersionRetraining(
  token: string,
  botId: string,
  versionId: string,
  payload: Pick<StudioVersionRetrainingApiResponse, "dialogs" | "system_config">,
) {
  const result = await apiRequest<StudioVersionRetrainingApiResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/retraining`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function fetchStudioWorkspaceContext(
  token: string,
  botId: string,
  versionScope: string,
  includeDocument = false,
  prefetchSection?: WorkspacePrefetchSection,
  userLoginId?: string,
  allowFullFallback = true,
) {
  const cacheKey = workspaceContextCacheKey(token, botId, versionScope, includeDocument, prefetchSection);
  const userCacheKey = workspaceContextUserCacheKey(userLoginId, botId, versionScope, includeDocument, prefetchSection);
  const cached = getCachedStudioWorkspaceContext(
    token,
    botId,
    versionScope,
    includeDocument,
    false,
    prefetchSection,
    userLoginId,
    allowFullFallback,
  );
  if (cached) {
    return cached;
  }

  const pending = workspaceContextRequests.get(cacheKey);
  if (pending) {
    return pending;
  }

  const search = new URLSearchParams();
  search.set("include_document", includeDocument ? "true" : "false");
  if (prefetchSection) {
    search.set("prefetch_section", prefetchSection);
  }
  const query = search.size > 0 ? `?${search.toString()}` : "";
  const request = apiRequest<StudioWorkspaceContextApiResponse>(
    `/api/v1/bots/${encodeURIComponent(botId)}/versions/${encodeURIComponent(versionScope)}/context${query}`,
    {},
    token,
  )
    .then((data) => {
      const entry = {
        data,
        expiresAt: Date.now() + WORKSPACE_CONTEXT_CACHE_TTL_MS,
      };
      workspaceContextCache.set(cacheKey, {
        ...entry,
      });
      storeWorkspaceContext(cacheKey, entry);
      if (userCacheKey) {
        workspaceContextCache.set(userCacheKey, entry);
        storeUserWorkspaceContext(userCacheKey, entry);
      }
      if (prefetchSection === "dialogs" && data.version && Array.isArray(data.version.version_json?.dialogs)) {
        versionSectionCache.set(versionSectionCacheKey(token, data.version.bot_id, data.version.id, "dialogs"), {
          data: {
            bot_id: data.bot.id,
            version_id: data.version.id,
            section: "dialogs",
            version: data.version,
            items: data.version.version_json.dialogs,
            asset_counts: data.counts,
            scenario_validation: data.version.scenario_validation,
            updated_at: data.version.updated_at,
          },
          expiresAt: Date.now() + VERSION_SECTION_CACHE_TTL_MS,
        });
      }
      return data;
    })
    .finally(() => {
      workspaceContextRequests.delete(cacheKey);
    });
  workspaceContextRequests.set(cacheKey, request);
  return request;
}

export async function fetchStudioBotSettingsContext(token: string, botId: string, versionScope: string) {
  const cacheKey = settingsContextCacheKey(token, botId, versionScope);
  const cached = settingsContextCache.get(cacheKey);
  if (cached && cached.expiresAt > Date.now()) {
    return cached.data;
  }
  if (cached) {
    settingsContextCache.delete(cacheKey);
  }

  const pending = settingsContextRequests.get(cacheKey);
  if (pending) {
    return pending;
  }

  const request = apiRequest<StudioWorkspaceContextApiResponse>(
    `/api/v1/bots/${encodeURIComponent(botId)}/versions/${encodeURIComponent(versionScope)}/settings-context`,
    {},
    token,
  )
    .then((data) => {
      settingsContextCache.set(cacheKey, {
        data,
        expiresAt: Date.now() + SETTINGS_CONTEXT_CACHE_TTL_MS,
      });
      return data;
    })
    .finally(() => {
      settingsContextRequests.delete(cacheKey);
    });
  settingsContextRequests.set(cacheKey, request);
  return request;
}

export type LlmIntentConfigureGroup = {
  id: string;
  name: string;
  answer: string;
  utterances: string[];
  reason?: string;
};

export type LlmIntentConfigureResponse = {
  provider: string;
  model: string;
  latency_ms: number;
  target_count: number;
  target_count_policy: "minimize" | "near" | "exact";
  groups: LlmIntentConfigureGroup[];
  raw_content?: string;
};

export async function configureStudioBotVersionLlmIntents(
  token: string,
  botId: string,
  versionId: string,
  payload: {
    utterances: string[];
    target_count: number;
    target_count_policy?: "minimize" | "near" | "exact";
    dictionary_terms?: Array<Record<string, unknown>>;
    entity_terms?: Array<Record<string, unknown>>;
    llm_provider?: string;
    llm_model?: string;
    llm_base_url?: string;
  },
) {
  return apiRequest<LlmIntentConfigureResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/nlu/llm/configure`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
}

export async function configureStudioBotVersionSemanticIntents(
  token: string,
  botId: string,
  versionId: string,
  payload: {
    utterances: string[];
    target_count: number;
    target_count_policy?: "minimize" | "near" | "exact";
    dictionary_terms?: Array<Record<string, unknown>>;
    entity_terms?: Array<Record<string, unknown>>;
    scoring?: Record<string, number>;
  },
) {
  return apiRequest<LlmIntentConfigureResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/nlu/semantic/configure`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
}

export async function configureStudioBotVersionMlIntents(
  token: string,
  botId: string,
  versionId: string,
  payload: {
    utterances: string[];
    target_count: number;
    target_count_policy?: "minimize" | "near" | "exact";
    seed_intents?: Array<{
      name: string;
      representative_utterances: string[];
    }>;
  },
) {
  return apiRequest<LlmIntentConfigureResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/nlu/ml/configure`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
}

export type MlIntentTokenizeResponse = {
  morph_analyzer: "kiwipiepy";
  items: Array<{
    text: string;
    tokens: string[];
  }>;
};

export async function tokenizeStudioBotVersionMlIntents(
  token: string,
  botId: string,
  versionId: string,
  utterances: string[],
) {
  return apiRequest<MlIntentTokenizeResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/nlu/ml/tokenize`,
    {
      method: "POST",
      body: JSON.stringify({ utterances }),
    },
    token,
  );
}

export type LlmIntentTestCandidate = {
  intent_id: string;
  intentId: string;
  intent_name: string;
  intentName: string;
  confidence: number;
  reason?: string;
};

export type LlmIntentTestResponse = {
  provider: string;
  model: string;
  latency_ms: number;
  utterance: string;
  top_k: number;
  candidates: LlmIntentTestCandidate[];
  raw_content?: string;
};

export type SemanticIntentTestResponse = Omit<LlmIntentTestResponse, "raw_content">;

export type MlIntentTestResponse = SemanticIntentTestResponse & {
  cutoff_score: number;
  similar_intent_score: number;
  max_intent_results: number;
};

export async function testStudioBotVersionMlIntent(
  token: string,
  botId: string,
  versionId: string,
  payload: {
    utterance: string;
    top_k?: number;
  },
) {
  return apiRequest<MlIntentTestResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/nlu/ml/test`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
}
export async function testStudioBotVersionLlmIntent(
  token: string,
  botId: string,
  versionId: string,
  payload: {
    utterance: string;
    top_k?: number;
  },
) {
  return apiRequest<LlmIntentTestResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/nlu/llm/test`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
}

export async function testStudioBotVersionSemanticIntent(
  token: string,
  botId: string,
  versionId: string,
  payload: {
    utterance: string;
    top_k?: number;
  },
) {
  return apiRequest<SemanticIntentTestResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/nlu/semantic/test`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
}

export type RagAnswerSearchMatch = {
  documentId?: string;
  title?: string;
  text?: string;
  score?: number;
  intentId?: string;
  intentName?: string;
  sourceType?: string;
  sourceTitle?: string;
  page?: string;
  metadata?: Record<string, unknown>;
};

export type RagAnswerSearchResponse = {
  answer_mode?: string;
  answerMode?: string;
  prefix?: string;
  matches?: RagAnswerSearchMatch[];
  variables?: Record<string, unknown>;
};

export async function searchStudioBotVersionRagAnswers(
  token: string,
  botId: string,
  versionId: string,
  payload: {
    query: string;
    intentId?: string;
    intentName?: string;
    topK?: number;
  },
) {
  return apiRequest<RagAnswerSearchResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/answers/rag/search`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
}

export async function generateStudioBotVersionLlmAnswer(
  token: string,
  botId: string,
  versionId: string,
  payload: {
    query: string;
    intentId?: string;
    intentName?: string;
  },
) {
  return apiRequest<RagAnswerSearchResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/answers/llm/generate`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
}

export async function refreshStudioBotSettingsContext(
  token: string,
  botId: string,
  versionHint?: string | null,
) {
  const context = await fetchStudioBotSettingsContext(token, botId, versionHint || "v1");
  const contextVersion = context.version ?? context.bot.active_version ?? null;
  const versionWithDialogs =
    contextVersion && !Array.isArray(contextVersion.version_json?.dialogs)
      ? await fetchStudioBotVersionDialogs(token, context.bot.id, contextVersion.id)
          .then((data) => ({
            ...contextVersion,
            version_json: {
              ...normalizeVersionDocument(contextVersion.version_json),
              dialogs: data.items,
            },
            asset_counts: data.asset_counts ?? contextVersion.asset_counts,
            scenario_validation: data.scenario_validation ?? contextVersion.scenario_validation,
            updated_at: data.updated_at ?? contextVersion.updated_at,
          }))
          .catch(() => contextVersion)
      : contextVersion;
  return {
    bot: {
      ...context.bot,
      active_version: versionWithDialogs,
    },
    versions: context.versions,
    version: versionWithDialogs,
  };
}


export async function refreshStudioBotSelectedVersion(
  token: string,
  botId: string,
  versionHint?: string | null,
  currentVersionId?: string | null,
  includeDocument = false,
) {
  const context = await fetchStudioWorkspaceContext(token, botId, currentVersionId || versionHint || "v1", includeDocument);
  const bot = context.bot;
  const versions = context.versions;
  const normalizedCurrentVersionId = String(currentVersionId ?? "").trim().toLowerCase();
  const normalizedVersionHint = String(versionHint ?? "").trim().toLowerCase();
  const matchesVersionHint = (version: StudioBotVersionApiItem, hint: string) => (
    version.id.toLowerCase() === hint ||
    version.name.toLowerCase() === hint ||
    `v${version.version_no}`.toLowerCase() === hint ||
    String(version.version_no) === hint
  );
  const selectedVersion =
    (normalizedCurrentVersionId
      ? versions.find((version) => matchesVersionHint(version, normalizedCurrentVersionId))
      : null) ??
    (normalizedVersionHint
      ? versions.find((version) => matchesVersionHint(version, normalizedVersionHint))
      : null) ??
    bot.active_version ??
    versions[0] ??
    null;
  const selectedVersionDetail =
    context.version && selectedVersion && context.version.id === selectedVersion.id
      ? context.version
      : selectedVersion && includeDocument
        ? await fetchStudioBotVersion(token, bot.id, selectedVersion.id).catch(() => selectedVersion)
        : null;
  const effectiveVersion = versionWithWorkspaceCounts(selectedVersionDetail ?? selectedVersion, context.counts);

  return {
    bot: {
      ...bot,
      active_version: effectiveVersion,
    },
    versions,
    version: effectiveVersion,
  };
}

export async function createStudioBotVersion(
  token: string,
  botId: string,
  payload: { name?: string; description?: string; comment?: string },
) {
  const result = await apiRequest<StudioBotVersionApiItem>(
    `/api/v1/bots/${botId}/versions`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function copyStudioBotVersion(token: string, botId: string, versionId: string) {
  const result = await apiRequest<StudioBotVersionApiItem>(
    `/api/v1/bots/${botId}/versions/${versionId}/copy`,
    {
      method: "POST",
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export type StudioBotVersionPackage = {
  schema_version?: string;
  bot?: {
    id?: string;
    name?: string;
  };
  version?: {
    id?: string;
    name?: string;
    version_no?: number;
    status?: string;
    comment?: string | null;
    description?: string | null;
  };
  version_json?: VersionDocument;
  answer_vector_index?: Record<string, unknown> | null;
};

export async function exportStudioBotVersionPackage(token: string, botId: string, versionId: string) {
  return apiRequest<StudioBotVersionPackage>(
    `/api/v1/bots/${botId}/versions/${versionId}/export`,
    {},
    token,
  );
}

export type StudioAidotBotPackage = {
  AIDOTAssistantVersion?: unknown;
  messageDigest?: unknown;
  botVo?: Record<string, unknown>;
  licenseVo?: unknown;
  botSystemConfigVoList?: unknown[];
  dialogList?: unknown[];
  dialogFlowGraphList?: unknown[];
  entityTypeList?: unknown[];
  faqDialogList?: unknown[];
  floatingButtonVoList?: unknown[];
  ruleVoList?: unknown[];
  smallTalkVoList?: unknown[];
  dictionaryVoList?: unknown[];
  blacklistList?: unknown[];
  [key: string]: unknown;
};

export function isStudioAidotBotPackage(value: unknown): value is StudioAidotBotPackage {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const source = value as Record<string, unknown>;
  const packageBody =
    source.package && typeof source.package === "object" && !Array.isArray(source.package)
      ? (source.package as Record<string, unknown>)
      : source;
  return Boolean(
    packageBody.botVo &&
      typeof packageBody.botVo === "object" &&
      !Array.isArray(packageBody.botVo) &&
      ("AIDOTAssistantVersion" in packageBody ||
        "dialogList" in packageBody ||
        "entityTypeList" in packageBody ||
        "dictionaryVoList" in packageBody),
  );
}

export async function exportStudioBotAidotPackage(token: string, botId: string, versionId: string) {
  return apiRequest<StudioAidotBotPackage>(
    `/api/v1/bots/${botId}/versions/${versionId}/aidot-package`,
    {},
    token,
  );
}

export async function importStudioBotAidotPackage(
  token: string,
  botId: string,
  versionId: string,
  payload: StudioAidotBotPackage,
) {
  const result = await apiRequest<{
    version: StudioBotVersionApiItem;
    compatibility: Record<string, unknown>;
  }>(
    `/api/v1/bots/${botId}/versions/${versionId}/aidot-package`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function importStudioBotVersionRagAnswerIndex(
  token: string,
  botId: string,
  versionId: string,
  answerVectorIndex: Record<string, unknown> | null | undefined,
) {
  const result = await apiRequest<{ status?: string; imported?: number }>(
    `/api/v1/bots/${botId}/versions/${versionId}/answers/rag/import-index`,
    {
      method: "POST",
      body: JSON.stringify({ answer_vector_index: answerVectorIndex ?? null }),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function deleteStudioBotVersion(token: string, botId: string, versionId: string) {
  const result = await apiRequest<{ message: string }>(
    `/api/v1/bots/${botId}/versions/${versionId}`,
    {
      method: "DELETE",
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function activateStudioBotVersion(token: string, botId: string, versionId: string) {
  const result = await apiRequest<StudioBotVersionApiItem>(
    `/api/v1/bots/${botId}/versions/${versionId}/activate`,
    {
      method: "POST",
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function deactivateStudioBotVersion(token: string, botId: string, versionId: string) {
  const result = await apiRequest<StudioBotVersionApiItem>(
    `/api/v1/bots/${botId}/versions/${versionId}/deactivate`,
    {
      method: "POST",
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export type NluModelManifest = {
  exists?: boolean;
  model_path: string;
  answer_training?: {
    status?: string;
    trained_at?: string;
    answer_mode?: string;
    document_profile?: string;
    chunk_strategy?: string;
    embedding_provider?: string;
    embedding_model?: string;
    document_count?: number;
    indexed?: number;
  } | null;
  evaluation?: {
    trained_at: string;
    random_accuracy: number | null;
    fixed_accuracy: number | null;
    gap: number | null;
    intent_count: number;
    training_utterance_count: number;
    validation_utterance_count: number;
    random_train_count: number;
    random_test_count: number;
  };
  model: {
    schema_version: number;
    engine_type: "ml";
    model: "deep_learning_lite";
    morph_analyzer: "kiwipiepy";
    bot_id: string;
    version_id: string;
    trained_at: string;
    counts: {
      intent_documents: number;
      entity_documents: number;
      intents: number;
      entities: number;
      vocabulary: number;
    };
  } | null;
};

export type RagAnswerTrainingSource = {
  source_type: "text" | "pdf";
  title?: string;
  text?: string;
  file_name?: string;
  mime_type?: string;
  file_base64?: string;
  embedding_provider?: string;
  embedding_model?: string;
};

export type VersionNluTrainPayload = {
  answer_training?: RagAnswerTrainingSource | null;
};

export type NluTrainingJob = {
  job_id: string;
  operation: "nlu.train";
  status: "queued" | "processing" | "completed" | "failed";
  receive_status: string;
  requested_at?: string | null;
  status_changed_at?: string | null;
  error_message?: string | null;
  manifest?: NluModelManifest | null;
};

type NluTrainingOptions = {
  signal?: AbortSignal;
  onStatus?: (job: NluTrainingJob) => void;
};

export type RagAnswerEmbeddingResult = {
  status?: string;
  trained_at?: string;
  answer_mode?: string;
  provider?: string;
  index_name?: string;
  endpoint_url?: string;
  document_profile?: string;
  document_title?: string;
  detected_document_title?: string;
  title_edited?: boolean;
  chunk_strategy?: string;
  embedding_provider?: string;
  embedding_model?: string;
  embedding_selection?: string;
  document_count?: number;
  indexed?: number;
};

export type RagAnswerConfigureResponse = LlmIntentConfigureResponse & {
  answer_training?: RagAnswerEmbeddingResult | null;
};

async function requestStudioMultipartProxy<T>(
  proxyPath: string,
  token: string,
  botId: string,
  versionId: string,
  formData: FormData,
) {
  let response: Response;
  try {
    response = await fetch(
      `${proxyPath}?botId=${encodeURIComponent(botId)}&versionId=${encodeURIComponent(versionId)}`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
        },
        body: formData,
        cache: "no-store",
      },
    );
  } catch (error) {
    throw new Error(
      error instanceof TypeError
        ? "API 서버에 연결할 수 없습니다. 서버 실행 상태와 API 주소를 확인해주세요."
        : "API 요청 중 오류가 발생했습니다.",
    );
  }

  const rawText = await response.text();
  const payload = (rawText
    ? await Promise.resolve().then(() => JSON.parse(rawText)).catch(() => null)
    : null) as { data?: T; detail?: unknown } | null;
  if (response.status === 401) {
    throw new Error("로그인이 필요합니다.");
  }
  if (!response.ok) {
    const detailMessage = payload && "detail" in payload ? formatApiErrorDetail(payload.detail) : "";
    const clippedText = rawText.trim().slice(0, 300);
    throw new Error(detailMessage || `요청 처리 중 오류가 발생했습니다. HTTP ${response.status}${clippedText ? `: ${clippedText}` : ""}`);
  }
  if (!payload || !("data" in payload) || typeof payload.data === "undefined") {
    throw new Error("응답 형식이 올바르지 않습니다.");
  }
  return payload.data;
}

async function requestStudioMultipartWithFallback<T>(
  directPath: string,
  proxyPath: string,
  token: string,
  botId: string,
  versionId: string,
  formData: FormData,
) {
  try {
    return await apiRequest<T>(
      directPath,
      {
        method: "POST",
        body: formData,
      },
      token,
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (message !== "API 서버에 연결할 수 없습니다. 서버 실행 상태와 API 주소를 확인해주세요.") {
      throw error;
    }
  }

  return requestStudioMultipartProxy<T>(proxyPath, token, botId, versionId, formData);
}

export async function configureStudioBotVersionRagAnswers(
  token: string,
  botId: string,
  versionId: string,
  payload: {
    answer_training: RagAnswerTrainingSource;
    target_count: number;
    target_count_policy?: "minimize" | "near" | "exact";
  },
) {
  const result = await apiRequest<RagAnswerConfigureResponse>(
    `/api/v1/bots/${botId}/versions/${versionId}/answers/rag/configure`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function configureStudioBotVersionRagAnswerPdf(
  token: string,
  botId: string,
  versionId: string,
  options: {
    file: File;
    title?: string;
    embedding_provider?: string;
    embedding_model?: string;
    target_count: number;
    target_count_policy?: "minimize" | "near" | "exact";
  },
) {
  const formData = new FormData();
  formData.append("file", options.file);
  if (options.title) {
    formData.append("title", options.title);
  }
  if (options.embedding_provider) {
    formData.append("embedding_provider", options.embedding_provider);
  }
  if (options.embedding_model) {
    formData.append("embedding_model", options.embedding_model);
  }
  formData.append("target_count", String(options.target_count));
  formData.append("target_count_policy", options.target_count_policy || "near");
  const directPath = `/api/v1/bots/${botId}/versions/${versionId}/answers/rag/configure-pdf`;
  const result = await requestStudioMultipartWithFallback<RagAnswerConfigureResponse>(
    directPath,
    "/api/studio/rag/configure-pdf",
    token,
    botId,
    versionId,
    formData,
  );
  clearStudioClientCaches();
  return result;
}

export async function embedStudioBotVersionRagAnswers(
  token: string,
  botId: string,
  versionId: string,
  payload: VersionNluTrainPayload,
) {
  const result = await apiRequest<RagAnswerEmbeddingResult>(
    `/api/v1/bots/${botId}/versions/${versionId}/answers/rag/embed`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    token,
  );
  clearStudioClientCaches();
  return result;
}

export async function embedStudioBotVersionRagAnswerPdf(
  token: string,
  botId: string,
  versionId: string,
  options: {
    file: File;
    title?: string;
    embedding_provider?: string;
    embedding_model?: string;
  },
) {
  const formData = new FormData();
  formData.append("file", options.file);
  if (options.title) {
    formData.append("title", options.title);
  }
  if (options.embedding_provider) {
    formData.append("embedding_provider", options.embedding_provider);
  }
  if (options.embedding_model) {
    formData.append("embedding_model", options.embedding_model);
  }
  const directPath = `/api/v1/bots/${botId}/versions/${versionId}/answers/rag/embed-pdf`;
  const result = await requestStudioMultipartWithFallback<RagAnswerEmbeddingResult>(
    directPath,
    "/api/studio/rag/embed-pdf",
    token,
    botId,
    versionId,
    formData,
  );
  clearStudioClientCaches();
  return result;
}

function waitForNluTrainingPoll(signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const handleAbort = () => {
      clearTimeout(timeoutId);
      signal?.removeEventListener("abort", handleAbort);
      reject(new DOMException("학습 상태 조회가 취소되었습니다.", "AbortError"));
    };
    const timeoutId = setTimeout(() => {
      signal?.removeEventListener("abort", handleAbort);
      resolve();
    }, 1500);
    if (signal?.aborted) {
      handleAbort();
      return;
    }
    signal?.addEventListener("abort", handleAbort, { once: true });
  });
}

export async function trainStudioBotVersionNlu(
  token: string,
  botId: string,
  versionId: string,
  payload?: VersionNluTrainPayload,
  options: NluTrainingOptions = {},
) {
  let job = await apiSameOriginRequest<NluTrainingJob>(
    `/api/v1/bots/${botId}/versions/${versionId}/nlu/train`,
    {
      method: "POST",
      body: payload ? JSON.stringify(payload) : undefined,
      signal: options.signal,
    },
    token,
  );
  options.onStatus?.(job);

  while (job.status === "queued" || job.status === "processing") {
    await waitForNluTrainingPoll(options.signal);
    job = await apiSameOriginRequest<NluTrainingJob>(
      `/api/v1/bots/${botId}/versions/${versionId}/nlu/train/jobs/${job.job_id}`,
      { signal: options.signal },
      token,
    );
    options.onStatus?.(job);
  }

  if (job.status === "failed") {
    throw new Error(job.error_message || "학습 작업이 실패했습니다. Queue 이력에서 상세 원인을 확인해주세요.");
  }
  if (!job.manifest) {
    throw new Error("학습 작업은 완료됐지만 학습 결과를 확인할 수 없습니다.");
  }
  clearStudioClientCaches();
  return job.manifest;
}

export async function fetchStudioBotVersionNluModel(token: string, botId: string, versionId: string) {
  return apiRequest<NluModelManifest>(`/api/v1/bots/${botId}/versions/${versionId}/nlu/model`, {}, token);
}

export async function deleteStudioHub(token: string, hubId: string) {
  const result = await apiRequest<{ message: string }>(
    `/api/v1/hubs/${encodeURIComponent(hubId)}`,
    { method: "DELETE" },
    token,
  );
  clearStudioClientCaches();
  return result;
}
