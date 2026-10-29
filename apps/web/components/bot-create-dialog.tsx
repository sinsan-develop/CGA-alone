"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type ChangeEvent } from "react";

import { useI18n } from "@/components/language-provider";
import {
  ANSWER_MODE_OPTIONS,
  DEFAULT_ANSWER_MODE,
  type AnswerMode,
} from "@/lib/answer-options";
import { loadAuthSession } from "@/lib/auth";
import { buildBotAiCombinationRows, selectedBotAiCombination } from "@/lib/bot-ai-combinations";
import {
  DEFAULT_LLM_MODEL,
  DEFAULT_LLM_PROVIDER,
  defaultLlmModelForProvider,
  LLM_MODEL_OPTIONS_BY_PROVIDER,
  LLM_PROVIDER_OPTIONS,
  type LlmModelKey,
  type LlmProvider,
} from "@/lib/llm-options";
import {
  defaultNluModelForType,
  getNluModelDescription,
  isSemanticNluType,
  NLU_MODEL_OPTIONS_BY_TYPE,
  NLU_TYPE_OPTIONS,
  type NluModelKey,
  type NluType,
} from "@/lib/nlu-options";
import { createStudioBot, fetchStudioBots } from "@/lib/studio-bots-api";
import { type VectorConnectionConfig, type VectorConnectionsConfig } from "@/lib/bot-settings";
import { normalizeSupportedLanguage, SUPPORTED_LANGUAGES, type SupportedLanguage } from "@/lib/language";
import { translateAiOptionText } from "@/lib/i18n/ai-options";
import { BOT_CREATE_CATALOGS, formatBotCreateText } from "@/lib/i18n/bot-create";

const PROFILE_OPTIONS = [
  { key: "gray" as const, className: "bot-create-dialog__profile--muted" },
  { key: "accent" as const, className: "bot-create-dialog__profile--active" },
  { key: "outline" as const, className: "bot-create-dialog__profile--outline" },
];

const BOT_NAME_MAX_LENGTH = 40;
const PROFILE_IMAGE_MAX_BYTES = 2 * 1024 * 1024;
const PROFILE_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
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

function internalVectorConnection(kind: "intent" | "answer", enabled = true): VectorConnectionConfig {
  return {
    enabled,
    endpoint_url: "",
    index_name: kind === "intent" ? INTERNAL_INTENT_INDEX_NAME : INTERNAL_ANSWER_INDEX_NAME,
    api_key: "",
  };
}

export function BotCreateDialog() {
  const router = useRouter();
  const { language: uiLanguage } = useI18n();
  const copy = BOT_CREATE_CATALOGS[uiLanguage];
  const [botKind, setBotKind] = useState<"bot" | "hub">("bot");
  const [botMode, setBotMode] = useState<"text" | "voice">("text");
  const [hubCallMethod, setHubCallMethod] = useState<"button" | "natural">("button");
  const [profileKey, setProfileKey] = useState<"gray" | "accent" | "outline">("accent");
  const [profileImageData, setProfileImageData] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [language, setLanguage] = useState<SupportedLanguage>("ko");
  const [nluType, setNluType] = useState<NluType>("ml");
  const [nluModel, setNluModel] = useState<NluModelKey>("deep_learning_lite");
  const [answerMode, setAnswerMode] = useState<AnswerMode>(DEFAULT_ANSWER_MODE);
  const [llmProvider, setLlmProvider] = useState<LlmProvider>(DEFAULT_LLM_PROVIDER);
  const [llmModel, setLlmModel] = useState<LlmModelKey>(DEFAULT_LLM_MODEL);
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [vectorConnections, setVectorConnections] = useState<VectorConnectionsConfig>(defaultVectorConnections());
  const [introduction, setIntroduction] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [creationAllowed, setCreationAllowed] = useState(false);

  useEffect(() => {
    let disposed = false;
    const session = loadAuthSession();
    if (!session) {
      setErrorMessage(copy.loginRequired);
      return;
    }
    fetchStudioBots(session.access_token, true)
      .then((bots) => {
        if (disposed) return;
        if (bots.length > 0) {
          const bot = bots[0];
          router.replace(bot.data_json?.bot_kind === "hub" ? `/studio/hubs/${bot.id}` : `/studio/bots/${bot.id}`);
          return;
        }
        setCreationAllowed(true);
      })
      .catch((error) => {
        if (!disposed) setErrorMessage(error instanceof Error ? error.message : copy.createError);
      });
    return () => { disposed = true; };
  }, [router, copy.loginRequired, copy.createError]);

  const nluModelOptions = NLU_MODEL_OPTIONS_BY_TYPE[nluType] ?? [];
  const nluModelDescription = getNluModelDescription(nluType, nluModel);
  const llmModelOptions = LLM_MODEL_OPTIONS_BY_PROVIDER[llmProvider];
  const aiCombinationRows = buildBotAiCombinationRows();
  const selectedCombination = selectedBotAiCombination(nluType, nluModel, answerMode);
  const usesLlmEngine = nluType === "llm" || answerMode === "llm_rag" || answerMode === "llm";
  const usesSemanticNlu = isSemanticNluType(nluType);
  const usesSemanticAnswer = answerMode === "semantic_rag";

  function selectCombination(nextType: NluType, nextAnswerMode: AnswerMode) {
    setNluType(nextType);
    setNluModel(defaultNluModelForType(nextType));
    setAnswerMode(nextAnswerMode);
    setVectorConnections((current) => ({
      intent: nextType === "semantic_vector"
        ? internalVectorConnection("intent")
        : normalizeVectorConnection(current.intent),
      answer: nextAnswerMode === "semantic_rag"
        ? internalVectorConnection("answer")
        : normalizeVectorConnection(current.answer),
    }));
  }

  function handleProfileImageChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;

    if (!PROFILE_IMAGE_TYPES.has(file.type)) {
      setErrorMessage(copy.invalidProfileType);
      return;
    }
    if (file.size > PROFILE_IMAGE_MAX_BYTES) {
      setErrorMessage(copy.profileTooLarge);
      return;
    }

    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") {
        setProfileImageData(reader.result);
        setErrorMessage("");
      }
    };
    reader.onerror = () => setErrorMessage(copy.profileReadError);
    reader.readAsDataURL(file);
  }

  async function handleSubmit() {
    if (!creationAllowed) return;
    const session = loadAuthSession();
    if (!session) {
      setErrorMessage(copy.loginRequired);
      return;
    }

    if (!name.trim()) {
      setErrorMessage(copy.nameRequired);
      return;
    }

    if (!BOT_NAME_PATTERN.test(name.trim())) {
      setErrorMessage(translateAiOptionText(uiLanguage, "Bot names may contain Unicode letters, numbers, spaces, and supported symbols."));
      return;
    }

    setIsSubmitting(true);
    setErrorMessage("");

    try {
      const sharedPayload = {
        name: name.trim(),
        description: introduction.trim() || undefined,
        bot_kind: botKind,
        bot_mode: botKind === "hub" ? "text" : botMode,
        profile_key: profileKey,
        profile_image_data: profileImageData || undefined,
        language,
        introduction: introduction.trim() || undefined,
        hub_call_method: botKind === "hub" ? hubCallMethod : undefined,
      };
      const createdBot = await createStudioBot(
        session.access_token,
        botKind === "hub"
          ? sharedPayload
          : {
              ...sharedPayload,
              nlu_engine: nluModel,
              nlu_type: nluType,
              nlu_model: nluModel,
              answer_mode: answerMode,
              llm_provider: llmProvider,
              llm_model: llmModel,
              llm_base_url: llmBaseUrl.trim() || undefined,
              vector_connections: vectorConnections,
            },
      );

      if (botKind === "hub") {
        router.push(`/studio/hubs/${createdBot.id}`);
        return;
      }

      router.push(`/studio/bots/${createdBot.id}/versions/${createdBot.active_version?.name ?? "v1"}/intents`);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : copy.createError);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="bot-create-dialog cga-bot-create-page">
      <div className="bot-create-dialog__header">
        <div className="cga-bot-create-page__heading">
          <span>01</span>
          <strong>{copy.createBot}</strong>
          <button type="button" title={copy.createInfoTitle} aria-label={copy.createInfoAria}>i</button>
        </div>
        <Link href="/studio/bots" className="cga-bot-create-page__version-link">{copy.versionManagement}</Link>
        <span className="cga-bot-create-page__current">+ {copy.createBot}</span>
      </div>

      <div className="bot-create-dialog__body">
        <div className="cga-bot-create-page__section-title cga-bot-create-page__section-title--basic">{copy.basicInfo} <span>i</span></div>
        <div className="cga-bot-create-page__section-title cga-bot-create-page__section-title--ai">{copy.aiItems} <span>i</span></div>
        <aside className="cga-bot-create-page__summary">
          <header><strong>{copy.structureSummary}</strong><span>{copy.selectedState}</span></header>
          <dl>
            <div><dt>{copy.language}</dt><dd>{language}</dd></div>
            <div><dt>{copy.nluType}</dt><dd>{nluType}</dd></div>
            <div><dt>{copy.nluModel}</dt><dd>{nluModel}</dd></div>
            <div><dt>{copy.answerMode}</dt><dd>{answerMode}</dd></div>
            <div><dt>LLM</dt><dd>{usesLlmEngine ? llmModel : "-"}</dd></div>
            <div><dt>{copy.version}</dt><dd>v1</dd></div>
          </dl>
          <p>{translateAiOptionText(uiLanguage, "Next: configure using the selected input structure.")}</p>
        </aside>
        <div className="bot-create-dialog__field cga-bot-create-page__field--type">
          <span className="bot-create-dialog__label">{copy.botType} <em>*</em></span>
          <div className="bot-create-dialog__type-row">
            <label className="bot-create-dialog__radio">
              <input
                type="radio"
                name="bot-kind"
                checked={botKind === "bot"}
                onChange={() => setBotKind("bot")}
              />
              <span>{copy.bot}</span>
            </label>
            <div className="bot-create-dialog__switch">
              <button
                type="button"
                className={botMode === "text" && botKind === "bot" ? "is-active" : ""}
                onClick={() => setBotMode("text")}
                disabled={botKind === "hub"}
              >
                {copy.textType}
              </button>
              <button
                type="button"
                className={botMode === "voice" && botKind === "bot" ? "is-active" : ""}
                onClick={() => setBotMode("voice")}
                disabled={botKind === "hub"}
              >
                {copy.voiceType}
              </button>
            </div>

          </div>
        </div>

        <div className="bot-create-dialog__field cga-bot-create-page__field--profile">
          <span className="bot-create-dialog__label">{copy.botProfile} <em>*</em></span>
          <div className="bot-create-dialog__profile-row">
            {profileImageData ? (
              <img
                className="bot-create-dialog__profile bot-create-dialog__profile-image is-selected"
                src={profileImageData}
                alt={copy.selectedProfileAlt}
              />
            ) : (
              PROFILE_OPTIONS.map((option) => (
                <button
                  key={option.key}
                  type="button"
                  aria-label={`${copy.defaultProfile} ${option.key}`}
                  className={option.className + (profileKey === option.key ? " is-selected" : "") + " bot-create-dialog__profile"}
                  onClick={() => setProfileKey(option.key)}
                />
              ))
            )}
            <label className="bot-create-dialog__profile-upload">
              {copy.choosePcImage}
              <input
                type="file"
                accept="image/png,image/jpeg,image/webp"
                onChange={handleProfileImageChange}
              />
            </label>
            {profileImageData ? (
              <button
                type="button"
                className="bot-create-dialog__profile-reset"
                onClick={() => setProfileImageData(null)}
              >
                {copy.useDefaultProfile}
              </button>
            ) : null}
          </div>
          <small>{copy.imageRequirements}</small>
        </div>

        <div className="bot-create-dialog__field cga-bot-create-page__field--name">
          <span className="bot-create-dialog__label">{copy.botName} <em>*</em></span>
          <input
            type="text"
            className="bot-create-dialog__input"
            placeholder={copy.botNamePlaceholder}
            value={name}
            maxLength={BOT_NAME_MAX_LENGTH}
            onChange={(event) => setName(event.target.value)}
          />
          <small>
            {translateAiOptionText(uiLanguage, "Bot names may contain Unicode letters, numbers, spaces, and supported symbols.")}
            <span>{name.length}/{BOT_NAME_MAX_LENGTH}</span>
          </small>
        </div>

        <div className="bot-create-dialog__field cga-bot-create-page__field--language">
          <span className="bot-create-dialog__label">{copy.language} <em>*</em></span>
          <select
            className="bot-create-dialog__select"
            value={language}
            onChange={(event) => setLanguage(normalizeSupportedLanguage(event.target.value))}
          >
            {SUPPORTED_LANGUAGES.map((option) => (
              <option key={option.code} value={option.code}>{option.label}</option>
            ))}
          </select>
        </div>

        {botKind === "hub" ? (
          <div className="bot-create-dialog__field cga-bot-create-page__field--hub-call">
            <span className="bot-create-dialog__label">{copy.callMethod} <em>*</em></span>
            <select
              className="bot-create-dialog__select"
              value={hubCallMethod}
              onChange={(event) => setHubCallMethod(event.target.value as "button" | "natural")}
            >
              <option value="button">{copy.buttonSelection}</option>
              <option value="natural">{copy.naturalLanguageInput}</option>
            </select>
          </div>
        ) : null}

        {botKind === "bot" ? (
          <>
        <div className="bot-create-dialog__field cga-bot-create-page__field--nlu-type">
          <span className="bot-create-dialog__label">{copy.nluType} <em>*</em></span>
          <select
            className="bot-create-dialog__select"
            value={nluType}
            onChange={(event) => {
              const nextType = event.target.value as NluType;
              setNluType(nextType);
              setNluModel(defaultNluModelForType(nextType));
              setVectorConnections((current) => ({
                ...current,
                intent: nextType === "semantic_vector"
                  ? internalVectorConnection("intent")
                  : normalizeVectorConnection(current.intent),
              }));
            }}
          >
            {NLU_TYPE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value} disabled={option.disabled}>
                {translateAiOptionText(uiLanguage, option.label)} ({translateAiOptionText(uiLanguage, option.note)})
              </option>
            ))}
          </select>
        </div>

        {nluType !== "llm" ? (
          <div className="bot-create-dialog__field cga-bot-create-page__field--nlu-model">
            <span className="bot-create-dialog__label">{copy.nluModel} <em>*</em></span>
            <select
              className="bot-create-dialog__select"
              value={nluModel}
              onChange={(event) => setNluModel(event.target.value as NluModelKey)}
            >
              {nluModelOptions.map((option) => (
                <option key={option.value} value={option.value} disabled={option.disabled}>
                  {translateAiOptionText(uiLanguage, option.label)} ({translateAiOptionText(uiLanguage, option.note)})
                </option>
              ))}
            </select>
            {nluModelDescription ? (
              <small>{translateAiOptionText(uiLanguage, nluModelDescription)}</small>
            ) : null}
          </div>
        ) : null}

        <div className="bot-create-dialog__field cga-bot-create-page__field--answer-mode">
          <span className="bot-create-dialog__label">{copy.answerMode} <em>*</em></span>
          <select
            className="bot-create-dialog__select"
            value={answerMode}
            onChange={(event) => {
              const nextAnswerMode = event.target.value as AnswerMode;
              setAnswerMode(nextAnswerMode);
              setVectorConnections((current) => ({
                ...current,
                answer: nextAnswerMode === "semantic_rag"
                  ? internalVectorConnection("answer")
                  : normalizeVectorConnection(current.answer),
              }));
            }}
          >
            {ANSWER_MODE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value} disabled={option.disabled}>
                {translateAiOptionText(uiLanguage, option.label)} ({translateAiOptionText(uiLanguage, option.note)})
              </option>
            ))}
          </select>
        </div>

        {usesLlmEngine ? (
          <>
            <div className="bot-create-dialog__field cga-bot-create-page__field--llm-provider">
              <span className="bot-create-dialog__label">LLM Provider <em>*</em></span>
              <select
                className="bot-create-dialog__select"
                value={llmProvider}
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
            </div>

            <div className="bot-create-dialog__field cga-bot-create-page__field--llm-model">
              <span className="bot-create-dialog__label">{copy.llmModel} <em>*</em></span>
              <select
                className="bot-create-dialog__select"
                value={llmModel}
                onChange={(event) => setLlmModel(event.target.value as LlmModelKey)}
              >
                {llmModelOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {translateAiOptionText(uiLanguage, option.label)} ({translateAiOptionText(uiLanguage, option.note)})
                  </option>
                ))}
              </select>
            </div>

            {llmProvider === "ollama" ? (
              <div className="bot-create-dialog__field cga-bot-create-page__field--llm-base">
                <span className="bot-create-dialog__label">{copy.ollamaAddress}</span>
                <input
                  type="url"
                  className="bot-create-dialog__input"
                  placeholder="http://192.168.220.180:11434"
                  value={llmBaseUrl}
                  onChange={(event) => setLlmBaseUrl(event.target.value)}
                />
              </div>
            ) : null}
          </>
        ) : null}

        {usesSemanticNlu ? (
          <div className="bot-create-dialog__field bot-create-dialog__field--vector cga-bot-create-page__field--intent-vector">
            <span className="bot-create-dialog__label">{copy.intentVectorConnection}</span>
            <small>
              {nluType === "semantic_external"
                ? copy.externalIntentVectorDescription
                : copy.internalIntentVectorDescription}
            </small>
            <select
              className="bot-create-dialog__select"
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
              <option value="false">{copy.disabled}</option>
              <option value="true">{copy.enabled}</option>
            </select>
            {nluType === "semantic_external" ? (
              <input
                type="url"
                className="bot-create-dialog__input"
                placeholder={copy.searchApiPlaceholder}
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
            ) : null}
            <input
              type="text"
              className="bot-create-dialog__input"
              placeholder={copy.indexNamePlaceholder}
              value={vectorConnections.intent?.index_name ?? ""}
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
            {nluType === "semantic_external" ? (
              <input
                type="password"
                className="bot-create-dialog__input"
                placeholder={copy.apiKeyPlaceholder}
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
            ) : null}
          </div>
        ) : null}

        {usesSemanticAnswer ? (
          <div className="bot-create-dialog__field bot-create-dialog__field--vector cga-bot-create-page__field--answer-vector">
            <span className="bot-create-dialog__label">{copy.answerVectorConnection}</span>
            <small>{copy.answerVectorDescription}</small>
            <select
              className="bot-create-dialog__select"
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
              <option value="false">{copy.disabled}</option>
              <option value="true">{copy.enabled}</option>
            </select>
            <input
              type="text"
              className="bot-create-dialog__input"
              placeholder={copy.indexNamePlaceholder.replace("aidot-intent", "aidot-answer")}
              value={vectorConnections.answer?.index_name ?? ""}
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
          </div>
        ) : null}

          </>
        ) : null}

        <div className="bot-create-dialog__field cga-bot-create-page__field--introduction">
          <span className="bot-create-dialog__label">{copy.introduction}</span>
          <input
            type="text"
            className="bot-create-dialog__input"
            placeholder={copy.introductionPlaceholder}
            value={introduction}
            onChange={(event) => setIntroduction(event.target.value)}
          />
        </div>

        {botKind === "bot" ? (
          <section className="bot-ai-combinations bot-ai-combinations--compact" aria-label={copy.combinationAria}>
          <div className="bot-ai-combinations__summary">
            <strong>{copy.selectedCombination}</strong>
            <span>
              {selectedCombination
                ? formatBotCreateText(copy.selectedCombinationSummary, {
                  nlu: translateAiOptionText(uiLanguage, selectedCombination.nluLabel),
                  model: translateAiOptionText(uiLanguage, selectedCombination.nluModelLabel),
                  answer: translateAiOptionText(uiLanguage, selectedCombination.answerLabel),
                })
                : copy.noCombination}
            </span>
            {selectedCombination ? (
              <em className={`bot-ai-combinations__badge bot-ai-combinations__badge--${selectedCombination.status}`}>
                {translateAiOptionText(uiLanguage, selectedCombination.statusLabel)}
              </em>
            ) : null}
          </div>
          <div className="bot-ai-combinations__matrix">
            <div className="bot-ai-combinations__axis bot-ai-combinations__axis--corner">
              <span>{copy.intentEngine}</span>
              <strong>{copy.answerEngine}</strong>
            </div>
            {ANSWER_MODE_OPTIONS.map((option) => (
              <div key={option.value} className="bot-ai-combinations__axis bot-ai-combinations__axis--answer">
                <span>{copy.answerEngine}</span>
                <strong>{translateAiOptionText(uiLanguage, option.label)}</strong>
              </div>
            ))}
            {aiCombinationRows.map((row) => (
              <div key={row.nluType} className="bot-ai-combinations__row">
                <div className="bot-ai-combinations__axis bot-ai-combinations__axis--nlu">
                  <span>{copy.intentEngine}</span>
                  <strong>{translateAiOptionText(uiLanguage, row.nluLabel)}</strong>
                  <small>{translateAiOptionText(uiLanguage, row.nluModelLabel)}</small>
                </div>
                {row.combinations.map((item) => {
                  const selected = item.nluType === nluType && item.answerMode === answerMode;
                  const unsupported = item.status === "unsupported";
                  return (
                    <button
                      type="button"
                      key={item.id}
                      className={`bot-ai-combinations__item bot-ai-combinations__item--${item.status}${selected ? " is-selected" : ""}`}
                      disabled={unsupported}
                      onClick={() => {
                        if (!unsupported) {
                          selectCombination(item.nluType, item.answerMode);
                        }
                      }}
                      aria-pressed={selected}
                    >
                      <span>{copy.combinationStatus}</span>
                      <strong>{translateAiOptionText(uiLanguage, item.statusLabel)}</strong>
                      <small>{unsupported ? copy.unavailable : selected ? copy.currentSelection : copy.available}</small>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
          <p>{selectedCombination ? translateAiOptionText(uiLanguage, selectedCombination.note) : copy.combinationHelp}</p>
        </section>
        ) : null}

        {errorMessage ? <p className="form-message form-message--error">{errorMessage}</p> : null}
      </div>

      <div className="bot-create-dialog__footer">
        <Link href="/studio/bots" className="bot-create-dialog__ghost">
          {copy.cancel}
        </Link>
        <button
          type="button"
          className="bot-create-dialog__primary"
          disabled={isSubmitting || !creationAllowed}
          onClick={handleSubmit}
        >
          {isSubmitting ? copy.creating : copy.confirm}
        </button>
      </div>
    </div>
  );
}
