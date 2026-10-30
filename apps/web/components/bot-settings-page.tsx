"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { useI18n } from "@/components/language-provider";
import {
  ANSWER_MODE_OPTIONS,
  DEFAULT_ANSWER_MODE,
  normalizeAnswerMode,
  type AnswerMode,
} from "@/lib/answer-options";
import {
  isStudioBotVersionTrained,
  type StudioBotApiItem,
  updateStudioBot,
} from "@/lib/studio-bots-api";
import {
  normalizeConfigurationScoring,
  normalizeSecondaryLlmConfig,
  type ConfigurationScoringConfig,
  type StudioBotDataJson,
  type VectorConnectionConfig,
  type VectorConnectionsConfig,
} from "@/lib/bot-settings";
import { buildBotAiCombinationRows, selectedBotAiCombination } from "@/lib/bot-ai-combinations";
import { getLanguageLabel, normalizeSupportedLanguage, type SupportedLanguage } from "@/lib/language";
import { BotSettingsShell } from "@/components/bot-settings-shell";
import {
  getNluModelLabel,
  getNluModelDescription,
  getNluTypeLabel,
  isSemanticNluType,
  NLU_MODEL_OPTIONS_BY_TYPE,
  normalizeNluModel,
  normalizeNluType,
  type NluModelKey,
  type NluType,
} from "@/lib/nlu-options";
import {
  DEFAULT_LLM_MODEL,
  DEFAULT_LLM_PROVIDER,
  defaultLlmModelForProvider,
  LLM_MODEL_OPTIONS_BY_PROVIDER,
  LLM_PROVIDER_OPTIONS,
  normalizeLlmModel,
  normalizeLlmProvider,
  type LlmModelKey,
  type LlmProvider,
} from "@/lib/llm-options";
import { translateAiOptionText } from "@/lib/i18n/ai-options";
import { BOT_CREATE_CATALOGS, formatBotCreateText } from "@/lib/i18n/bot-create";
import { getBotDialogs } from "@/lib/dialog-assets";
import { BOT_SETTINGS_CATALOGS } from "@/lib/i18n/bot-settings";

const PROFILE_OPTIONS = [
  { key: "gray" as const, className: "bot-create-dialog__profile--muted" },
  { key: "accent" as const, className: "bot-create-dialog__profile--active" },
  { key: "outline" as const, className: "bot-create-dialog__profile--outline" },
];

const BOT_NAME_MAX_LENGTH = 40;
const BOT_NAME_PATTERN = /^[\p{L}\p{N}\s\-()_.]+$/u;
const INTERNAL_INTENT_INDEX_NAME = "aidot-intent";
const INTERNAL_ANSWER_INDEX_NAME = "aidot-answer";

function defaultVectorConnections(): VectorConnectionsConfig {
  return {
    intent: {
      enabled: false,
      endpoint_url: "",
      index_name: "",
      api_key: "",
    },
    answer: {
      enabled: false,
      endpoint_url: "",
      index_name: "",
      api_key: "",
    },
  };
}

function normalizeVectorConnection(value?: Partial<VectorConnectionConfig> | null): VectorConnectionConfig {
  return {
    enabled: value?.enabled === true,
    endpoint_url: value?.endpoint_url ?? "",
    index_name: value?.index_name ?? "",
    api_key: value?.api_key ?? "",
  };
}

function normalizeVectorConnections(value?: VectorConnectionsConfig | null): VectorConnectionsConfig {
  return {
    intent: normalizeVectorConnection(value?.intent),
    answer: normalizeVectorConnection(value?.answer),
  };
}

function internalVectorConnection(kind: "intent" | "answer", value?: Partial<VectorConnectionConfig> | null): VectorConnectionConfig {
  const current = normalizeVectorConnection(value);
  return {
    enabled: true,
    endpoint_url: "",
    index_name: current.index_name || (kind === "intent" ? INTERNAL_INTENT_INDEX_NAME : INTERNAL_ANSWER_INDEX_NAME),
    api_key: "",
  };
}

function mapBotTypeLabel(bot: {
  data_json?: { bot_kind?: "bot" | "hub"; bot_mode?: "text" | "voice" };
}, labels: { hub: string; voice: string; text: string }) {
  if (bot.data_json?.bot_kind === "hub") {
    return labels.hub;
  }

  if (bot.data_json?.bot_mode === "voice") {
    return labels.voice;
  }

  return labels.text;
}

function formatUpdatedAt(value?: string | null) {
  if (!value) {
    return "-";
  }

  return value.replace("T", " ").slice(0, 16).replace(/-/g, ". ");
}

function getSelectedAiConfig(bot: StudioBotApiItem): StudioBotDataJson {
  const botConfig = bot.data_json ?? {};
  const systemConfig = bot.active_version?.version_json?.system_config;
  const aiConfig =
    systemConfig && typeof systemConfig === "object" && "ai_config" in systemConfig
      ? systemConfig.ai_config
      : null;

  if (aiConfig && typeof aiConfig === "object" && !Array.isArray(aiConfig)) {
    return {
      ...botConfig,
      ...(aiConfig as Partial<StudioBotDataJson>),
    };
  }

  return botConfig;
}

type BotEditSectionProps = {
  bot: StudioBotApiItem;
  mainHref: string;
  token: string;
  versionName: string;
  setBot: (bot: StudioBotApiItem) => void;
  setMessage: (value: string) => void;
  setErrorMessage: (value: string) => void;
};

function BotEditSection({
  bot,
  mainHref,
  token,
  versionName,
  setBot,
  setMessage,
  setErrorMessage,
}: BotEditSectionProps) {
  const { language: uiLanguage } = useI18n();
  const createCopy = BOT_CREATE_CATALOGS[uiLanguage];
  const copy = BOT_SETTINGS_CATALOGS[uiLanguage];
  const [name, setName] = useState("");
  const [language, setLanguage] = useState<SupportedLanguage>("ko");
  const [nluType, setNluType] = useState<NluType>("ml");
  const [nluModel, setNluModel] = useState<NluModelKey>("deep_learning_lite");
  const [answerMode, setAnswerMode] = useState<AnswerMode>(DEFAULT_ANSWER_MODE);
  const [llmProvider, setLlmProvider] = useState<LlmProvider>(DEFAULT_LLM_PROVIDER);
  const [llmModel, setLlmModel] = useState<LlmModelKey>(DEFAULT_LLM_MODEL);
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [vectorConnections, setVectorConnections] = useState<VectorConnectionsConfig>(defaultVectorConnections());
  const [configurationScoring, setConfigurationScoring] = useState<ConfigurationScoringConfig>(normalizeConfigurationScoring());
  const [profileKey, setProfileKey] = useState<"gray" | "accent" | "outline">("accent");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [secondaryModuleId, setSecondaryModuleId] = useState("");
  const [outcomeVariable, setOutcomeVariable] = useState("");
  const [answeredValue, setAnsweredValue] = useState("");
  const [unrecognizedValue, setUnrecognizedValue] = useState("");

  useEffect(() => {
    const aiConfig = getSelectedAiConfig(bot);
    const nextType = normalizeNluType(aiConfig.nlu_type);
    setName(bot.name);
    setLanguage(normalizeSupportedLanguage(aiConfig.language));
    setNluType(nextType);
    const secondaryConfig = normalizeSecondaryLlmConfig(aiConfig.secondary_llm);
    setSecondaryModuleId(secondaryConfig.module_id);
    setOutcomeVariable(secondaryConfig.outcome?.variableName ?? "");
    setAnsweredValue(secondaryConfig.outcome?.answeredValue ?? "");
    setUnrecognizedValue(secondaryConfig.outcome?.unrecognizedValue ?? "");
    setNluModel(normalizeNluModel(nextType, aiConfig.nlu_model ?? aiConfig.nlu_engine));
    setAnswerMode(normalizeAnswerMode(aiConfig.answer_mode));
    const nextLlmProvider = normalizeLlmProvider(aiConfig.llm_provider);
    setLlmProvider(nextLlmProvider);
    setLlmModel(normalizeLlmModel(nextLlmProvider, aiConfig.llm_model));
    setLlmBaseUrl(typeof aiConfig.llm_base_url === "string" ? aiConfig.llm_base_url : "");
    setVectorConnections(normalizeVectorConnections(aiConfig.vector_connections));
    setConfigurationScoring(normalizeConfigurationScoring(aiConfig.configuration_scoring));
    setProfileKey((aiConfig.profile_key as "gray" | "accent" | "outline") ?? "accent");
    setDescription(aiConfig.introduction ?? bot.description ?? "");
  }, [bot]);

  const typeLabel = mapBotTypeLabel(bot, {
    hub: createCopy.botHub,
    voice: createCopy.voiceType,
    text: createCopy.textType,
  });
  const llmModelOptions = LLM_MODEL_OPTIONS_BY_PROVIDER[llmProvider];
  const secondaryModules = getBotDialogs(bot).filter((dialog) => dialog.dialogType === 0);
  const secondaryModuleMissing = Boolean(secondaryModuleId) && !secondaryModules.some((dialog) => dialog.id === secondaryModuleId);
  const nluModelOptions = NLU_MODEL_OPTIONS_BY_TYPE[nluType] ?? [];
  const nluModelDescription = getNluModelDescription(nluType, nluModel);
  const aiCombinationRows = buildBotAiCombinationRows();
  const selectedCombination = selectedBotAiCombination(nluType, nluModel, answerMode);
  const usesLlmEngine = nluType === "llm" || answerMode === "llm_rag" || answerMode === "llm";
  const usesSemanticNlu = isSemanticNluType(nluType);
  const usesSemanticAnswer = answerMode === "semantic_rag";
  const answerModeLabel = translateAiOptionText(
    uiLanguage,
    ANSWER_MODE_OPTIONS.find((option) => option.value === answerMode)?.label ?? "정해진 답변",
  );
  const selectedVersionTrained = isStudioBotVersionTrained(bot.active_version);
  const modelLockHint = selectedVersionTrained ? copy.modelLocked : copy.modelChangeable;
  const fixedModeMessage = copy.fixedModeMessage;

  async function handleSave() {
    if (!token) {
      setErrorMessage(createCopy.loginRequired);
      return;
    }

    if (!name.trim()) {
      setErrorMessage(createCopy.nameRequired);
      return;
    }

    if (!BOT_NAME_PATTERN.test(name.trim())) {
      setErrorMessage(translateAiOptionText(uiLanguage, "Bot names may contain Unicode letters, numbers, spaces, and supported symbols."));
      return;
    }

    setSaving(true);
    setErrorMessage("");
    setMessage("");

    try {
      const normalizedVectorConnections: VectorConnectionsConfig = {
        intent: nluType === "semantic_vector"
          ? internalVectorConnection("intent", vectorConnections.intent)
          : normalizeVectorConnection(vectorConnections.intent),
        answer: answerMode === "semantic_rag"
          ? internalVectorConnection("answer", vectorConnections.answer)
          : normalizeVectorConnection(vectorConnections.answer),
      };
      const payload = {
        settings_scope: bot.active_version?.id ?? versionName,
        secondary_llm: normalizeSecondaryLlmConfig({ module_id: secondaryModuleId, outcome: { variableName: outcomeVariable, answeredValue, unrecognizedValue } }),
        name: name.trim(),
        description: description.trim() || undefined,
        profile_key: profileKey,
        nlu_engine: nluModel,
        nlu_model: nluModel,
        llm_provider: llmProvider,
        llm_model: llmModel,
        llm_base_url: llmBaseUrl.trim() || undefined,
        vector_connections: normalizedVectorConnections,
        configuration_scoring: configurationScoring,
        introduction: description.trim() || undefined,
      };
      const updated = await updateStudioBot(token, bot.id, payload, {
        responseMode: "settings",
        versionScope: versionName,
      });

      setBot(updated);
      setMessage(copy.saved);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : copy.saveError);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
          <div className="bot-settings-page__top-actions">
            <Link href={mainHref} className="secondary-action">
              {copy.cancel}
            </Link>
            <button type="button" className="primary-action" disabled={saving} onClick={handleSave}>
              {saving ? copy.saving : copy.save}
            </button>
          </div>

          <section className="bot-settings-section">
            <h2>{copy.basicInfo}</h2>
            <div className="bot-settings-grid bot-settings-grid--4">
              <label className="bot-settings-card">
                <span>{createCopy.botName} <em>*</em></span>
                <input
                  type="text"
                  className="bot-settings-card__input"
                  maxLength={BOT_NAME_MAX_LENGTH}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
                <small className="bot-settings-card__hint">
                  {translateAiOptionText(uiLanguage, "Bot names may contain Unicode letters, numbers, spaces, and supported symbols.")}
                  <span>{name.length}/{BOT_NAME_MAX_LENGTH}</span>
                </small>
              </label>

              <div className="bot-settings-card">
                <span>{copy.type}</span>
                <strong>{typeLabel}</strong>
              </div>

              <div className="bot-settings-card">
                <span>{copy.botId}</span>
                <strong>{bot.id}</strong>
              </div>

              <div className="bot-settings-card">
                <span>{copy.recentModified}</span>
                <strong>{formatUpdatedAt(bot.updated_at)}</strong>
              </div>
            </div>
          </section>

          <section className="bot-settings-section">
            <h2>{copy.creationInfo}</h2>
            <div className="bot-settings-grid bot-settings-grid--5">
              <div className="bot-settings-card">
                <span>{createCopy.language} <em>*</em></span>
                <strong>{getLanguageLabel(language)}</strong>
                <small className="bot-settings-card__hint">{copy.fixedAtCreation}</small>
              </div>

              <div className="bot-settings-card">
                <span>{createCopy.nluType}</span>
                <strong>{translateAiOptionText(uiLanguage, getNluTypeLabel(nluType))}</strong>
                <small className="bot-settings-card__hint">{copy.fixedAtCreation}</small>
              </div>

              {nluType === "semantic_external" ? (
                <label className="bot-settings-card">
                  <span>{createCopy.nluModel} <em>*</em></span>
                  <select
                    className="bot-settings-card__select"
                    value={nluModel}
                    disabled={selectedVersionTrained}
                    onChange={(event) => setNluModel(event.target.value as NluModelKey)}
                  >
                    {nluModelOptions.map((option) => (
                      <option key={option.value} value={option.value} disabled={option.disabled}>
                        {translateAiOptionText(uiLanguage, option.label)} ({translateAiOptionText(uiLanguage, option.note)})
                      </option>
                    ))}
                  </select>
                  <small className="bot-settings-card__hint">{selectedVersionTrained ? modelLockHint : (translateAiOptionText(uiLanguage, nluModelDescription) || modelLockHint)}</small>
                </label>
              ) : (
                <div className="bot-settings-card">
                  <span>{createCopy.nluModel} <em>*</em></span>
                  <strong>{translateAiOptionText(uiLanguage, getNluModelLabel(nluType, nluModel))}</strong>
                  <small className="bot-settings-card__hint">{modelLockHint}</small>
                </div>
              )}

              <div className="bot-settings-card">
                <span>{createCopy.answerMode}</span>
                <strong>{answerModeLabel}</strong>
                <small className="bot-settings-card__hint">{copy.fixedAtCreation}</small>
              </div>

              <div className="bot-settings-card">
                <span>{copy.profile}</span>
                <div className="bot-settings-card__profiles">
                  {PROFILE_OPTIONS.map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      className={`${option.className}${profileKey === option.key ? " is-selected" : ""} bot-create-dialog__profile`}
                      onClick={() => setProfileKey(option.key)}
                      aria-label={`${copy.profile} ${option.key}`}
                    />
                  ))}
                </div>
              </div>

              <div className="bot-settings-card">
                <span>{copy.versionCount}</span>
                <strong>{bot.version_count}</strong>
              </div>

            </div>

            {bot.data_json?.bot_kind !== "hub" ? (
              <div className="bot-settings-grid">
                <label className="bot-settings-card">
                  <span>{copy.secondaryLlmModule}</span>
                  <select className="bot-settings-card__select" value={secondaryModuleId} onChange={(event) => setSecondaryModuleId(event.target.value)}>
                    <option value="">{copy.secondaryLlmNone}</option>
                    {secondaryModuleMissing ? <option value={secondaryModuleId}>{copy.secondaryLlmMissing}</option> : null}
                    {secondaryModules.map((module) => <option key={module.id} value={module.id}>{module.displayName || module.name}</option>)}
                  </select>
                  <small className="bot-settings-card__hint">{copy.secondaryLlmHint}</small>
                </label>
                <label className="bot-settings-card">
                  <span>{copy.secondaryOutcomeVariable}</span>
                  <input className="bot-settings-card__input" value={outcomeVariable} onChange={(event) => setOutcomeVariable(event.target.value)} />
                  <small className="bot-settings-card__hint">{copy.secondaryOutcomeHint}</small>
                </label>
                <label className="bot-settings-card">
                  <span>{copy.secondaryAnsweredValue}</span>
                  <input className="bot-settings-card__input" value={answeredValue} onChange={(event) => setAnsweredValue(event.target.value)} />
                </label>
                <label className="bot-settings-card">
                  <span>{copy.secondaryUnrecognizedValue}</span>
                  <input className="bot-settings-card__input" value={unrecognizedValue} onChange={(event) => setUnrecognizedValue(event.target.value)} />
                </label>
              </div>
            ) : null}

            {usesLlmEngine ? (
              <div className="bot-settings-grid bot-settings-grid--4 bot-settings-grid--llm">
                <label className="bot-settings-card">
                  <span>LLM Provider <em>*</em></span>
                  <select
                    className="bot-settings-card__select"
                    value={llmProvider}
                    disabled={selectedVersionTrained}
                    onChange={(event) => {
                      const nextProvider = event.target.value as LlmProvider;
                      setLlmProvider(nextProvider);
                      setLlmModel(defaultLlmModelForProvider(nextProvider));
                    }}
                  >
                    {LLM_PROVIDER_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {translateAiOptionText(uiLanguage, option.label)} ({translateAiOptionText(uiLanguage, option.note)})
                      </option>
                    ))}
                  </select>
                </label>

                <label className="bot-settings-card">
                  <span>{createCopy.llmModel} <em>*</em></span>
                  <select
                    className="bot-settings-card__select"
                    value={llmModel}
                    disabled={selectedVersionTrained}
                    onChange={(event) => setLlmModel(event.target.value as LlmModelKey)}
                  >
                    {llmModelOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {translateAiOptionText(uiLanguage, option.label)} ({translateAiOptionText(uiLanguage, option.note)})
                      </option>
                    ))}
                  </select>
                  <small className="bot-settings-card__hint">{modelLockHint}</small>
                </label>

                {llmProvider === "ollama" ? (
                  <label className="bot-settings-card">
                    <span>{createCopy.ollamaAddress}</span>
                    <input
                      type="url"
                      className="bot-settings-card__input"
                      placeholder="http://192.168.220.180:11434"
                      value={llmBaseUrl}
                      onChange={(event) => setLlmBaseUrl(event.target.value)}
                    />
                  </label>
                ) : null}
              </div>
            ) : null}

            {usesSemanticNlu ? (
              <div className="bot-settings-vector">
                <div className="bot-settings-vector__header">
                  <strong>{createCopy.intentVectorConnection}</strong>
                  <span>
                    {nluType === "semantic_external"
                      ? createCopy.externalIntentVectorDescription
                      : createCopy.internalIntentVectorDescription}
                  </span>
                </div>
                <div className="bot-settings-grid bot-settings-grid--4">
                  <label className="bot-settings-card">
                    <span>{copy.useStatus}</span>
                    <select
                      className="bot-settings-card__select"
                      value={vectorConnections.intent?.enabled ? "true" : "false"}
                      onChange={(event) =>
                        setVectorConnections((current) => ({
                          ...current,
                          intent: {
                            ...normalizeVectorConnection(current.intent),
                            enabled: event.target.value === "true",
                          },
                        }))
                      }
                    >
                      <option value="false">{createCopy.disabled}</option>
                      <option value="true">{createCopy.enabled}</option>
                    </select>
                  </label>

                  {nluType === "semantic_external" ? (
                    <label className="bot-settings-card bot-settings-card--wide">
                      <span>{copy.searchApiUrl}</span>
                      <input
                        type="url"
                        className="bot-settings-card__input"
                        placeholder="https://vector.example.com/intent/search"
                        value={vectorConnections.intent?.endpoint_url ?? ""}
                        onChange={(event) =>
                          setVectorConnections((current) => ({
                            ...current,
                            intent: {
                              ...normalizeVectorConnection(current.intent),
                              endpoint_url: event.target.value,
                            },
                          }))
                        }
                      />
                    </label>
                  ) : null}

                  <label className="bot-settings-card">
                    <span>{copy.indexName}</span>
                    <input
                      type="text"
                      className="bot-settings-card__input"
                      placeholder="aidot-intent"
                      value={vectorConnections.intent?.index_name || (nluType === "semantic_vector" ? INTERNAL_INTENT_INDEX_NAME : "")}
                      onChange={(event) =>
                        setVectorConnections((current) => ({
                          ...current,
                          intent: {
                            ...normalizeVectorConnection(current.intent),
                            index_name: event.target.value,
                          },
                        }))
                      }
                    />
                  </label>

                  {nluType === "semantic_external" ? (
                    <label className="bot-settings-card">
                      <span>API Key</span>
                      <input
                        type="password"
                        className="bot-settings-card__input"
                        placeholder={copy.optionalInput}
                        value={vectorConnections.intent?.api_key ?? ""}
                        onChange={(event) =>
                          setVectorConnections((current) => ({
                            ...current,
                            intent: {
                              ...normalizeVectorConnection(current.intent),
                              api_key: event.target.value,
                            },
                          }))
                        }
                      />
                    </label>
                  ) : null}
                </div>
              </div>
            ) : null}

            {usesSemanticAnswer ? (
              <div className="bot-settings-vector">
                <div className="bot-settings-vector__header">
                  <strong>{createCopy.answerVectorConnection}</strong>
                  <span>{createCopy.answerVectorDescription}</span>
                </div>
                <div className="bot-settings-grid bot-settings-grid--4">
                  <label className="bot-settings-card">
                    <span>{copy.useStatus}</span>
                    <select
                      className="bot-settings-card__select"
                      value={vectorConnections.answer?.enabled ? "true" : "false"}
                      onChange={(event) =>
                        setVectorConnections((current) => ({
                          ...current,
                          answer: {
                            ...normalizeVectorConnection(current.answer),
                            enabled: event.target.value === "true",
                          },
                        }))
                      }
                    >
                      <option value="false">{createCopy.disabled}</option>
                      <option value="true">{createCopy.enabled}</option>
                    </select>
                  </label>

                  <label className="bot-settings-card">
                    <span>{copy.indexName}</span>
                    <input
                      type="text"
                      className="bot-settings-card__input"
                      placeholder="aidot-answer"
                      value={vectorConnections.answer?.index_name || (answerMode === "semantic_rag" ? INTERNAL_ANSWER_INDEX_NAME : "")}
                      onChange={(event) =>
                        setVectorConnections((current) => ({
                          ...current,
                          answer: {
                            ...normalizeVectorConnection(current.answer),
                            index_name: event.target.value,
                          },
                        }))
                      }
                    />
                  </label>

                </div>
              </div>
            ) : null}

            {nluType !== "llm" ? (
              <div className="bot-settings-vector">
                <div className="bot-settings-vector__header">
                  <strong>{copy.scoringTitle}</strong>
                  <span>{copy.scoringDescription}</span>
                </div>
                <div className="bot-settings-grid bot-settings-grid--6">
                  {[
                    ["dictionaryWeight", copy.dictionaryRepresentative],
                    ["entityWeight", copy.entity],
                    ["wordWeight", copy.nounVerb],
                    ["gramWeight", copy.characterFragment],
                    ["particleEndingWeight", copy.particleEnding],
                    ["keyMatchScore", copy.keyMatchMinimum],
                  ].map(([key, label]) => (
                    <label key={key} className="bot-settings-card">
                      <span>{label}</span>
                      <input
                        type="number"
                        step="0.01"
                        min={0}
                        max={key === "keyMatchScore" ? 1 : 20}
                        className="bot-settings-card__input"
                        value={configurationScoring[key as keyof ConfigurationScoringConfig]}
                        onChange={(event) =>
                          setConfigurationScoring((current) => ({
                            ...current,
                            [key]: Number(event.target.value),
                          }))
                        }
                      />
                    </label>
                  ))}
                </div>
              </div>
            ) : null}

            <label className="bot-settings-intro">
              <span>{copy.introduction}</span>
              <textarea
                className="bot-settings-intro__textarea"
                placeholder={createCopy.introductionPlaceholder}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>

            <section className="bot-ai-combinations" aria-label={createCopy.combinationAria}>
              <div className="bot-ai-combinations__summary">
                <strong>{createCopy.selectedCombination}</strong>
                <span>
                  {selectedCombination
                    ? formatBotCreateText(createCopy.selectedCombinationSummary, {
                      nlu: translateAiOptionText(uiLanguage, selectedCombination.nluLabel),
                      model: translateAiOptionText(uiLanguage, selectedCombination.nluModelLabel),
                      answer: translateAiOptionText(uiLanguage, selectedCombination.answerLabel),
                    })
                    : createCopy.noCombination}
                </span>
                {selectedCombination ? (
                  <em className={`bot-ai-combinations__badge bot-ai-combinations__badge--${selectedCombination.status}`}>
                    {translateAiOptionText(uiLanguage, selectedCombination.statusLabel)}
                  </em>
                ) : null}
              </div>
              <p className="bot-ai-combinations__lock">{fixedModeMessage}</p>
              <div className="bot-ai-combinations__matrix">
                <div className="bot-ai-combinations__axis bot-ai-combinations__axis--corner">
                  <span>{createCopy.intentEngine}</span>
                  <strong>{createCopy.answerEngine}</strong>
                </div>
                {ANSWER_MODE_OPTIONS.map((option) => (
                  <div key={option.value} className="bot-ai-combinations__axis bot-ai-combinations__axis--answer">
                    <span>{createCopy.answerEngine}</span>
                    <strong>{translateAiOptionText(uiLanguage, option.label)}</strong>
                  </div>
                ))}
                {aiCombinationRows.map((row) => (
                  <div key={row.nluType} className="bot-ai-combinations__row">
                    <div className="bot-ai-combinations__axis bot-ai-combinations__axis--nlu">
                      <span>{createCopy.intentEngine}</span>
                      <strong>{translateAiOptionText(uiLanguage, row.nluLabel)}</strong>
                      <small>{translateAiOptionText(uiLanguage, row.nluModelLabel)}</small>
                    </div>
                    {row.combinations.map((item) => {
                      const selected = item.nluType === nluType && item.answerMode === answerMode;
                      return (
                        <button
                          type="button"
                          key={item.id}
                          className={`bot-ai-combinations__item bot-ai-combinations__item--${item.status}${selected ? " is-selected" : ""}`}
                          disabled={!selected}
                          aria-pressed={selected}
                        >
                          <span>{createCopy.combinationStatus}</span>
                          <strong>{translateAiOptionText(uiLanguage, item.statusLabel)}</strong>
                          <small>{item.status === "unsupported" ? createCopy.unavailable : selected ? createCopy.currentSelection : copy.lockedAfterTraining}</small>
                        </button>
                      );
                    })}
                  </div>
                ))}
              </div>
              <p>{selectedCombination ? translateAiOptionText(uiLanguage, selectedCombination.note) : createCopy.combinationHelp}</p>
            </section>
          </section>

    </>
  );
}

export function BotSettingsPage() {
  return (
    <BotSettingsShell activeMenu="edit">
      {(args) => <BotEditSection {...args} />}
    </BotSettingsShell>
  );
}
