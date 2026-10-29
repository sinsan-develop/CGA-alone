import { type AnswerMode } from "@/lib/answer-options";
import { type LlmModelKey, type LlmProvider } from "@/lib/llm-options";
import { type SupportedLanguage } from "@/lib/language";
import { type NluModelKey, type NluType } from "@/lib/nlu-options";

export type ConversationDefaultsConfig = {
  ml: {
    cutOffScore: number;
    similarIntentScore: number;
    maxIntentResults: number;
  };
  qa: {
    faqCutOffScore: number;
    extractiveCutOffScore: number;
    searchMaxResults: number;
    faqMaxIntentResults: number;
    extractiveMaxIntentResults: number;
    answerPriority: "ml-first" | "qa-first";
  };
  timeout: {
    enabled: boolean;
    seconds: number;
    applyToPushMessage: boolean;
  };
  entityPrompt: {
    maxRepeatCount: number;
  };
  intentDetection: {
    retryCount: number;
    overflowModule: string;
    preprocessModule: string;
    beforeSessionEndModule: string;
    multiIntentButtonModule: string;
  };
  voice: {
    ttsUrl: string;
  };
  validation: {
    mode: "random" | "fixed";
    imbalanceOversampling: boolean;
  };
  buttonSelection: {
    option: "exact" | "contains";
  };
  exactingMatching: {
    enabled: boolean;
  };
  runtime: {
    maxCardsBetweenUserResponses: number;
  };
  llmAnswer: {
    systemPrompt: string;
  };
};

export type MessageItemConfig = {
  enabled: boolean;
  mode: "text" | "module";
  value: string;
};

export type MessageSettingsConfig = {
  greeting: MessageItemConfig;
  fallback: MessageItemConfig;
  intentEndGuide: MessageItemConfig;
  buttonMismatch: MessageItemConfig;
  multiIntentGuide: {
    enabled: boolean;
    message: string;
    noIntentButtonLabel: string;
    noIntentButtonMessage: string;
  };
  system: {
    errorMessage: MessageItemConfig;
    timeoutMessage: MessageItemConfig;
    sessionEndMessage: MessageItemConfig;
    inProgressMessage: MessageItemConfig;
  };
  intentSwitch: {
    maxExceededMessage: string;
    beforeIntentNameMessage: string;
    afterIntentNameMessage: string;
  };
  intentReturn: {
    enabled: boolean;
    message: string;
  };
  parallelWork: {
    firstMessage: string;
    skipButtonLabel: string;
    skipButtonMessage: string;
    forcedPriorityMessage: string;
    forcedCloseMessage: string;
  };
  feedback: {
    mode: "none" | "all" | "per-intent";
    promptMessage: string;
    scale: "binary" | "five-point";
    scaleLabels: {
      one: string;
      two: string;
      three: string;
      four: string;
      five: string;
    };
  };
};

export type FloatingButtonConfig = {
  id: string;
  label: string;
  actionType: "key" | "command";
  actionValue: string;
  enabled: boolean;
};

export type RecommendedIntentConfig = {
  id: string;
  intentId: string;
  label: string;
};

export type BlocklistConfig = {
  id: string;
  name: string;
  type: "word" | "regex";
  pattern: string;
  description: string;
  enabled: boolean;
  updatedBy?: string;
  updatedAt?: string;
};

export type RuleConfig = {
  id: string;
  name: string;
  description: string;
  expression: string;
  target: string;
  enabled: boolean;
  updatedBy?: string;
  updatedAt?: string;
};

export type SmalltalkPriority = "High" | "Medium" | "Low";

export type SmalltalkItemConfig = {
  id: string;
  title: string;
  priority?: SmalltalkPriority;
  utterance: string;
  response: string;
  userMessages?: string[];
  botMessages?: string[];
  enabled: boolean;
  createdBy?: string;
  createdAt?: string;
  updatedBy?: string;
  updatedAt?: string;
};

export type SmalltalkSettingsConfig = {
  enabled: boolean;
  items: SmalltalkItemConfig[];
  fallbackResponse: string;
};

export type BotStationChannelConfig = {
  id: string;
  channelId: string;
  channelCode: string;
  channelName: string;
  botIdentifier: string;
  botName: string;
  name: string;
  enabled: boolean;
  appId: string;
  appSecret: string;
  callbackUrl: string;
  description: string;
  updatedAt: string;
};

export type BotStationSettingsConfig = {
  connected: boolean;
  enabled: boolean;
  connectedAt: string;
  channels: BotStationChannelConfig[];
};

export type VectorConnectionConfig = {
  enabled: boolean;
  endpoint_url: string;
  index_name: string;
  api_key: string;
};

export type VectorConnectionsConfig = {
  intent?: VectorConnectionConfig;
  answer?: VectorConnectionConfig;
};

export type ConfigurationScoringConfig = {
  dictionaryWeight: number;
  entityWeight: number;
  wordWeight: number;
  gramWeight: number;
  particleEndingWeight: number;
  keyMatchScore: number;
};

export type BotVersionSettings = {
  conversationDefaults: ConversationDefaultsConfig;
  messages: MessageSettingsConfig;
  floatingButtons: FloatingButtonConfig[];
  recommendedIntents: RecommendedIntentConfig[];
  blocklists: BlocklistConfig[];
  rules: RuleConfig[];
  smalltalk: SmalltalkSettingsConfig;
  botstation: BotStationSettingsConfig;
};

export type SecondaryLlmSettings = {
  module_id: string;
  outcome?: { variableName: string; answeredValue: string; unrecognizedValue: string };
};

export type StudioBotDataJson = {
  secondary_llm?: SecondaryLlmSettings;
  bot_kind?: "bot" | "hub";
  bot_mode?: "text" | "voice";
  profile_key?: "gray" | "accent" | "outline";
  language?: SupportedLanguage;
  nlu_engine?: NluModelKey;
  nlu_type?: NluType;
  nlu_model?: NluModelKey;
  answer_mode?: AnswerMode;
  llm_provider?: LlmProvider;
  llm_model?: LlmModelKey;
  llm_base_url?: string | null;
  vector_connections?: VectorConnectionsConfig;
  configuration_scoring?: Partial<ConfigurationScoringConfig>;
  introduction?: string | null;
  settings_by_version?: Record<string, Partial<BotVersionSettings>>;
};

export const DEFAULT_CONFIGURATION_SCORING: ConfigurationScoringConfig = {
  dictionaryWeight: 1.2,
  entityWeight: 1.2,
  wordWeight: 1,
  gramWeight: 0.2,
  particleEndingWeight: 0.05,
  keyMatchScore: 0.82,
};

function numberSetting(value: unknown, fallback: number) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return fallback;
}

export function normalizeConfigurationScoring(value?: Partial<ConfigurationScoringConfig> | null): ConfigurationScoringConfig {
  return {
    dictionaryWeight: numberSetting(value?.dictionaryWeight, DEFAULT_CONFIGURATION_SCORING.dictionaryWeight),
    entityWeight: numberSetting(value?.entityWeight, DEFAULT_CONFIGURATION_SCORING.entityWeight),
    wordWeight: numberSetting(value?.wordWeight, DEFAULT_CONFIGURATION_SCORING.wordWeight),
    gramWeight: numberSetting(value?.gramWeight, DEFAULT_CONFIGURATION_SCORING.gramWeight),
    particleEndingWeight: numberSetting(value?.particleEndingWeight, DEFAULT_CONFIGURATION_SCORING.particleEndingWeight),
    keyMatchScore: numberSetting(value?.keyMatchScore, DEFAULT_CONFIGURATION_SCORING.keyMatchScore),
  };
}

export const DEFAULT_VERSION_SETTINGS: BotVersionSettings = {
  conversationDefaults: {
    ml: {
      cutOffScore: 0.75,
      similarIntentScore: 0.85,
      maxIntentResults: 3,
    },
    qa: {
      faqCutOffScore: 0.5,
      extractiveCutOffScore: 0.5,
      searchMaxResults: 3,
      faqMaxIntentResults: 1,
      extractiveMaxIntentResults: 3,
      answerPriority: "qa-first",
    },
    timeout: {
      enabled: true,
      seconds: 120,
      applyToPushMessage: false,
    },
    entityPrompt: {
      maxRepeatCount: 2,
    },
    intentDetection: {
      retryCount: 2,
      overflowModule: "",
      preprocessModule: "",
      beforeSessionEndModule: "",
      multiIntentButtonModule: "",
    },
    voice: {
      ttsUrl: "",
    },
    validation: {
      mode: "random",
      imbalanceOversampling: false,
    },
    buttonSelection: {
      option: "exact",
    },
    exactingMatching: {
      enabled: true,
    },
    runtime: {
      maxCardsBetweenUserResponses: 100,
    },
    llmAnswer: {
      systemPrompt: "",
    },
  },
  messages: {
    greeting: { enabled: true, mode: "text", value: "안녕하세요." },
    fallback: { enabled: true, mode: "text", value: "질문을 이해하지 못했습니다. 다시 말씀해주세요." },
    intentEndGuide: { enabled: true, mode: "text", value: "다음으로 필요한 업무를 말씀해주세요." },
    buttonMismatch: { enabled: true, mode: "text", value: "목록에 있는 버튼 중 하나를 선택해주세요." },
    multiIntentGuide: {
      enabled: true,
      message: "아래 후보 중 원하는 의도를 선택해주세요.",
      noIntentButtonLabel: "원하는 의도 없음",
      noIntentButtonMessage: "다시 질문해주시면 다른 의도를 찾겠습니다.",
    },
    system: {
      errorMessage: { enabled: true, mode: "text", value: "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요." },
      timeoutMessage: { enabled: true, mode: "text", value: "응답이 없어 대화를 종료합니다." },
      sessionEndMessage: { enabled: true, mode: "text", value: "세션이 종료되었습니다. 다시 시작하려면 말씀해주세요." },
      inProgressMessage: { enabled: true, mode: "text", value: "현재 진행 중인 대화가 있습니다." },
    },
    intentSwitch: {
      maxExceededMessage: "의도 전환 시도가 많아 현재 대화를 유지합니다.",
      beforeIntentNameMessage: "지금",
      afterIntentNameMessage: "의도로 이동할까요?",
    },
    intentReturn: {
      enabled: true,
      message: "이전 의도로 돌아갈까요?",
    },
    parallelWork: {
      firstMessage: "진행 중인 업무 외에 추가로 처리할 업무가 있습니다.",
      skipButtonLabel: "처리하지 않음",
      skipButtonMessage: "현재 업무만 계속 진행하겠습니다.",
      forcedPriorityMessage: "우선순위가 높은 업무를 먼저 진행합니다.",
      forcedCloseMessage: "진행 중인 업무가 종료되었습니다.",
    },
    feedback: {
      mode: "none",
      promptMessage: "이번 답변이 도움이 되었나요?",
      scale: "binary",
      scaleLabels: {
        one: "네",
        two: "아니오",
        three: "3점",
        four: "4점",
        five: "5점",
      },
    },
  },
  floatingButtons: [],
  recommendedIntents: [],
  blocklists: [],
  rules: [],
  smalltalk: {
    enabled: true,
    items: [],
    fallbackResponse: "등록된 스몰토크가 없어 기본 대화 흐름으로 이동합니다.",
  },
  botstation: {
    connected: false,
    enabled: false,
    connectedAt: "",
    channels: [],
  },
};

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function readStringValue(source: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return "";
}

function normalizeBlocklistItem(item: unknown): BlocklistConfig | null {
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    return null;
  }

  const source = item as Record<string, unknown>;
  return {
    id: typeof source.id === "string" && source.id.trim() ? source.id : crypto.randomUUID(),
    name: readStringValue(source, ["name"]),
    type: source.type === "regex" ? "regex" : "word",
    pattern: readStringValue(source, ["pattern"]),
    description: readStringValue(source, ["description"]),
    enabled: source.enabled !== false,
    updatedBy: readStringValue(source, ["updatedBy", "updated_by", "modifiedBy", "modified_by"]),
    updatedAt: readStringValue(source, ["updatedAt", "updated_at", "modifiedAt", "modified_at"]),
  };
}

function normalizeBlocklists(items: unknown): BlocklistConfig[] {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .map((item) => normalizeBlocklistItem(item))
    .filter((item): item is BlocklistConfig => item !== null);
}

function normalizeRuleItem(item: unknown): RuleConfig | null {
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    return null;
  }

  const source = item as Record<string, unknown>;
  return {
    id: typeof source.id === "string" && source.id.trim() ? source.id : crypto.randomUUID(),
    name: readStringValue(source, ["name"]),
    description: readStringValue(source, ["description"]),
    expression: readStringValue(source, ["expression"]),
    target: readStringValue(source, ["target"]),
    enabled: source.enabled !== false,
    updatedBy: readStringValue(source, ["updatedBy", "updated_by", "modifiedBy", "modified_by"]),
    updatedAt: readStringValue(source, ["updatedAt", "updated_at", "modifiedAt", "modified_at"]),
  };
}

function normalizeRules(items: unknown): RuleConfig[] {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .map((item) => normalizeRuleItem(item))
    .filter((item): item is RuleConfig => item !== null);
}

function normalizeSmalltalkItem(item: unknown): SmalltalkItemConfig | null {
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    return null;
  }

  const source = item as Record<string, unknown>;
  const userMessages = Array.isArray(source.userMessages)
    ? source.userMessages
    : Array.isArray(source.user_messages)
      ? source.user_messages
      : [];
  const botMessages = Array.isArray(source.botMessages)
    ? source.botMessages
    : Array.isArray(source.bot_messages)
      ? source.bot_messages
      : [];

  return {
    id: typeof source.id === "string" && source.id.trim() ? source.id : crypto.randomUUID(),
    title: readStringValue(source, ["title"]),
    priority: source.priority === "High" || source.priority === "Low" ? source.priority : "Medium",
    utterance: readStringValue(source, ["utterance"]),
    response: readStringValue(source, ["response"]),
    userMessages: userMessages.filter((item): item is string => typeof item === "string"),
    botMessages: botMessages.filter((item): item is string => typeof item === "string"),
    enabled: source.enabled !== false,
    createdBy: readStringValue(source, ["createdBy", "created_by"]),
    createdAt: readStringValue(source, ["createdAt", "created_at"]),
    updatedBy: readStringValue(source, ["updatedBy", "updated_by", "modifiedBy", "modified_by"]),
    updatedAt: readStringValue(source, ["updatedAt", "updated_at", "modifiedAt", "modified_at"]),
  };
}

function normalizeSmalltalkSettingsValue(value: unknown): SmalltalkSettingsConfig {
  const source =
    value && typeof value === "object" && !Array.isArray(value)
      ? value as Record<string, unknown>
      : {};
  const items = Array.isArray(source.items) ? source.items : [];
  return {
    enabled: source.enabled !== false,
    items: items
      .map((item) => normalizeSmalltalkItem(item))
      .filter((item): item is SmalltalkItemConfig => item !== null),
    fallbackResponse: readStringValue(source, ["fallbackResponse", "fallback_response"]),
  };
}

function mergeObject<T extends Record<string, unknown>>(base: T, patch?: Partial<T> | null): T {
  if (!patch) {
    return clone(base);
  }

  const result = clone(base);
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) {
      continue;
    }

    const baseValue = result[key];
    if (
      baseValue &&
      value &&
      typeof baseValue === "object" &&
      !Array.isArray(baseValue) &&
      typeof value === "object" &&
      !Array.isArray(value)
    ) {
      result[key as keyof T] = mergeObject(
        baseValue as Record<string, unknown>,
        value as Record<string, unknown>,
      ) as T[keyof T];
      continue;
    }

    result[key as keyof T] = value as T[keyof T];
  }
  return result;
}

export function getBotSettingsVersionName(
  bot: { active_version?: { name: string } | null },
  versionName?: string | null,
) {
  return versionName?.trim() || bot.active_version?.name || "v1";
}

export function getBotVersionSettings(bot: {
  active_version?: { id?: string; name: string } | null;
  data_json?: StudioBotDataJson;
}, versionScope?: string | null) {
  const selectedVersionScope = versionScope?.trim() || bot.active_version?.id || bot.active_version?.name || "v1";
  const currentSettings =
    bot.data_json?.settings_by_version?.[selectedVersionScope] ??
    (bot.active_version?.id === selectedVersionScope || !versionScope
      ? bot.data_json?.settings_by_version?.[bot.active_version?.name ?? ""]
      : undefined);
  const merged = mergeObject(DEFAULT_VERSION_SETTINGS, currentSettings);
  return {
    ...merged,
    blocklists: normalizeBlocklists(merged.blocklists),
    rules: normalizeRules(merged.rules),
    smalltalk: normalizeSmalltalkSettingsValue(merged.smalltalk),
  };
}


export function normalizeSecondaryLlmConfig(value: unknown): SecondaryLlmSettings {
  const config = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const normalized: SecondaryLlmSettings = { module_id: typeof config.module_id === "string" ? config.module_id.trim() : "" };
  if (config.outcome && typeof config.outcome === "object" && !Array.isArray(config.outcome)) {
    const source = config.outcome as Record<string, unknown>;
    normalized.outcome = {
      variableName: typeof source.variableName === "string" ? source.variableName.trim() : "",
      answeredValue: typeof source.answeredValue === "string" ? source.answeredValue : "",
      unrecognizedValue: typeof source.unrecognizedValue === "string" ? source.unrecognizedValue : "",
    };
  }
  return normalized;
}
