"use client";

import { formatStudioRuntimeMessage, getStudioRuntimeMessage } from "@/lib/i18n/studio-runtime-native";

import * as AdaptiveCards from "adaptivecards";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { Fragment, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";

import { useI18n } from "@/components/language-provider";
import { evaluateSecondaryOutcome, prepareSecondaryVariables, secondaryOutcomeMapping, secondaryConfigForVersion, type SecondaryModuleConfig } from "@/lib/secondary-llm";
import { normalizeAnswerMode } from "@/lib/answer-options";
import { getBotVersionSettings, type MessageItemConfig } from "@/lib/bot-settings";
import { isSimulatorRichFormLocalFilePath, normalizeSimulatorRichFormAssetUrl } from "./simulator-rich-form-assets";
import { normalizeApiAssets } from "@/lib/api-assets";
import {
  normalizeAidotAdaptiveCard,
  getAidotRichFormComponents,
  readAidotRichFormArray,
  readAidotRichFormString,
  type AidotRichFormComponent,
} from "../../../packages/shared/src";
import { applyUpdatedVersionToBot, getBotDialogs, withUpdatedDialogs } from "@/lib/dialog-assets";
import {
  CONDITION_OPERATOR_OPTIONS,
  FLOW_CARD_LABELS,
  collectDialogFlowVariables,
  formatVariableName,
  getDialogFlowGraph,
  getFlowLinkTargetId,
  stripVariablePrefix,
  type DialogFlowGraph,
  type DialogFlowNode,
} from "@/lib/dialog-flow";
import { loadAuthSession, saveLastBotScreen, type AuthSession } from "@/lib/auth";
import {
  fetchStudioPersistentConfig, fetchStudioPersistentRooms, fetchStudioPersistentRoom, fetchStudioPersistentTurn,
  startStudioPersistentRoom, submitStudioPersistentTurn, ackStudioPersistentTurn, resetStudioPersistentRoom,
  closeStudioPersistentRoom, manageStudioPersistentTurn, persistentRoomStorageKey, canSendPersistentTurn,
  persistentDisplayReceipt, persistentTranscriptNeedsRefresh, isPersistentSubmissionRejected, isPersistentQuestionValid, type PersistentConfig, type PersistentRoom, type PersistentTurn, type PersistentTranscript,
  generateStudioBotVersionLlmAnswer,
  refreshStudioBotSelectedVersion,
  searchStudioBotVersionRagAnswers,
  testStudioBotVersionLlmIntent,
  testStudioBotVersionMlIntent,
  testStudioBotVersionSemanticIntent,
  type StudioBotApiItem,
  type StudioBotVersionApiItem,
  updateStudioBotVersionDialogs,
} from "@/lib/studio-bots-api";
import {
  classifyIntent,
  tokenizeForDeepLearningLite,
  trainIntentClassifier,
  type IntentClassificationModel,
  type IntentClassificationScore,
} from "@/lib/nlu";
import {
  normalizeVersionDocument,
  normalizeVersionDialogs,
  normalizeVersionEntities,
  type VersionDialogAsset,
  type VersionDialogUtterance,
  type VersionDocument,
} from "@/lib/version-document";
import { isSemanticNluType, type NluType } from "@/lib/nlu-options";
import { normalizeSupportedLanguage, type SupportedLanguage } from "@/lib/language";
import { formatSimulatorText, getSimulatorLabel, SIMULATOR_CATALOGS, SIMULATOR_DTMF_CATALOGS } from "@/lib/i18n/simulator";

import {
  createAidotSharedRoom, joinAidotSharedRoom, fetchAidotSharedRoom, sendAidotSharedRoomMessage,
  canAskAidotRoomBot, canSendAidotRoomChat, canRespondAidotRoomCard,
  type AidotSharedRoomIdentity, type AidotSharedRoomHistoryResponse,
} from "../../../packages/shared/src/channel";

const ADAPTIVE_CARD_SCHEMA_VERSION = "1.6";
const SIMULATOR_RUNTIME_MESSAGES = {
  systemError: "처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
  runtimeFlowError: "대화 흐름 설정 오류로 대화를 계속할 수 없습니다.",
  runtimeModuleNotFound: "연결할 대화 모듈을 찾지 못했습니다.",
  scriptException: "Script 실행 중 오류가 발생했습니다.",
  functionException: "API 호출 중 오류가 발생했습니다.",
  conditionNoElseDetail: "Condition 카드에 '그 외의 경우' 분기가 없어 대화를 계속할 수 없습니다.",
  conditionTargetMissingDetail: "Condition 분기 연결 대상이 없어 대화를 계속할 수 없습니다.",
  jumpTargetMissingDetail: "Jump 대상이 설정되어 있지 않아 대화를 중단했습니다.",
  unsupportedNluMode: "선택한 NLU 엔진은 아직 실행 엔진에 연결되지 않았습니다.",
  unsupportedAnswerMode:
    "선택한 답변 방식은 아직 실행 엔진에 연결되지 않았습니다. 현재 실행 가능한 답변 방식은 정해진 답변, Semantic RAG 답변, LLM RAG 답변, LLM 답변입니다.",
} as const;

type SimulatorMessage = {
  participantName?: string;
  id: string;
  sender: "bot" | "user" | "system";
  text: string;
  timestamp: string;
  cardTitle?: string;
  cardImageUrl?: string;
  cardDescription?: string;
  cardItems?: Array<{ label: string; value: string; href?: string }>;
  html?: string;
  dtmf?: {
    minLength: number;
    maxLength: number;
    endCharacter: string;
    firstInputTimeoutMs: number;
    overallInputTimeoutMs: number;
  };
  table?: {
    columns: string[];
    rows: Array<{ key: string; firstCell?: string; values: string[] }>;
    selectable?: boolean;
  };
  template?: {
    kind: "rich-form" | "adaptive-card";
    title: string;
    lines: string[];
    actions: string[];
    msgKey?: string;
    active?: boolean;
    valid?: boolean;
    rawJson?: unknown;
    schemaVersion?: string;
  };
  quickReplies?: string[];
  analysisId?: string;
  sourceTalkNodeId?: string;
  debugLogs?: Array<{ level: SimulatorLogLevel; event: string; detail: Record<string, unknown> }>;
};

// 저장된 Webchat 메시지를 기존 카드 렌더러의 표시 모델에 연결한다.
function sharedMessageToSimulatorMessage(message: import("../../../packages/shared/src/channel").AidotChannelStoredMessage): SimulatorMessage {
  const payload = (message.payload || {}) as Record<string, unknown>;
  const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const text = (value: unknown) => typeof value === "string" ? value : value == null ? "" : String(value);
  const safeUrl = (value: unknown) => {
    const valueText = text(value);
    try { const url = new URL(valueText); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; } catch { return ""; }
  };
  const result: SimulatorMessage = {
    id: message.id, sender: message.participantKind, participantName: message.participantName,
    text: message.text, timestamp: message.createdAt, sourceTalkNodeId: text(payload.sourceTalkNodeId) || undefined,
  };
  if (message.participantKind === "user") {
    try {
      const submitted = record(JSON.parse(message.text));
      if (submitted.webchatRichFormVersion === "1.0" && typeof submitted.sourceTalkNodeId === "string") {
        const response = record(submitted.response);
        const selected = text(response.buttonValue) || Object.values(response).map(value => text(record(value).value)).filter(Boolean).join(", ");
        if (selected) result.text = selected;
      }
    } catch { /* 일반 텍스트와 JSON 원문은 그대로 표시한다. */ }
  }
  if (message.messageType === "button") {
    result.quickReplies = Array.isArray(payload.options) ? payload.options.filter((value): value is string => typeof value === "string") : [];
  } else if (message.messageType === "link-button") {
    result.cardTitle = message.text || "링크";
    result.cardItems = (Array.isArray(payload.options) ? payload.options : []).flatMap(value => {
      const item = record(value); const href = safeUrl(item.url);
      return href ? [{ label: text(item.label), value: href, href }] : [];
    });
  } else if (message.messageType === "card" || message.messageType === "carousel") {
    const card = record(message.messageType === "card" ? payload.card : payload.carousel);
    result.cardTitle = text(card.itemTitle || card.title) || message.text || "카드";
    result.cardDescription = text(card.itemContents || card.description);
    result.cardImageUrl = safeUrl(card.imageUrl);
    if (card.itemButtonValue) result.quickReplies = [text(card.itemButtonValue)];
  } else if (message.messageType === "table") {
    const columns = Array.isArray(payload.columns) ? payload.columns.map(text) : [];
    const keyColumn = text(payload.keyColumn) || columns[0] || "";
    const rows = (Array.isArray(payload.rows) ? payload.rows : []).map(value => {
      const row = record(value);
      return { key: text(row[keyColumn]), ...(keyColumn !== columns[0] ? { firstCell: text(row[columns[0]]) } : {}), values: columns.slice(1).map(column => text(row[column])) };
    });
    result.table = { columns, rows, selectable: payload.selectable === true };
  } else if (message.messageType === "dtmf") {
    const dtmf = record(payload.dtmf);
    result.dtmf = { minLength: Number(dtmf.minLength) || 1, maxLength: Number(dtmf.maxLength) || 1,
      endCharacter: text(dtmf.endCharacter) || "#", firstInputTimeoutMs: Number(dtmf.firstInputTimeoutMs) || 1000,
      overallInputTimeoutMs: Number(dtmf.overallInputTimeoutMs) || 1000 };
  } else if (message.messageType === "form" || message.messageType === "form-a-card") {
    const rich = message.messageType === "form";
    const source = rich ? payload.richForm : payload.adaptiveCard;
    result.template = { kind: rich ? "rich-form" : "adaptive-card", title: rich ? "Rich Form" : "Adaptive Card",
      lines: [], actions: [], rawJson: source, valid: Boolean(source), active: true };
  }
  return result;
}

type SharedSimulatorTemplateCache = Map<string, { signature: string; template: NonNullable<SimulatorMessage["template"]> }>;

function sharedMessagesForSimulator(messages: import("../../../packages/shared/src/channel").AidotChannelStoredMessage[], templates: SharedSimulatorTemplateCache): SimulatorMessage[] {
  const next = new Map<string, { signature: string; template: NonNullable<SimulatorMessage["template"]> }>();
  const displayed = messages.map(message => {
    const rendered = sharedMessageToSimulatorMessage(message);
    if (rendered.template) {
      const signature = JSON.stringify(rendered.template);
      const previous = templates.get(message.id);
      if (previous?.signature === signature) rendered.template = previous.template;
      next.set(message.id, { signature, template: rendered.template });
    }
    return rendered;
  });
  // 방을 바꾸거나 기록이 제거되면 이전 template을 보관하지 않는다.
  templates.clear();
  next.forEach((value, key) => templates.set(key, value));
  return displayed;
}

type SimulatorAnalysis = {
  id: string;
  nluType: NluType;
  utterance: string;
  selectedIntentName: string;
  cutoffScore: number;
  similarIntentScore: number;
  scoreIn: Array<{ intentName: string; score: number; features: string[] }>;
  scoreOut: Array<{ intentName: string; score: number; features: string[] }>;
  entities: Array<{ name: string; variableName: string; value: string; targetValue: string; matched: boolean }>;
  morphemes: Array<{ token: string; tag: string; description: string }>;
  cardLogs: Array<{ cardType: string; cardName: string; description: string; variables: Record<string, string> }>;
  variableSnapshots: Array<{ label: string; variables: Record<string, string> }>;
};

const PRE_NLU_DECISION_LOG_TYPES = ["제외/무시", "스몰토크", "Exacting Matching", "룰"] as const;

type NluValidationDiagnostic = {
  dialogId: string;
  utterance: string;
  expectedIntentName: string;
  expectedScore: number;
  topIntentName: string;
  topScore: number;
  secondIntentName: string;
  secondScore: number;
  features: string[];
  expectedFeatures: string[];
  commonFeatures: string[];
  expectedTrainingCount: number;
  status: "정상" | "미분류" | "오분류" | "유사의도";
  diagnosisType: "공통 Feature 충돌" | "학습문장 부족" | "Score 설정 후보" | "정상";
  reason: string;
  suggestion: string;
};

type RuntimeState = {
  secondaryConfig?: SecondaryModuleConfig;
  secondaryError?: boolean;
  dialog: VersionDialogAsset;
  graph: DialogFlowGraph;
  nextNodeId: string;
  variables: Record<string, string>;
  waitingTalkNodeId?: string;
  ended: boolean;
  sessionEnded?: boolean;
  beforeSessionEndModuleRan?: boolean;
  returnStack?: RuntimeReturnFrame[];
  transitionLocked?: boolean;
  returnBlocked?: boolean;
};

type RuntimeReturnFrame = {
  secondaryConfig?: SecondaryModuleConfig;
  secondaryError?: boolean;
  dialog: VersionDialogAsset;
  graph: DialogFlowGraph;
  nextNodeId: string;
  waitingTalkNodeId?: string;
};

type LoadedScript = {
  fileName: string;
  sentences: string[];
  index: number;
  active: boolean;
};

type PendingIntentSelection = {
  scores: IntentClassificationScore[];
  candidates: IntentClassificationScore[];
  noIntentButtonLabel: string;
  noIntentButtonMessage: string;
};

type SimulatorLogLevel = "debug" | "info" | "warn" | "error";

type EntityMatchResult = {
  name: string;
  variableName: string;
  value: string;
  targetValue: string;
  matched: boolean;
};

type SimulatorToolbarIconName = "restart" | "close-floating" | "load" | "download" | "list";

function SimulatorToolbarIcon({ name }: { name: SimulatorToolbarIconName }) {
  switch (name) {
    case "restart":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6.7 7.1A7 7 0 1 1 5 12" />
          <path d="M6.7 7.1H2.8V3.2" />
        </svg>
      );
    case "close-floating":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 6l12 12" />
          <path d="M18 6L6 18" />
        </svg>
      );
    case "load":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M7 8h7a5 5 0 0 1 0 10H9" />
          <path d="M7 8l4-4" />
          <path d="M7 8l4 4" />
        </svg>
      );
    case "download":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 3v12" />
          <path d="M7 10l5 5 5-5" />
          <path d="M5 20h14" />
        </svg>
      );
    case "list":
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M5 7h14" />
          <path d="M5 12h14" />
          <path d="M5 17h14" />
        </svg>
      );
  }
}
function nowText() {
  return new Date().toISOString();
}

const SIMULATOR_TIME_LOCALES: Record<SupportedLanguage, string> = {
  ko: "ko-KR",
  en: "en-US",
  "zh-CN": "zh-CN",
  ja: "ja-JP",
  vi: "vi-VN",
  fr: "fr-FR",
  de: "de-DE",
};

function formatMessageTime(value: string, language: SupportedLanguage) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }

  return date.toLocaleTimeString(SIMULATOR_TIME_LOCALES[language], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  });
}

function queueRuntimeReturn(currentRuntime: RuntimeState | null, targetDialog: VersionDialogAsset): RuntimeReturnFrame[] {
  if (!currentRuntime || currentRuntime.ended || currentRuntime.returnBlocked || currentRuntime.dialog.returnBlocked) {
    return currentRuntime?.returnStack ? [...currentRuntime.returnStack] : [];
  }
  if (currentRuntime.dialog.id === targetDialog.id) {
    return currentRuntime.returnStack ? [...currentRuntime.returnStack] : [];
  }
  const stack = currentRuntime.returnStack ? [...currentRuntime.returnStack] : [];
  const top = stack[stack.length - 1];
  if (top?.dialog.id === currentRuntime.dialog.id) {
    return stack;
  }
  stack.push({
    dialog: currentRuntime.dialog,
    graph: currentRuntime.graph,
    nextNodeId: currentRuntime.nextNodeId,
    waitingTalkNodeId: currentRuntime.waitingTalkNodeId,
    secondaryConfig: currentRuntime.secondaryConfig,
    secondaryError: currentRuntime.secondaryError,
  });
  return stack;
}

function restoreRuntimeReturn(currentRuntime: RuntimeState): RuntimeState | null {
  const stack = currentRuntime.returnStack ? [...currentRuntime.returnStack] : [];
  const frame = stack.pop();
  if (!frame) {
    return null;
  }
  return {
    dialog: frame.dialog,
    graph: frame.graph,
    nextNodeId: frame.nextNodeId,
    waitingTalkNodeId: frame.waitingTalkNodeId,
    secondaryConfig: frame.secondaryConfig,
    secondaryError: frame.secondaryError,
    variables: currentRuntime.variables,
    ended: false,
    sessionEnded: false,
    beforeSessionEndModuleRan: currentRuntime.beforeSessionEndModuleRan,
    returnStack: stack,
    transitionLocked: frame.dialog.transitionLocked || stack.some((item) => item.dialog.transitionLocked),
    returnBlocked: frame.dialog.returnBlocked,
  };
}

function normalizeText(value: unknown) {
  return typeof value === "string" ? value.normalize("NFKC").trim().replace(/\s+/g, " ").toLowerCase() : "";
}

function normalizeTextKey(value: unknown) {
  return normalizeText(value);
}

function normalizeEntityComparable(value: unknown) {
  return normalizeText(value).replace(/\s+/g, "");
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function tokenize(value: string) {
  return normalizeText(value)
    .split(/[^\p{L}\p{N}_]+/u)
    .map((item) => item.trim())
    .filter(Boolean);
}

function makeMessage(
  sender: SimulatorMessage["sender"],
  text: string,
  options?: Partial<Omit<SimulatorMessage, "id" | "sender" | "text" | "timestamp">>,
): SimulatorMessage {
  return {
    id: crypto.randomUUID(),
    sender,
    text,
    timestamp: nowText(),
    ...options,
  };
}

function postSimulatorLog(payload: Record<string, unknown>) {
  void fetch("/api/simulator/logs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch(() => {
    // Logging must never block or alter simulator runtime behavior.
  });
}

function getGraphStartNodeId(graph: DialogFlowGraph) {
  const startNode = graph.nodes.find((node) => node.kind === "start");
  if (!startNode) {
    return "";
  }
  return getFlowLinkTargetId(graph, startNode.id, "next");
}

function getNextNodeId(graph: DialogFlowGraph, nodeId: string, sourcePort = "next") {
  return getFlowLinkTargetId(graph, nodeId, sourcePort);
}

function getNodeById(graph: DialogFlowGraph, nodeId: string) {
  return graph.nodes.find((node) => node.id === nodeId) ?? null;
}

function getNodeByIdOrTitle(graph: DialogFlowGraph, value: string) {
  const key = value.trim();
  if (!key) return null;
  return graph.nodes.find((node) => node.id === key || node.title.trim() === key) ?? null;
}

function normalizeRuntimeVariableName(value: string) {
  return stripVariablePrefix(value)
    .replace(/\[\]/g, ".0")
    .replace(/\[(?:"([^"]+)"|'([^']+)'|(\d+))\]/g, (_token, doubleQuoted: string, singleQuoted: string, numeric: string) =>
      `.${doubleQuoted ?? singleQuoted ?? numeric}`,
    );
}

function stringifyRuntimeVariableValue(value: unknown) {
  if (value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (typeof value === "object") {
    const source = value as Record<string, unknown>;
    const text = source.text ?? source.value ?? source.target;
    if (typeof text === "string") {
      return text;
    }
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function normalizeRuntimeVariableValues(values?: Record<string, unknown>) {
  return Object.entries(values ?? {}).reduce<Record<string, string>>((acc, [key, value]) => {
    const normalizedKey = normalizeRuntimeVariableName(key);
    if (normalizedKey) {
      acc[normalizedKey] = stringifyRuntimeVariableValue(value);
    }
    return acc;
  }, {});
}

function replaceVariables(value: string, variables: Record<string, string>) {
  return value
    .replace(
      /\{\{\s*(\$[^{}\s]+)\.(target|value)\(\)\s*\}\}/gu,
      (token, variableToken: string, accessor: string) => {
        const key = normalizeRuntimeVariableName(variableToken);
        return accessor === "target"
          ? variables[`${key}.__target`] ?? variables[key] ?? ""
          : variables[key] ?? "";
      },
    )
    .replace(/\{\{\s*(\$[^{}\s]+)\s*\}\}/gu, (token, variableToken: string) => {
      return resolveRuntimeVariableValue(variables, variableToken);
    })
    .replace(
      /\$[\p{ID_Start}_][$_‌‍\p{ID_Continue}]*(?:\.[\p{ID_Start}_][$_‌‍\p{ID_Continue}-]*|\[(?:"[^"]+"|'[^']+'|\d+)\])*\.(target|value)\(\)/gu,
      (token, accessor: string) => {
        const key = normalizeRuntimeVariableName(token.replace(/\.(?:target|value)\(\)$/u, ""));
        const resolved = accessor === "target" ? variables[`${key}.__target`] ?? variables[key] : variables[key];
        return resolved ?? token;
      },
    )
    .replace(/\$[\p{ID_Start}_][$_‌‍\p{ID_Continue}]*(?:\.[\p{ID_Start}_][$_‌‍\p{ID_Continue}-]*|\[(?:"[^"]+"|'[^']+'|\d+)\])*/gu, (token) => {
      const resolved = resolveRuntimeVariableValue(variables, token);
      return resolved || token;
    });
}

function withFunctionResultAlias(
  variables: Record<string, string>,
  resultVariableName: string,
  resultValue: string,
  statusValue: string,
  resultSource: unknown,
) {
  const normalizedResultVariableName = stripVariablePrefix(resultVariableName) || "apiResult";
  const aliasVariables: Record<string, string> = {
    ...variables,
    ...Object.fromEntries(flattenFunctionOutputVariables("result", resultSource)),
    ...Object.fromEntries(flattenFunctionOutputVariables(normalizedResultVariableName, resultSource)),
    "result.__status": statusValue,
  };
  aliasVariables[normalizedResultVariableName] = resultValue;
  aliasVariables[`${normalizedResultVariableName}.__status`] = statusValue;
  return aliasVariables;
}
function stripHtml(value: string) {
  return value.replace(/<br\s*\/?>/gi, "\n").replace(/<[^>]+>/g, "").trim();
}

function buildMorphemes(utterance: string) {
  return tokenize(utterance).map((token) => ({
    token,
    tag: /\d/.test(token) ? "SN" : /^[a-z]+$/i.test(token) ? "SL" : "NNG",
    description: /\d/.test(token) ? "숫자" : /^[a-z]+$/i.test(token) ? "외국어" : "일반 명사",
  }));
}

function matchEntityRows(
  entity: ReturnType<typeof normalizeVersionEntities>[number],
  utterance: string,
): { value: string; target: string } | null {
  const sourceUtterance = typeof utterance === "string" ? utterance.normalize("NFKC") : "";
  const normalizedUtterance = normalizeText(utterance);
  const compactUtterance = normalizeEntityComparable(utterance);

  for (const row of entity.rows) {
    const target = row.value.normalize("NFKC").trim();

    if (row.rowType === "P") {
      const matchedPattern = row.details.find((pattern) => {
        try {
          return new RegExp(pattern).test(utterance);
        } catch {
          return false;
        }
      });
      if (matchedPattern) {
        const match = new RegExp(matchedPattern).exec(utterance);
        return { value: match?.[0] ?? matchedPattern, target: target || match?.[0] || matchedPattern };
      }
      continue;
    }

    const candidates = [target, ...row.details]
      .map((item) => item.normalize("NFKC").trim())
      .filter(Boolean)
      .sort((left, right) => right.length - left.length);
    const matched = candidates.find((candidate) => {
      const normalizedCandidate = normalizeText(candidate);
      const compactCandidate = normalizeEntityComparable(candidate);
      if (!normalizedCandidate) {
        return false;
      }
      if (sourceUtterance.includes(candidate)) {
        return true;
      }
      if (normalizedUtterance.includes(normalizedCandidate)) {
        return true;
      }
      if (compactCandidate && compactUtterance.includes(compactCandidate)) {
        return true;
      }
      try {
        const escaped = escapeRegExp(candidate).replace(/\s+/g, "\\s*");
        return new RegExp(escaped, "iu").test(sourceUtterance);
      } catch {
        return false;
      }
    });
    if (matched) {
      return { value: matched, target: target || matched };
    }
  }

  return null;
}

function matchEntityValue(
  versionDocument: VersionDocument,
  entityId: string,
  entityName: string,
  utterance: string,
): { value: string; target: string } | null {
  const normalizedEntityId = typeof entityId === "string" ? entityId.trim() : "";
  const normalizedEntityName = typeof entityName === "string" ? entityName.replace(/^@/, "").trim() : "";
  const entities = normalizeVersionEntities(versionDocument.entities);
  const entity = entities.find(
    (item) =>
      (normalizedEntityId && item.id === normalizedEntityId) ||
      item.name.replace(/^@/, "").trim() === normalizedEntityName,
  );

  if (!entity) {
    return null;
  }

  const directMatch = matchEntityRows(entity, utterance);
  if (directMatch) {
    return directMatch;
  }

  return null;
}

function setVariableValue(
  variables: Record<string, string>,
  name: string,
  value: string,
  targetValue?: string,
) {
  variables[name] = value;
  if (targetValue != null) {
    variables[`${name}.__target`] = targetValue;
  }
}

function detectEntities(versionDocument: VersionDocument, dialog: VersionDialogAsset, utterance: string) {
  const normalizedUtterance = normalizeText(utterance);
  return dialog.entityBindings.map((binding) => {
    const entityMatch = matchEntityValue(versionDocument, binding.entityId, binding.entityName, utterance);
    const candidates = [
      typeof binding.entityValue === "string" ? binding.entityValue : "",
      ...(typeof binding.entityValue === "string" ? binding.entityValue.split(",") : []),
    ]
      .map((item) => item.trim())
      .filter(Boolean);
    const fallbackValue =
      candidates.find((candidate) => normalizedUtterance.includes(normalizeText(candidate))) ?? "";
    const matchedValue = entityMatch?.value ?? fallbackValue;

    return {
      name: binding.entityName,
      variableName: formatVariableName(binding.variableName) || `$${binding.variableName}`,
      value: matchedValue,
      targetValue: entityMatch?.target ?? matchedValue,
      matched: Boolean(matchedValue),
    };
  });
}

function buildAnalysis(
  versionDocument: VersionDocument,
  utterance: string,
  scores: IntentClassificationScore[],
  selectedDialog: VersionDialogAsset | null,
  cutoffScore: number,
  similarIntentScore: number,
  extraEntities: EntityMatchResult[] = [],
  analysisNluType: NluType = "ml",
  language: SupportedLanguage = "ko",
): SimulatorAnalysis {
  const scoreIn = scores
    .filter((item) => item.score >= cutoffScore)
    .map((item) => ({
      intentName: item.dialog.name,
      score: item.score,
      features: item.features,
    }));
  const scoreOut = scores
    .filter((item) => item.score < cutoffScore)
    .slice(0, 10)
    .map((item) => ({
      intentName: item.dialog.name,
      score: item.score,
      features: item.features,
    }));

  return {
    id: crypto.randomUUID(),
    nluType: analysisNluType,
    utterance,
    selectedIntentName: selectedDialog?.name ?? getStudioRuntimeMessage(language, "의도 미분류"),
    cutoffScore,
    similarIntentScore,
    scoreIn,
    scoreOut,
    entities: [...(selectedDialog ? detectEntities(versionDocument, selectedDialog, utterance) : []), ...extraEntities],
    morphemes: analysisNluType === "ml" ? buildMorphemes(utterance) : [],
    cardLogs: [],
    variableSnapshots: [],
  };
}

function normalizeScoreSetting(value: number) {
  return value <= 1 ? Math.round(value * 100) : Math.round(value);
}

function getSimilarIntentThreshold(topScore: number, similarIntentScore: number) {
  return Math.round(topScore * (similarIntentScore / 100) * 100) / 100;
}

function getSimilarCandidates(
  scores: IntentClassificationScore[],
  scoreCutoff: number,
  similarIntentScore: number,
  maxIntentResults: number,
) {
  const selectedScore = scores[0] ?? null;
  if (!selectedScore || selectedScore.score < scoreCutoff) {
    return [];
  }

  const threshold = getSimilarIntentThreshold(selectedScore.score, similarIntentScore);
  return scores
    .filter((item, index) => index === 0 || (item.score >= scoreCutoff && item.score >= threshold))
    .slice(0, maxIntentResults);
}

function intersectFeatures(left: string[], right: string[]) {
  const rightSet = new Set(right);
  return Array.from(new Set(left.filter((item) => rightSet.has(item))));
}

function createRuntimeRegex(pattern: string) {
  const rawPattern = pattern.trim();
  const literalMatch = rawPattern.match(/^\/(.+)\/([dgimsuvy]*)$/);
  const source = literalMatch ? literalMatch[1] : rawPattern;
  const flags = literalMatch ? literalMatch[2] : "i";
  return new RegExp(source, flags);
}

function buildNluValidationDiagnostics(
  versionDocument: VersionDocument,
  nluModel: IntentClassificationModel,
  scoreCutoff: number,
  similarIntentScore: number,
  maxIntentResults: number,
  language: SupportedLanguage,
) {
  return versionDocument.dialogs
    .filter((dialog) => dialog.dialogType === 1)
    .flatMap((dialog) =>
      dialog.utterances
        .filter((utterance) => utterance.utteranceType === "V" && utterance.text.trim())
        .map((utterance) => {
          const scores = classifyIntent(nluModel, utterance.text);
          const top = scores[0] ?? null;
          const second = scores[1] ?? null;
          const expectedScore = scores.find((score) => score.dialog.id === dialog.id) ?? null;
          const candidates = getSimilarCandidates(scores, scoreCutoff, similarIntentScore, maxIntentResults);
          const selected = top && top.score >= scoreCutoff ? top : null;
          const isAmbiguous = candidates.length > 1;
          const isWrong = selected !== null && selected.dialog.id !== dialog.id;
          const status: NluValidationDiagnostic["status"] =
            selected === null ? "미분류" : isWrong ? "오분류" : isAmbiguous ? "유사의도" : "정상";
          const commonFeatures = intersectFeatures(top?.features ?? [], expectedScore?.features ?? []);
          const expectedTrainingCount = nluModel.deepLearningLite.documents.filter(
            (document) => document.dialog.id === dialog.id,
          ).length;
          const scoreGap = (top?.score ?? 0) - (expectedScore?.score ?? 0);
          const diagnosisType: NluValidationDiagnostic["diagnosisType"] =
            status === "정상"
              ? "정상"
              : expectedTrainingCount < 10 || (expectedScore?.score ?? 0) < scoreCutoff
                ? "학습문장 부족"
                : (status === "오분류" || status === "유사의도") && (commonFeatures.length >= 2 || scoreGap <= 10)
                  ? "공통 Feature 충돌"
                  : "Score 설정 후보";
          const reason =
            status === "미분류"
              ? formatStudioRuntimeMessage(language, "최고 점수 {top}점이 Cut-off {cutoff}점 미만입니다. 기대 의도 점수는 {expected}점입니다.", { top: top?.score.toFixed(2) ?? "0.00", cutoff: scoreCutoff, expected: expectedScore?.score.toFixed(2) ?? "0.00" })
              : status === "오분류"
                ? formatStudioRuntimeMessage(language, "기대 의도와 1순위 의도가 다릅니다. 점수 차이는 {gap}점입니다.", { gap: scoreGap.toFixed(2) })
                : status === "유사의도"
                  ? formatStudioRuntimeMessage(language, "1순위 대비 {score}% 기준 안에 다른 의도가 포함됩니다.", { score: similarIntentScore })
                  : getStudioRuntimeMessage(language, "검증 문장이 기대 의도로 분류되었습니다.");
          const suggestion =
            diagnosisType === "학습문장 부족"
              ? getStudioRuntimeMessage(language, "기대 의도에 같은 의미의 T 문장을 먼저 보강하세요. 문서 기준상 의도별 최소 10개 이상을 권장합니다.")
              : diagnosisType === "공통 Feature 충돌"
                ? formatStudioRuntimeMessage(language, "공통 Feature{features}보다 의도를 가르는 표현을 T 문장에 추가하거나 동의어/패턴을 조정하세요.", { features: commonFeatures.length > 0 ? `(${commonFeatures.join(", ")})` : "" })
                : diagnosisType === "Score 설정 후보"
                  ? getStudioRuntimeMessage(language, "학습문장과 Feature가 충분한데도 후보가 과도하면 Cut-off 또는 유사의도 Score를 마지막에 조정하세요.")
                  : getStudioRuntimeMessage(language, "추가 조치가 필요 없습니다.");

          return {
            utterance: utterance.text,
            dialogId: dialog.id,
            expectedIntentName: dialog.name,
            expectedScore: expectedScore?.score ?? 0,
            topIntentName: top?.dialog.name ?? "-",
            topScore: top?.score ?? 0,
            secondIntentName: second?.dialog.name ?? "-",
            secondScore: second?.score ?? 0,
            features: top?.features ?? [],
            expectedFeatures: expectedScore?.features ?? [],
            commonFeatures,
            expectedTrainingCount,
            status,
            diagnosisType,
            reason,
            suggestion,
          } satisfies NluValidationDiagnostic;
        }),
    )
    .sort((left, right) => {
      const priority = { 오분류: 0, 미분류: 1, 유사의도: 2, 정상: 3 } satisfies Record<NluValidationDiagnostic["status"], number>;
      return priority[left.status] - priority[right.status] || right.topScore - left.topScore;
    })
    .slice(0, 30);
}

function getConfiguredTextMessage(item: MessageItemConfig) {
  if (!item.enabled || item.mode !== "text" || !item.value.trim()) {
    return "";
  }
  return item.value.trim();
}

function parseLoadedConversation(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === "string") {
          return item;
        }
        if (item && typeof item === "object" && !Array.isArray(item)) {
          const source = item as Record<string, unknown>;
          return typeof source.text === "string" && source.sender !== "bot" ? source.text : "";
        }
        return "";
      })
      .map((item) => item.trim())
      .filter(Boolean);
  }

  if (value && typeof value === "object") {
    const source = value as Record<string, unknown>;
    return parseLoadedConversation(source.sentences ?? source.utterances ?? source.messages ?? []);
  }

  return [];
}

function readRecordValue(source: unknown, key: string) {
  if (!source || typeof source !== "object" || Array.isArray(source)) {
    return "";
  }
  const record = source as Record<string, unknown>;
  const value = record[key];
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
}

function getValueByPath(source: unknown, path: string) {
  const segments = path
    .replace(/\[\]/g, ".0")
    .split(".")
    .map((segment) => segment.trim())
    .filter(Boolean);

  return segments.reduce<unknown>((current, segment) => {
    if (current == null) {
      return undefined;
    }

    if (Array.isArray(current)) {
      const index = Number(segment);
      return Number.isInteger(index) ? current[index] : undefined;
    }

    if (typeof current === "object") {
      return (current as Record<string, unknown>)[segment];
    }

    return undefined;
  }, source);
}

function stringifyVariableValue(value: unknown) {
  if (value == null) {
    return "";
  }

  if (typeof value === "string") {
    return value;
  }

  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }

  return JSON.stringify(value);
}

function parseJsonValue(value: string): { value: unknown; error?: string } {
  try {
    return { value: JSON.parse(value) as unknown };
  } catch (error) {
    let depth = 0;
    let inString = false;
    let escaped = false;
    let start = -1;
    for (let index = 0; index < value.length; index += 1) {
      const char = value[index];
      if (inString) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') inString = false;
        continue;
      }
      if (char === '"') {
        inString = true;
        continue;
      }
      if (char === "{" || char === "[") {
        if (depth === 0) start = index;
        depth += 1;
      } else if (char === "}" || char === "]") {
        depth -= 1;
        if (depth === 0 && start >= 0) {
          try {
            return { value: JSON.parse(value.slice(start, index + 1)) as unknown };
          } catch {
            break;
          }
        }
      }
    }
    return { value: null, error: error instanceof Error ? error.message : "JSON parse error" };
  }
}

function getFunctionOutputValue(source: unknown, path: string) {
  const normalizedPath = path.trim();
  if (!normalizedPath || normalizedPath === "root") {
    return source;
  }

  const segments = normalizedPath
    .replace(/^root\.?/, "")
    .replace(/\[\]/g, ".0")
    .split(".")
    .map((segment) => segment.trim())
    .filter(Boolean);

  return segments.reduce<unknown>((current, segment) => {
    if (current == null) {
      return undefined;
    }

    if (Array.isArray(current)) {
      const index = Number(segment);
      return Number.isInteger(index) ? current[index] : undefined;
    }

    if (typeof current === "object") {
      return (current as Record<string, unknown>)[segment];
    }

    return undefined;
  }, source);
}

function stringifyFunctionOutputValue(value: unknown) {
  return stringifyVariableValue(value);
}

function flattenFunctionOutputVariables(variableName: string, value: unknown, depth = 0): Array<[string, string]> {
  const entries: Array<[string, string]> = [[variableName, stringifyFunctionOutputValue(value)]];
  if (depth >= 12 || value == null) {
    return entries;
  }

  if (Array.isArray(value)) {
    value.forEach((item, index) => {
      entries.push(...flattenFunctionOutputVariables(`${variableName}.${index}`, item, depth + 1));
    });
    return entries;
  }

  if (typeof value === "object") {
    Object.entries(value as Record<string, unknown>).forEach(([key, childValue]) => {
      entries.push(...flattenFunctionOutputVariables(`${variableName}.${key}`, childValue, depth + 1));
    });
  }

  return entries;
}

function flattenRuntimeValue(
  variableName: string,
  value: unknown,
  variables: Record<string, string>,
  depth = 0,
) {
  variables[variableName] = stringifyVariableValue(value);
  if (depth >= 12 || value == null) {
    return;
  }

  if (Array.isArray(value)) {
    value.forEach((item, index) => {
      flattenRuntimeValue(`${variableName}.${index}`, item, variables, depth + 1);
    });
    return;
  }

  if (typeof value === "object") {
    Object.entries(value as Record<string, unknown>).forEach(([key, childValue]) => {
      flattenRuntimeValue(`${variableName}.${key}`, childValue, variables, depth + 1);
    });
  }
}
function resolveRuntimeVariableValue(variables: Record<string, string>, variableName: string) {
  const normalizedName = normalizeRuntimeVariableName(variableName);
  if (!normalizedName) return "";
  if (Object.prototype.hasOwnProperty.call(variables, normalizedName)) {
    return variables[normalizedName] ?? "";
  }

  const responsePath = ".response.";
  if (normalizedName.includes(responsePath)) {
    const withoutResponse = normalizedName.replace(responsePath, ".");
    if (Object.prototype.hasOwnProperty.call(variables, withoutResponse)) {
      return variables[withoutResponse] ?? "";
    }
  } else {
    const [root, ...rest] = normalizedName.split(".");
    if (root && rest.length > 0) {
      const withResponse = `${root}.response.${rest.join(".")}`;
      if (Object.prototype.hasOwnProperty.call(variables, withResponse)) {
        return variables[withResponse] ?? "";
      }
    }
  }

  return "";
}
function buildTableMessage(
  node: Extract<DialogFlowNode, { kind: "talk" }>,
  variables: Record<string, string>,
  language: SupportedLanguage,
) {
  const config = node.config;
  const mappedColumns = config.tableColumnMappings
    .map((mapping) => mapping.column.trim() || mapping.value.trim())
    .filter(Boolean);
  const manualValueCount = Math.max(0, ...config.tableManualRows.map((row) => row.values.length));
  const valueColumns =
    mappedColumns.length > 0
      ? mappedColumns
      : Array.from({ length: manualValueCount }, (_, index) => (index === 0
        ? getStudioRuntimeMessage(language, "값")
        : formatStudioRuntimeMessage(language, "값{index}", { index: index + 1 })));
  const columns = [getStudioRuntimeMessage(language, "항목"), ...valueColumns];
  const debugLogs: SimulatorMessage["debugLogs"] = [];

  if (config.tableUseVariable) {
    const variableItemId = config.tableVariableItemId.trim();
    const variableName = stripVariablePrefix(variableItemId);
    const rawValue =
      (variableName ? variables[variableName] : undefined) ??
      (variableItemId ? variables[variableItemId] : undefined) ??
      "";
    if (rawValue.trim()) {
      const parsed = parseJsonValue(rawValue);
      if (parsed.error) {
        debugLogs.push({
          level: "warn",
          event: "simulator.table_variable_parse_failed",
          detail: { nodeId: node.id, nodeTitle: node.title, variableName, error: parsed.error, rawValue },
        });
      } else if (Array.isArray(parsed.value)) {
        const objectKeys = Array.from(
          new Set(
            parsed.value.flatMap((item) =>
              item && typeof item === "object" && !Array.isArray(item) ? Object.keys(item as Record<string, unknown>) : [],
            ),
          ),
        );
        const variableColumns = valueColumns.length > 0 ? valueColumns : objectKeys.length > 0 ? objectKeys : columns;
        const rows = parsed.value.map((item, index) => {
          if (Array.isArray(item)) {
            const values = item.map((cell) => String(cell ?? ""));
            return { key: values[0] || `${index + 1}`, values: values.slice(1) };
          }
          if (item && typeof item === "object") {
            const keyColumn = config.tableKeyColumn.trim();
            const mappedValues =
              config.tableColumnMappings.length > 0
                ? config.tableColumnMappings.map((mapping) =>
                    readRecordValue(item, mapping.value.trim() || mapping.column.trim()),
                  )
                : variableColumns.map((column) => readRecordValue(item, column));
            const firstColumn = variableColumns[0] ?? "";
            const key = keyColumn ? readRecordValue(item, keyColumn) : mappedValues[0] || `${index + 1}`;
            const values = keyColumn && firstColumn === keyColumn ? mappedValues.slice(1) : mappedValues.slice(1);
            return { key: key || `${index + 1}`, values };
          }
          return { key: `${index + 1}`, values: [String(item ?? "")] };
        });
        return { columns: variableColumns, rows, debugLogs };
      }
    }
  }

  return {
    columns,
    rows: config.tableManualRows.map((row) => ({
      key: replaceVariables(row.key, variables).trim() || "항목",
      values: row.values.map((value) => replaceVariables(value, variables).trim()),
    })),
    debugLogs,
  };
}

function collectTemplateValues(value: unknown, lines: string[], actions: string[], depth = 0) {
  if (depth > 5 || lines.length >= 8) {
    return;
  }

  if (Array.isArray(value)) {
    value.forEach((item) => collectTemplateValues(item, lines, actions, depth + 1));
    return;
  }

  if (!value || typeof value !== "object") {
    return;
  }

  const record = value as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : "";
  const text = typeof record.text === "string" ? record.text.trim() : "";
  const title = typeof record.title === "string" ? record.title.trim() : "";
  const label = typeof record.label === "string" ? record.label.trim() : "";
  const id = typeof record.id === "string" ? record.id.trim() : "";

  if (type.toLowerCase().includes("action") && (title || label)) {
    actions.push(title || label);
  } else if (text || title || label || id) {
    lines.push(text || title || label || id);
  }

  Object.entries(record).forEach(([key, item]) => {
    if (["type", "text", "title", "label", "id"].includes(key)) {
      return;
    }
    collectTemplateValues(item, lines, actions, depth + 1);
  });
}

function buildTemplateMessage(
  kind: "rich-form" | "adaptive-card",
  rawJson: string,
  fallbackTitle: string,
  node: Extract<DialogFlowNode, { kind: "talk" }>,
  msgKey = "",
) {
  const debugLogs: SimulatorMessage["debugLogs"] = [];
  let source: unknown = rawJson;

  if (kind === "rich-form") {
    const richFormSource = fallbackRichFormSource(rawJson) || fallbackRichFormSource(node.title) || rawJson;
    const components = getAidotRichFormComponents(richFormSource);
    if (components.length === 0) {
      debugLogs.push({
        level: "warn",
        event: "simulator.template_parse_failed",
        detail: { nodeId: node.id, nodeTitle: node.title, kind, msgKey, error: "RichForm component parse failed" },
      });
      return {
        kind,
        title: fallbackTitle,
        lines: [],
        actions: [],
        msgKey,
        active: false,
        valid: false,
        debugLogs,
      };
    }
    source = components;
  } else {
    const parsed = parseJsonValue(rawJson);
    if (parsed.error) {
      debugLogs.push({
        level: "warn",
        event: "simulator.template_parse_failed",
        detail: { nodeId: node.id, nodeTitle: node.title, kind, msgKey, error: parsed.error },
      });
      return {
        kind,
        title: fallbackTitle,
        lines: [],
        actions: [],
        msgKey,
        active: false,
        valid: false,
        debugLogs,
      };
    }
    source = normalizeAidotAdaptiveCard(parsed.value);
  }

  const schemaVersion = readRecordValue(source, "version") || ADAPTIVE_CARD_SCHEMA_VERSION;
  if (kind === "adaptive-card" && source && typeof source === "object" && !Array.isArray(source)) {
    (source as Record<string, unknown>).version = schemaVersion;
    if (!(source as Record<string, unknown>).$schema) {
      (source as Record<string, unknown>).$schema = AdaptiveCards.AdaptiveCard.schemaUrl;
    }
  }

  const lines: string[] = [];
  const actions: string[] = [];
  collectTemplateValues(source, lines, actions);
  const title =
    readRecordValue(source, "title") ||
    readRecordValue(source, "name") ||
    lines[0] ||
    fallbackTitle;

  return {
    kind,
    title,
    lines: lines.filter((line) => line !== title).slice(0, 6),
    actions: actions.slice(0, 5),
    msgKey,
    active: true,
    valid: true,
    rawJson: kind === "rich-form" ? source : source,
    schemaVersion,
    debugLogs,
  };
}
function isVisibleSimulatorMessage(message: SimulatorMessage) {
  if (message.text || message.html || message.dtmf || message.cardTitle || message.table || message.quickReplies?.length) {
    return true;
  }
  return Boolean(message.template?.valid);
}

function localizeSimulatorAnalysisLabel(language: SupportedLanguage, value: string) {
  return value
    .split(" / ")
    .map((part) => getStudioRuntimeMessage(language, part))
    .join(" / ");
}

function localizeSimulatorAnalysisDescription(language: SupportedLanguage, value: string) {
  for (const prefix of ["템플릿 JSON 파싱 실패", "Table 변수 JSON 파싱 실패"]) {
    if (value === prefix) return getStudioRuntimeMessage(language, prefix);
    if (value.startsWith(`${prefix}: `)) {
      return `${getStudioRuntimeMessage(language, prefix)}: ${value.slice(prefix.length + 2)}`;
    }
  }
  const extracted = value.match(/^(.*?) → (.*?): '(.*?)' 추출, target '(.*?)'$/);
  if (extracted) {
    return formatStudioRuntimeMessage(language, "{entity} → {variable}: '{value}' 추출, target '{target}'", {
      entity: extracted[1],
      variable: extracted[2],
      value: extracted[3],
      target: extracted[4],
    });
  }
  const failed = value.match(/^(.*?) → (.*?): 추출 실패\((.*)\)$/);
  if (failed) {
    return formatStudioRuntimeMessage(language, "{entity} → {variable}: 추출 실패({detail})", {
      entity: failed[1],
      variable: failed[2],
      detail: failed[3],
    });
  }
  return getStudioRuntimeMessage(language, value);
}

function localizeSimulatorAnalysisUtterance(language: SupportedLanguage, value: string) {
  return value === "대화 시작" ? getStudioRuntimeMessage(language, value) : value;
}

function describeSimulatorDebugLog(log: NonNullable<SimulatorMessage["debugLogs"]>[number]) {
  const detail = log.detail;
  const nodeTitle = typeof detail.nodeTitle === "string" ? detail.nodeTitle : "Talk";
  const error = typeof detail.error === "string" ? detail.error : "";
  switch (log.event) {
    case "simulator.template_parse_failed":
      return {
        cardType: "템플릿 오류",
        cardName: nodeTitle,
        description: `템플릿 JSON 파싱 실패${error ? `: ${error}` : ""}`,
      };
    case "simulator.table_variable_parse_failed":
      return {
        cardType: "Table 오류",
        cardName: nodeTitle,
        description: `Table 변수 JSON 파싱 실패${error ? `: ${error}` : ""}`,
      };
    default:
      return null;
  }
}

function richFormFieldName(component: AidotRichFormComponent, fallback: string) {
  return readAidotRichFormString(component, ["name", "id", "key", "variableName", "field", "title", "label", "text"], fallback);
}

function getTalkConfigForChannel(node: Extract<DialogFlowNode, { kind: "talk" }>, channelCode: string) {
  const config = node.config;
  const channelTemplate = config.channelTemplates?.[channelCode];
  return channelTemplate ? { ...config, ...channelTemplate } : config;
}

function richFormComponentCanSubmit(component: AidotRichFormComponent) {
  const type = String(component.type || "").trim().toUpperCase();
  if (!type) return false;
  if (["FORM_TITLE", "TEXT", "HR", "IMAGE", "FIELDS", "VIDEO"].includes(type)) {
    return false;
  }
  if (type === "TABLE") {
    return component.selectable !== false;
  }
  return true;
}

function adaptiveCardCanSubmit(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some((item) => adaptiveCardCanSubmit(item));
  const record = value as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : "";
  if (type === "Action.Submit" || type.startsWith("Input.")) return true;
  return Object.values(record).some((item) => adaptiveCardCanSubmit(item));
}

function renderedTalkMessagesNeedResponse(messages: SimulatorMessage[]) {
  return messages.some((message) => {
    if (message.template?.kind === "rich-form") {
      return getAidotRichFormComponents(message.template.rawJson).some(richFormComponentCanSubmit);
    }
    if (message.template?.kind === "adaptive-card") {
      return adaptiveCardCanSubmit(message.template.rawJson);
    }
    return Boolean(message.quickReplies?.length || message.template?.actions.length);
  });
}

function shouldWaitForTalkResponse(
  config: Extract<DialogFlowNode, { kind: "talk" }>["config"],
  messages: SimulatorMessage[] = [],
) {
  if (config.messageType === "dtmf") {
    return true;
  }
  if (config.responseType === "form-relay") {
    return renderedTalkMessagesNeedResponse(messages);
  }
  return (
    config.responseType === "single-select" ||
    config.responseType === "relay" ||
    config.responseType === "extract-entity"
  );
}

function talkResponseTypeSupportsIntentInterrupt(
  responseType: Extract<DialogFlowNode, { kind: "talk" }>["config"]["responseType"],
) {
  return (
    responseType === "single-select" ||
    responseType === "relay" ||
    responseType === "extract-entity" ||
    responseType === "form-relay"
  );
}

function fallbackRichFormSource(value: string) {
  const source = value.trim();
  if (!source) return "";
  if (getAidotRichFormComponents(source).length > 0) return source;
  const start = source.indexOf("{");
  const end = source.lastIndexOf("}");
  if (start >= 0 && end > start) {
    const objectSource = source.slice(start, end + 1);
    if (getAidotRichFormComponents(objectSource).length > 0) return objectSource;
  }
  const arrayStart = source.indexOf("[");
  const arrayEnd = source.lastIndexOf("]");
  if (arrayStart >= 0 && arrayEnd > arrayStart) {
    const arraySource = source.slice(arrayStart, arrayEnd + 1);
    if (getAidotRichFormComponents(arraySource).length > 0) return arraySource;
  }
  return "";
}

function collectRichFormSource(values: string[]) {
  for (const value of values) {
    const source = fallbackRichFormSource(value);
    if (source) return source;
  }
  return "";
}
function simulatorRichFormValue(component: AidotRichFormComponent, fallback = "선택") {
  return readAidotRichFormString(component, ["value", "message", "text", "title", "label", "name"], fallback);
}

function cleanRichFormText(value: unknown) {
  return String(value ?? "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p\s*>/gi, "\n")
    .replace(/<p[^>]*>/gi, "")
    .replace(/<\/div\s*>/gi, "\n")
    .replace(/<div[^>]*>/gi, "")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&amp;/gi, "&")
    .replace(/\n{3,}/g, "\n\n")
    .trim()
    .replace(/^\*([\s\S]+)\*$/, "$1");
}

function richFormTemplateText(value: string, row: Record<string, unknown>) {
  return value.replace(/\$\{\s*([^}]+?)\s*\}/g, (_token, key: string) => String(row[key.trim()] ?? ""));
}

function richFormParseJsonArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string") return [];
  const trimmed = value.trim();
  if (!trimmed.startsWith("[") && !trimmed.startsWith("{")) return [];
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed)) return parsed;
    if (parsed && typeof parsed === "object") {
      for (const key of ["dataValue", "rows", "items", "list", "value"]) {
        const nested = (parsed as Record<string, unknown>)[key];
        if (Array.isArray(nested)) return nested;
      }
    }
  } catch {
    return [];
  }
  return [];
}

function richFormRowsFromValue(value: unknown): Record<string, unknown>[] {
  const rawRows: unknown[] = Array.isArray(value) ? value : richFormParseJsonArray(value);
  return rawRows.map((item, index) => {
    if (item && typeof item === "object" && !Array.isArray(item)) return item as Record<string, unknown>;
    return { value: item, title: cleanRichFormText(item), key: String(index + 1) };
  });
}

function richFormDataRows(component: AidotRichFormComponent) {
  for (const key of ["dataValue", "rows", "items", "list", "value"]) {
    const rows = richFormRowsFromValue(component[key]);
    if (rows.length > 0) return rows;
  }
  return [] as Record<string, unknown>[];
}

function richFormDataKeys(component: AidotRichFormComponent, rows: Record<string, unknown>[]) {
  const keys = readAidotRichFormArray(component, ["dataKey", "columns", "headers"])
    .map((item) => String(item ?? "").trim())
    .filter(Boolean);
  return keys.length > 0 ? keys : Array.from(new Set(rows.flatMap((row) => Object.keys(row))));
}

function richFormDataTitles(component: AidotRichFormComponent, keys: string[]) {
  const titles = readAidotRichFormArray(component, ["dataTitle", "tableHeaders"])
    .map((item) => cleanRichFormText(item))
    .filter(Boolean);
  return keys.map((key, index) => titles[index] || key);
}

function richFormImageUrl(component: AidotRichFormComponent, row?: Record<string, unknown>) {
  const raw = readAidotRichFormString(component, ["url", "src", "imageUrl", "imageURL", "image_url", "imgUrl", "imgURL", "img_url", "image", "img", "thumbnail", "thumbnailUrl", "thumbnail_url", "thumbUrl", "thumb_url", "fileUrl", "fileURL", "file_url", "mediaUrl", "mediaURL", "media_url", "videoUrl", "videoURL", "video_url", "movieUrl", "movie_url", "path", "file", "link"]);
  return normalizeRichFormAssetUrl(row ? richFormTemplateText(raw, row) : raw);
}

function richFormMediaUrl(component: AidotRichFormComponent, row?: Record<string, unknown>) {
  const raw = readAidotRichFormString(component, ["url", "src", "mediaUrl", "mediaURL", "media_url", "videoUrl", "videoURL", "video_url", "movieUrl", "movie_url", "path", "file", "link"]);
  return normalizeSimulatorRichFormAssetUrl(row ? richFormTemplateText(raw, row) : raw, { proxyRemote: false });
}

function richFormIsLocalFilePath(value: string) {
  return isSimulatorRichFormLocalFilePath(value);
}

function normalizeRichFormAssetUrl(value: string) {
  return normalizeSimulatorRichFormAssetUrl(value);
}

function normalizeRichFormImageUrl(value: string) {
  const raw = value.trim();
  if (!raw) return "";
  if (
    /\/api\/v1\/richform\/(?:image|local-image)\?/i.test(raw) ||
    /\/files\/(?:temp|bot-images)\//i.test(raw)
  ) {
    return raw;
  }
  return normalizeRichFormAssetUrl(raw);
}

function normalizeRichFormVideoUrl(component: AidotRichFormComponent) {
  const normalized = richFormMediaUrl(component);
  if (!normalized) return "";

  let source = normalized;

  try {
    const wrapped = new URL(normalized);
    if (/\/api\/v1\/richform\/image$/i.test(wrapped.pathname)) {
      const originalUrl = wrapped.searchParams.get("url");
      if (originalUrl) source = originalUrl;
    }
  } catch {
    source = normalized;
  }

  try {
    const parsed = new URL(source);
    const host = parsed.hostname.toLowerCase();

    if (host === "youtu.be") {
      const videoId = parsed.pathname.replace(/^\/+/, "").split("/")[0];
      return videoId ? `https://www.youtube.com/embed/${videoId}` : source;
    }

    if (host.endsWith("youtube.com")) {
      if (parsed.pathname.startsWith("/embed/")) return source;
      const videoId = parsed.searchParams.get("v");
      if (videoId) return `https://www.youtube.com/embed/${videoId}`;
      const shortsMatch = parsed.pathname.match(/^\/shorts\/([^/?#]+)/i);
      if (shortsMatch) return `https://www.youtube.com/embed/${shortsMatch[1]}`;
    }

    if (host === "vimeo.com" || host.endsWith(".vimeo.com")) {
      if (parsed.pathname.startsWith("/video/")) return source;
      const videoId = parsed.pathname.replace(/^\/+/, "").split("/")[0];
      if (videoId) return `https://player.vimeo.com/video/${videoId}`;
    }
  } catch {
    return normalized;
  }

  return source;
}
function richFormLooksLikeImageUrl(value: unknown) {
  const text = cleanRichFormText(value);
  if (!text) return false;
  if (richFormIsLocalFilePath(text)) return true;
  if (/^(?:https?:)?\/\//i.test(text) || text.startsWith("data:")) return true;
  return /\.(?:png|jpe?g|gif|webp|svg)(?:[?#].*)?$/i.test(text) || /^(?:\/)?(?:assets|uploads|files)\//i.test(text);
}

function richFormRecordImageUrl(row: Record<string, unknown>) {
  const preferredKeys = ["url", "src", "imageUrl", "imageURL", "image_url", "imgUrl", "imgURL", "img_url", "image", "img", "thumbnail", "thumbnailUrl", "thumbnail_url", "thumbUrl", "thumb_url", "fileUrl", "fileURL", "file_url", "path", "file", "value2"];
  for (const key of preferredKeys) {
    if (richFormLooksLikeImageUrl(row[key])) return normalizeRichFormAssetUrl(cleanRichFormText(row[key]));
  }
  for (const value of Object.values(row)) {
    if (richFormLooksLikeImageUrl(value)) return normalizeRichFormAssetUrl(cleanRichFormText(value));
  }
  return "";
}

function richFormChoiceRows(component: AidotRichFormComponent) {
  const rows = richFormRowsFromValue(component.dataValue);
  if (rows.length > 0) return rows;
  return richFormRowsFromValue(component.value);
}

function richFormChoiceLabel(row: Record<string, unknown>, index: number, language: SupportedLanguage) {
  return cleanRichFormText(row.value || row.title || row.label || row.name || row.text || formatStudioRuntimeMessage(language, "항목 {index}", { index: index + 1 }));
}

function richFormBoolean(value: unknown) {
  if (typeof value === "boolean") return value;
  return String(value ?? "").trim().toLowerCase() === "true";
}

function richFormPositiveInteger(value: unknown, fallback: number) {
  const numberValue = Number(value);
  if (Number.isInteger(numberValue) && numberValue > 0) return numberValue;
  return fallback;
}

function richFormVisibleRows(component: AidotRichFormComponent, rows: Record<string, unknown>[]) {
  const count = richFormPositiveInteger(component.listCount, rows.length);
  return rows.slice(0, Math.min(rows.length, count));
}

function richFormIsSelectable(component: AidotRichFormComponent) {
  return richFormBoolean(component.selectable);
}

function richFormListForms(component: AidotRichFormComponent) {
  return readAidotRichFormArray(component, ["listForm"]);
}

function richFormSelectionClass(component: AidotRichFormComponent, className: string) {
  return richFormIsSelectable(component) ? `${className} is-selectable` : className;
}

function richFormSelectedDataValue(component: AidotRichFormComponent, row: Record<string, unknown>) {
  const selectedDataKey = readAidotRichFormString(component, ["selectedDataKey"]);
  if (selectedDataKey && row[selectedDataKey] != null) return row[selectedDataKey];
  return row;
}

function richFormPageSize(component: AidotRichFormComponent, total: number) {
  return Math.max(1, Math.min(total || 1, richFormPositiveInteger(component.listCount, total || 1)));
}

function RichFormPager({
  className,
  page,
  pageCount,
  onPageChange,
}: {
  className: string;
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
}) {
  if (pageCount <= 1) return null;
  return (
    <div className={className}>
      {Array.from({ length: pageCount }, (_item, index) => (
        <button key={index} type="button" className={index === page ? "is-active" : undefined} disabled={index === page} onClick={() => onPageChange(index)}>{index + 1}</button>
      ))}
    </div>
  );
}
function richFormButtonClassName(baseClass: string, item: AidotRichFormComponent) {
  const style = readAidotRichFormString(item, ["style"]).toLowerCase();
  const buttonType = readAidotRichFormString(item, ["type", "buttonType"]).toLowerCase();
  const classes = [baseClass];
  if (style.includes("primary")) classes.push("is-primary");
  if (style.includes("secondary")) classes.push("is-secondary");
  if (style.includes("wide")) classes.push("is-wide");
  if (buttonType.includes("popup") || readAidotRichFormString(item, ["popup", "popupTitle"])) classes.push("is-popup");
  if (buttonType.includes("reject")) classes.push("is-reject");
  return classes.join(" "  );
}

function richFormShouldShowImageCaption(value: string) {
  const text = value.trim();
  if (!text) return false;
  if (richFormIsLocalFilePath(text)) return false;
  if (/^(?:https?:)?\/\//i.test(text) || text.startsWith("data:")) return false;
  if (/^(?:\/)?(?:assets|uploads|files)\//i.test(text)) return false;
  if (/\.(?:png|jpe?g|gif|webp|svg)(?:[?#].*)?$/i.test(text)) return false;
  return true;
}
function SimulatorLocalizedText({ value }: { value: string }) {
  const { language: uiLanguage } = useI18n();
  return <>{getSimulatorLabel(uiLanguage, value)}</>;
}

function renderRichFormImageElement(
  url: string,
  alt: string,
  className = "",
  options?: { caption?: string; link?: string },
) {
  const rawAlt = cleanRichFormText(alt);
  const safeAlt = rawAlt || "Image";
  const caption = options?.caption ?? (rawAlt && richFormShouldShowImageCaption(rawAlt) ? rawAlt : "");
  const link = cleanRichFormText(options?.link);
  const imageUrl = normalizeRichFormImageUrl(url);
  const image = imageUrl ? (
    <>
      <img
        src={imageUrl}
        alt={safeAlt}
        referrerPolicy="no-referrer"
        onError={(event) => {
          event.currentTarget.style.display = "none";
          event.currentTarget.nextElementSibling?.classList.add("is-visible");
        }}
      />
      <a className="rich-form-image-fallback" href={imageUrl} target="_blank" rel="noreferrer"><SimulatorLocalizedText value="이미지 열기" /></a>
    </>
  ) : null;

  return (
    <span
      className={`${className} simulator-rich-form__image-frame${link && imageUrl ? " is-linked" : ""}`.trim()}
      role={link && imageUrl ? "link" : undefined}
      tabIndex={link && imageUrl ? 0 : undefined}
      onClick={link && imageUrl ? () => window.open(link, "_blank", "noopener,noreferrer") : undefined}
      onKeyDown={link && imageUrl ? (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          window.open(link, "_blank", "noopener,noreferrer");
        }
      } : undefined}
    >
      {image}
      {caption ? <span>{caption}</span> : null}
    </span>
  );
}
function richFormAddressItems(component: AidotRichFormComponent) {
  const items = simulatorObjectItems(component.address);
  return items.length > 0 ? items : [component];
}

function buildRichFormGroupPayload(parent: AidotRichFormComponent, values: Record<string, unknown>, fallback: string) {
  const response: Record<string, unknown> = {};
  Object.entries(values).forEach(([fieldName, fieldValue]) => {
    response[fieldName] = {
      value: fieldValue,
      title: fieldName,
      required: Boolean(fieldValue),
      validated: fieldValue !== "" && fieldValue != null,
      key: fieldName,
    };
  });
  return JSON.stringify({ webchatRichFormVersion: "1.0", response });
}

function renderSimulatorAddressComponent(component: AidotRichFormComponent, index: number, disabled: boolean, onAction: (value: string) => void, language: SupportedLanguage, formApi?: RichFormFormApi) {
  const items = richFormAddressItems(component);
  const key = `ADDRESS-${index}`;
  return (
    <div key={key} className="simulator-rich-form__address" data-rich-form-address={key}>
      {items.map((item, itemIndex) => {
        const itemRecord = item as AidotRichFormComponent;
        const itemTitle = cleanRichFormText(readAidotRichFormString(itemRecord, ["title", "label", "name"], itemIndex === 0 ? getStudioRuntimeMessage(language, "주소") : formatStudioRuntimeMessage(language, "주소 {index}", { index: itemIndex + 1 })));
        const itemKey = richFormFieldName(itemRecord, itemIndex === 0 ? "address" : `address${itemIndex + 1}`);
        const placeholder = readAidotRichFormString(itemRecord, ["hint", "placeholder"]);
        const value = readAidotRichFormString(itemRecord, ["value", "defaultValue"]);
        const isBase = readAidotRichFormString(itemRecord, ["type"]).toUpperCase().includes("BASE");
        return (
          <label key={`${key}-${itemIndex}`} className="simulator-rich-form__field simulator-rich-form__address-row">
            <span>{itemTitle}{itemRecord.required === true ? " *" : ""}</span>
            <div>
              <input data-address-key={itemKey} defaultValue={value} placeholder={placeholder} disabled={disabled || itemRecord.disabled === true} onChange={(event) => {
                if (!formApi) return;
                const root = event.currentTarget.closest('[data-rich-form-address]');
                const values: Record<string, unknown> = {};
                root?.querySelectorAll<HTMLInputElement>('input[data-address-key]').forEach((input) => {
                  values[input.dataset.addressKey || 'address'] = input.value.trim();
                });
                formApi.setValue(component, values, `address${index + 1}`);
              }} />
              {isBase ? <button type="button" disabled={disabled || itemRecord.disabled === true}>⌕</button> : null}
            </div>
          </label>
        );
      })}
    </div>
  );
}

function richFormDateInputValue(value: string) {
  const trimmed = value.trim();
  const match = trimmed.match(/^(\d{4})[-.\/년 ]+(\d{1,2})[-.\/월 ]+(\d{1,2})/);
  if (match) return `${match[1]}-${match[2].padStart(2, "0")}-${match[3].padStart(2, "0")}`;
  return /^\d{4}-\d{2}-\d{2}$/.test(trimmed) ? trimmed : "";
}

function richFormTimeInputValue(value: string) {
  const trimmed = value.trim();
  const ampm = trimmed.match(/^(\d{1,2}):(\d{2})\s*(AM|PM)$/i);
  if (ampm) {
    let hour = Number(ampm[1]);
    if (ampm[3].toUpperCase() === "PM" && hour < 12) hour += 12;
    if (ampm[3].toUpperCase() === "AM" && hour === 12) hour = 0;
    return `${String(hour).padStart(2, "0")}:${ampm[2]}`;
  }
  const match = trimmed.match(/^(\d{1,2}):(\d{2})/);
  if (match) return `${match[1].padStart(2, "0")}:${match[2]}`;
  return "";
}

function richFormFormatMonth(year: number, month: number, language: SupportedLanguage) {
  return formatStudioRuntimeMessage(language, "{year}년 {month}월", { year, month: month + 1 });
}

const RICH_FORM_WEEKDAY_LABELS: Record<SupportedLanguage, string[]> = {
  ko: ["일", "월", "화", "수", "목", "금", "토"],
  en: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
  "zh-CN": ["日", "一", "二", "三", "四", "五", "六"],
  ja: ["日", "月", "火", "水", "木", "金", "土"],
  vi: ["CN", "T2", "T3", "T4", "T5", "T6", "T7"],
  fr: ["dim.", "lun.", "mar.", "mer.", "jeu.", "ven.", "sam."],
  de: ["So.", "Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa."],
};

function richFormMonthDays(year: number, month: number) {
  const firstDay = new Date(year, month, 1).getDay();
  const lastDate = new Date(year, month + 1, 0).getDate();
  return [
    ...Array.from({ length: firstDay }, () => 0),
    ...Array.from({ length: lastDate }, (_item, index) => index + 1),
  ];
}

function SimulatorRichFormDateTimeView({
  component,
  index,
  disabled,
  onAction,
  type,
  formApi,
}: {
  component: AidotRichFormComponent;
  index: number;
  disabled: boolean;
  onAction: (value: string) => void;
  type: string;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const title = cleanRichFormText(readAidotRichFormString(component, ["title", "label", "name"]));
  const placeholder = readAidotRichFormString(component, ["placeholder", "hint"]);
  const defaultValue = readAidotRichFormString(component, ["value", "defaultValue"]);
  const isDate = type === "DATEPICKER";
  const initialValue = isDate ? richFormDateInputValue(defaultValue) : richFormTimeInputValue(defaultValue);
  const initialDate = initialValue ? new Date(initialValue) : new Date();
  const [value, setValue] = useState(initialValue);
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [month, setMonth] = useState({ year: initialDate.getFullYear(), month: initialDate.getMonth() });
  const [hour, setHour] = useState(() => initialValue ? initialValue.slice(0, 2) : "13");
  const [minute, setMinute] = useState(() => initialValue ? initialValue.slice(3, 5) : "00");
  const isDisabled = disabled || component.disabled === true;
  const commit = (nextValue: string) => {
    setValue(nextValue);
    formApi?.setValue(component, nextValue, `field${index + 1}`);
  };
  const chooseDay = (day: number) => {
    if (!day) return;
    const nextValue = `${month.year}-${String(month.month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    commit(nextValue);
    setOpen(false);
  };
  const chooseTime = (nextHour = hour, nextMinute = minute) => {
    setHour(nextHour);
    setMinute(nextMinute);
    commit(`${nextHour}:${nextMinute}`);
  };
  const openPicker = (event?: React.MouseEvent<HTMLElement>) => {
    event?.preventDefault();
    event?.stopPropagation();
    if (isDisabled) return;
    inputRef.current?.focus();
    setOpen((current) => !current);
  };
  const showPicker = () => {
    if (!isDisabled) setOpen(true);
  };
  return (
    <div key={`${type}-${index}`} className="simulator-rich-form__field simulator-rich-form__picker" data-rich-form-picker>
      <label>
        {title ? <span>{title}{component.required === true ? " *" : ""}</span> : null}
        <div>
          <input ref={inputRef} type={isDate ? "date" : "time"} step={isDate ? undefined : 60} value={value} placeholder={placeholder || (isDate ? "YYYY-MM-DD" : "HH:mm")} disabled={isDisabled} onChange={(event) => commit(event.currentTarget.value)} onClick={showPicker} />
          <button type="button" disabled={isDisabled} onClick={openPicker}>{isDate ? "□" : "◷"}</button>
        </div>
      </label>
      {open ? isDate ? (
        <div className="simulator-rich-form__picker-panel simulator-rich-form__calendar" role="dialog">
          <div className="simulator-rich-form__calendar-head">
            <button type="button" onClick={() => setMonth((current) => current.month === 0 ? { year: current.year - 1, month: 11 } : { ...current, month: current.month - 1 })}>‹</button>
            <strong>{richFormFormatMonth(month.year, month.month, uiLanguage)}</strong>
            <button type="button" onClick={() => setMonth((current) => current.month === 11 ? { year: current.year + 1, month: 0 } : { ...current, month: current.month + 1 })}>›</button>
          </div>
          <div className="simulator-rich-form__calendar-grid">
            {RICH_FORM_WEEKDAY_LABELS[uiLanguage].map((day) => <span key={day}>{day}</span>)}
            {richFormMonthDays(month.year, month.month).map((day, dayIndex) => (
              <button key={`${dayIndex}-${day}`} type="button" disabled={day === 0 || isDisabled} className={value === `${month.year}-${String(month.month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}` ? "is-selected" : undefined} onClick={() => chooseDay(day)}>{day || ""}</button>
            ))}
          </div>
        </div>
      ) : (
        <div className="simulator-rich-form__picker-panel simulator-rich-form__time-panel" role="dialog">
          <strong>{value || getSimulatorLabel(uiLanguage, "시간 선택")}</strong>
          <div>
            <select value={hour} disabled={isDisabled} onChange={(event) => chooseTime(event.currentTarget.value, minute)}>
              {Array.from({ length: 24 }, (_item, itemIndex) => String(itemIndex).padStart(2, "0")).map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <span>:</span>
            <select value={minute} disabled={isDisabled} onChange={(event) => chooseTime(hour, event.currentTarget.value)}>
              {Array.from({ length: 12 }, (_item, itemIndex) => String(itemIndex * 5).padStart(2, "0")).map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </div>
          <button type="button" disabled={isDisabled} onClick={() => { chooseTime(hour, minute); setOpen(false); }}>{getSimulatorLabel(uiLanguage, "확인")}</button>
        </div>
      ) : null}
    </div>
  );
}

function renderSimulatorDateTimeComponent(component: AidotRichFormComponent, index: number, disabled: boolean, onAction: (value: string) => void, type: string, formApi?: RichFormFormApi) {
  return <SimulatorRichFormDateTimeView key={`${type}-${index}`} component={component} index={index} disabled={disabled} onAction={onAction} type={type} formApi={formApi} />;
}

function richFormLooksLikeHtml(value: string) {
  return /<\/?(?:p|br|div|span|strong|b|em|i|u|ul|ol|li|a|img|table|thead|tbody|tr|th|td)\b/i.test(value);
}

function sanitizeRichFormHtml(value: string) {
  return value
    .replace(/<script[\s\S]*?>[\s\S]*?<\/script>/gi, "")
    .replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "")
    .replace(/\s(href|src)\s*=\s*(["'])\s*javascript:[\s\S]*?\2/gi, "")
    .replace(/url\s*\(\s*("|')?\s*javascript:[\s\S]*?\)/gi, "");
}

function renderRichFormLines(value: unknown, className?: string) {
  const raw = String(value ?? "");
  if (richFormLooksLikeHtml(raw)) {
    return <div className={className} dangerouslySetInnerHTML={{ __html: sanitizeRichFormHtml(raw) }} />;
  }
  const text = cleanRichFormText(value);
  if (!text) return null;
  return <p className={className}>{text}</p>;
}

function renderRichFormImageFromRecord(record: Record<string, unknown>, title = "") {
  const url = richFormImageUrl(record as AidotRichFormComponent, record) || richFormRecordImageUrl(record);
  const alt = cleanRichFormText(record.alt || record.title || title);
  return renderRichFormImageElement(url, alt);
}

type RichFormResponseEntry = {
  value: unknown;
  title: string;
  required?: boolean;
  validated: boolean;
  key: string;
};

type RichFormResponseDraft = Record<string, unknown>;


type RichFormSubmitOptions = { buttonValue?: string; validate?: boolean; buttonOnly?: boolean; includeField?: boolean };

function richFormValuePresent(value: unknown): boolean {
  if (value == null) return false;
  if (typeof value === "string") return value.trim().length > 0;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value === "boolean") return true;
  if (Array.isArray(value)) return value.some((item) => richFormValuePresent(item));
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if ("value" in record && Object.keys(record).some((key) => ["title", "required", "validated", "key"].includes(key))) {
      return richFormValuePresent(record.value);
    }
    return Object.values(record).some((item) => richFormValuePresent(item));
  }
  return true;
}

function richFormValidationRecord(component: AidotRichFormComponent) {
  const validation = component.validation || component.validator || component.validations;
  if (validation && typeof validation === "object" && !Array.isArray(validation)) return validation as AidotRichFormComponent;
  return {} as AidotRichFormComponent;
}

function richFormDraftEntryValue(entry: unknown): unknown {
  if (entry && typeof entry === "object" && !Array.isArray(entry) && "value" in entry) {
    return (entry as RichFormResponseEntry).value;
  }
  return entry;
}

function richFormDraftValue(draft: RichFormResponseDraft, fieldName: string): unknown {
  if (fieldName in draft) return richFormDraftEntryValue(draft[fieldName]);
  for (const value of Object.values(draft)) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const nested = richFormDraftValue(value as RichFormResponseDraft, fieldName);
      if (nested !== undefined) return nested;
    }
  }
  return undefined;
}

function richFormDefaultValue(component: AidotRichFormComponent): unknown {
  const type = readAidotRichFormString(component, ["type"]).toUpperCase();
  if (component.defaultValue != null) return component.defaultValue;
  if (component.selectedValue != null) return component.selectedValue;
  if (["CHECK", "CHECKBOX", "RADIO", "RADIOBUTTON", "COMBO", "COMBOBOX"].includes(type)) {
    const selectedIndexes = readAidotRichFormArray(component, ["selectedIndex"])
      .map((item) => Number(item))
      .filter((item) => Number.isFinite(item))
      .map((item) => (item > 0 ? item - 1 : item));
    if (selectedIndexes.length === 0) return undefined;
    const rows = richFormChoiceRows(component);
    const selectedRows = selectedIndexes.map((item) => rows[item]).filter(Boolean);
    if (selectedRows.length === 0) return undefined;
    return type.includes("CHECK") ? selectedRows : selectedRows[0];
  }
  if (["TABLE", "CAROUSEL", "IMAGE_LIST", "KAKAOMAP", "INPUT_POPUP", "INPUTPOPUP"].includes(type)) {
    const selectedIndexes = readAidotRichFormArray(component, ["selectedIndex"])
      .map((item) => Number(item))
      .filter((item) => Number.isFinite(item))
      .map((item) => (item > 0 ? item - 1 : item));
    if (selectedIndexes.length === 0) return undefined;
    const rows = richFormDataRows(component);
    const selectedRows = selectedIndexes.map((item) => rows[item]).filter(Boolean);
    if (selectedRows.length === 0) return undefined;
    return selectedRows.length === 1 ? richFormSelectedDataValue(component, selectedRows[0]) : selectedRows.map((row) => richFormSelectedDataValue(component, row));
  }
  if (type === "ADDRESS") return undefined;
  if (component.value != null) return component.value;
  return undefined;
}

function richFormConditionValue(token: string, draft: RichFormResponseDraft) {
  const trimmed = token.trim();
  const variableMatch = trimmed.match(/^\$\{\s*([^}]+?)\s*\}$/);
  if (variableMatch) return richFormDraftValue(draft, variableMatch[1].trim());
  if ((trimmed.startsWith('"') && trimmed.endsWith('"')) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) return trimmed.slice(1, -1);
  if (/^(true|false)$/i.test(trimmed)) return trimmed.toLowerCase() === "true";
  if (/^null$/i.test(trimmed)) return null;
  const numeric = Number(trimmed);
  if (trimmed && Number.isFinite(numeric)) return numeric;
  return trimmed;
}

function richFormCompareCondition(left: unknown, operator: string, right: unknown) {
  if ([">", ">=", "<", "<="].includes(operator)) {
    const leftNumber = Number(left);
    const rightNumber = Number(right);
    if (!Number.isFinite(leftNumber) || !Number.isFinite(rightNumber)) return false;
    if (operator === ">") return leftNumber > rightNumber;
    if (operator === ">=") return leftNumber >= rightNumber;
    if (operator === "<") return leftNumber < rightNumber;
    return leftNumber <= rightNumber;
  }
  const leftText = typeof left === "object" ? JSON.stringify(left) : String(left ?? "");
  const rightText = typeof right === "object" ? JSON.stringify(right) : String(right ?? "");
  if (operator === "==" || operator === "===") return leftText === rightText;
  return leftText !== rightText;
}

function richFormEvaluateConditionExpression(expression: string, draft: RichFormResponseDraft): boolean {
  const source = expression.trim();
  if (!source) return true;
  const orParts = source.split(/\s*\|\|\s*/);
  if (orParts.length > 1) return orParts.some((part) => richFormEvaluateConditionExpression(part, draft));
  const andParts = source.split(/\s*&&\s*/);
  if (andParts.length > 1) return andParts.every((part) => richFormEvaluateConditionExpression(part, draft));
  const comparison = source.match(/^(.+?)\s*(===|!==|==|!=|>=|<=|>|<)\s*(.+)$/);
  if (comparison) {
    return richFormCompareCondition(richFormConditionValue(comparison[1], draft), comparison[2], richFormConditionValue(comparison[3], draft));
  }
  return richFormValuePresent(richFormConditionValue(source, draft));
}

function richFormConditionExpression(component: AidotRichFormComponent) {
  const validation = richFormValidationRecord(component);
  return readAidotRichFormString(validation, ["condition", "expression", "rule", "value"]) || readAidotRichFormString(component, ["validationCondition", "condition"]);
}

function richFormValidationMessage(component: AidotRichFormComponent, fallback: string, language: SupportedLanguage) {
  const validation = richFormValidationRecord(component);
  const message = validation.message;
  if (message && typeof message === "object" && !Array.isArray(message)) {
    const invalid = readAidotRichFormString(message as AidotRichFormComponent, ["invalid", "error", "fail", "failure"]);
    if (invalid) return cleanRichFormText(invalid);
  }
  return cleanRichFormText(
    readAidotRichFormString(validation, ["invalidMessage", "errorMessage", "messageInvalid", "failMessage"]) ||
    readAidotRichFormString(component, ["invalidMessage", "errorMessage", "messageInvalid"])
  ) || formatStudioRuntimeMessage(language, "{field} 값을 확인해 주세요.", { field: fallback });
}

function richFormValidationRequired(component: AidotRichFormComponent) {
  const validation = richFormValidationRecord(component);
  return richFormBoolean(component.required ?? validation.required ?? component.requiredYn ?? component.mandatory);
}

function richFormValidationPattern(component: AidotRichFormComponent) {
  const validation = richFormValidationRecord(component);
  return readAidotRichFormString(validation, ["regex", "regexp", "regExp", "pattern"]) || readAidotRichFormString(component, ["regex", "regexp", "regExp", "pattern"]);
}

function richFormNumberRule(component: AidotRichFormComponent, keys: string[]) {
  const validation = richFormValidationRecord(component);
  for (const source of [validation, component]) {
    for (const key of keys) {
      const value = Number(source[key]);
      if (Number.isFinite(value)) return value;
    }
  }
  return null;
}

function richFormPatternOk(pattern: string, value: unknown) {
  if (!pattern) return true;
  try {
    return new RegExp(pattern).test(String(value ?? ""));
  } catch {
    return true;
  }
}
function richFormEntryState(component: AidotRichFormComponent, value: unknown, fieldName: string, draft: RichFormResponseDraft) {
  const nextDraft = { ...draft, [fieldName]: { value } };
  const required = richFormValidationRequired(component);
  const requiredOk = !required || richFormValuePresent(value);
  const condition = richFormConditionExpression(component);
  const conditionOk = condition ? richFormEvaluateConditionExpression(condition, nextDraft) : true;
  const textValue = String(value ?? "");
  const minLength = richFormNumberRule(component, ["minLength", "minlength", "minLen"]);
  const maxLength = richFormNumberRule(component, ["maxLength", "maxlength", "maxLen"]);
  const minValue = richFormNumberRule(component, ["min", "minimum", "minValue"]);
  const maxValue = richFormNumberRule(component, ["max", "maximum", "maxValue"]);
  const numberValue = Number(value);
  const lengthOk = (minLength == null || textValue.length >= minLength) && (maxLength == null || textValue.length <= maxLength);
  const rangeOk = (!Number.isFinite(numberValue) || ((minValue == null || numberValue >= minValue) && (maxValue == null || numberValue <= maxValue)));
  const patternOk = richFormPatternOk(richFormValidationPattern(component), value);
  return { required, requiredOk, validated: requiredOk && conditionOk && lengthOk && rangeOk && patternOk };
}

function richFormIsValidationTarget(component: AidotRichFormComponent) {
  const type = readAidotRichFormString(component, ["type"]).toUpperCase();
  return richFormValidationRequired(component) || Boolean(richFormConditionExpression(component)) || Boolean(richFormValidationPattern(component)) || [
    "INPUT", "TEXTAREA", "ADDRESS", "DATEPICKER", "TIMEPICKER", "CHECK", "CHECKBOX", "RADIO", "RADIOBUTTON",
    "COMBO", "COMBOBOX", "TOGGLEBUTTON", "INPUT_POPUP", "INPUTPOPUP", "STAR_RATE", "STARRATE", "STAR",
  ].includes(type);
}

function richFormChildComponents(component: AidotRichFormComponent): AidotRichFormComponent[] {
  const children: AidotRichFormComponent[] = [];
  ["content", "contents", "children", "components", "items", "formMessage", "formMessages"].forEach((key) => {
    readAidotRichFormArray(component, [key]).forEach((item) => {
      if (item && typeof item === "object") children.push(item as AidotRichFormComponent);
    });
  });
  simulatorRichFormItems(component).forEach((item) => {
    if (item && typeof item === "object") {
      const record = item as AidotRichFormComponent;
      children.push(record);
      const popup = richFormPopupComponent(record);
      if (popup !== record) children.push(popup);
    }
  });
  simulatorObjectItems(component.address).forEach((address) => children.push(address as AidotRichFormComponent));
  simulatorObjectItems(component.tab || component.tabs).forEach((tab) => {
    simulatorObjectItems(tab.content || tab.contents || tab.items || tab.children).forEach((child) => children.push(child as AidotRichFormComponent));
  });
  return children;
}

function richFormValidateDraftComponents(components: AidotRichFormComponent[], draft: RichFormResponseDraft, language: SupportedLanguage): string | null {
  const stack = [...components];
  while (stack.length > 0) {
    const component = stack.shift() as AidotRichFormComponent;
    stack.push(...richFormChildComponents(component));
    if (!richFormIsValidationTarget(component)) continue;
    const fallback = cleanRichFormText(readAidotRichFormString(component, ["title", "label", "name", "key"], getSimulatorLabel(language, "입력값")));
    const fieldName = richFormFieldName(component, fallback);
    const value = richFormDraftValue(draft, fieldName) ?? richFormDefaultValue(component);
    const state = richFormEntryState(component, value, fieldName, draft);
    if (!state.requiredOk) return formatStudioRuntimeMessage(language, "{field}은(는) 필수입니다.", { field: fallback });
    if (!state.validated) return richFormValidationMessage(component, fallback, language);
  }
  return null;
}

type RichFormResponsePathItem = { key: string };

type RichFormFormApi = {
  setValue: (component: AidotRichFormComponent, value: unknown, fallback: string) => void;
  submit: (component: AidotRichFormComponent, value: unknown, fallback: string, options?: RichFormSubmitOptions) => void;
  setNestedValue: (path: RichFormResponsePathItem[], component: AidotRichFormComponent, value: unknown, fallback: string) => void;
  submitNested: (path: RichFormResponsePathItem[], component: AidotRichFormComponent, value: unknown, fallback: string, options?: RichFormSubmitOptions) => void;
};

function richFormResponseEntry(component: AidotRichFormComponent, value: unknown, fallback: string, draft: RichFormResponseDraft = {}): [string, RichFormResponseEntry] {
  const fieldName = richFormFieldName(component, fallback);
  const title = cleanRichFormText(readAidotRichFormString(component, ["title", "label", "name", "text"], fieldName));
  const state = richFormEntryState(component, value, fieldName, draft);
  return [fieldName, {
    value,
    title,
    ...(state.required ? { required: state.requiredOk } : {}),
    validated: state.validated,
    key: fieldName,
  }];
}

function richFormPayloadFromDraft(draft: RichFormResponseDraft, options: RichFormSubmitOptions = {}) {
  const response: Record<string, unknown> = options.buttonOnly ? {} : { ...draft };
  if (options.buttonValue != null) response.buttonValue = options.buttonValue;
  return JSON.stringify({ webchatRichFormVersion: "1.0", response });
}

function richFormInitialDraftFromComponents(components: AidotRichFormComponent[]) {
  const draft: RichFormResponseDraft = {};
  const stack = components.map((component, index) => ({ component, fallback: `field${index + 1}` }));
  while (stack.length > 0) {
    const { component, fallback } = stack.shift() as { component: AidotRichFormComponent; fallback: string };
    if (richFormIsValidationTarget(component)) {
      const value = richFormDefaultValue(component);
      if (value !== undefined) {
        const [fieldName, entry] = richFormResponseEntry(component, value, fallback, draft);
        draft[fieldName] = entry;
      }
    }
    richFormChildComponents(component).forEach((child, childIndex) => {
      stack.push({ component: child, fallback: `${fallback}_${childIndex + 1}` });
    });
  }
  return draft;
}

function richFormNestedDraft(
  draft: RichFormResponseDraft,
  path: RichFormResponsePathItem[],
  fieldName: string,
  entry: RichFormResponseEntry,
) {
  const next: RichFormResponseDraft = { ...draft };
  let cursor: Record<string, unknown> = next;
  path.filter((item) => item.key).forEach((item) => {
    const existing = cursor[item.key];
    const child = existing && typeof existing === "object" && !Array.isArray(existing)
      ? { ...(existing as Record<string, unknown>) }
      : {};
    child.key = item.key;
    cursor[item.key] = child;
    cursor = child;
  });
  cursor[fieldName] = entry;
  return next;
}

function simulatorRichFormResultPayload(
  component: AidotRichFormComponent,
  value: unknown,
  fallback: string,
  options: RichFormSubmitOptions = {},
) {
  const [fieldName, entry] = richFormResponseEntry(component, value, fallback);
  return richFormPayloadFromDraft({ [fieldName]: entry }, options);
}

function simulatorRichFormActionPayload(
  parent: AidotRichFormComponent,
  action: AidotRichFormComponent,
  fallback: string,
  options: { buttonValue?: boolean } = {},
) {
  const value = simulatorRichFormValue(action);
  const fieldSource = readAidotRichFormString(action, ["key", "name", "id", "variableName", "field"]) ? action : parent;
  return simulatorRichFormResultPayload(fieldSource, value, fallback, {
    buttonValue: options.buttonValue ? value : undefined,
  });
}

function simulatorRichFormItems(component: AidotRichFormComponent) {
  for (const key of ["items", "options", "button", "buttons", "rows", "dataValue", "list", "listForm", "contents", "tabs", "tab", "content"]) {
    const value = component[key];
    if (Array.isArray(value)) return value;
  }
  return [] as unknown[];
}

function simulatorObjectItems(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [];
}

function renderSimulatorDataValueForm(value: unknown, row: Record<string, unknown>, fallback: unknown) {
  const fallbackText = cleanRichFormText(fallback);
  const fallbackImageUrl = richFormLooksLikeImageUrl(fallback) ? normalizeRichFormAssetUrl(fallbackText) : "";

  if (Array.isArray(value)) {
    return <div className="simulator-rich-form__inline-form">{value.map((item, index) => <Fragment key={`inline-${index}`}>{renderSimulatorDataValueForm(item, row, fallback ?? index)}</Fragment>)}</div>;
  }
  if (value && typeof value === "object") {
    const form = value as AidotRichFormComponent;
    const type = readAidotRichFormString(form, ["type"]).toUpperCase();
    if (type === "IMAGE") {
      const rawTemplateUrl = readAidotRichFormString(form, ["url", "src", "imageUrl", "imageURL", "image_url", "imgUrl", "imgURL", "img_url", "thumbnail", "thumbnailUrl", "thumbnail_url", "fileUrl", "fileURL", "file_url", "path", "file", "link"]);
      const resolvedTemplateUrl = cleanRichFormText(richFormTemplateText(rawTemplateUrl, row));
      const templateUrl = richFormLooksLikeImageUrl(resolvedTemplateUrl) ? normalizeRichFormAssetUrl(resolvedTemplateUrl) : "";
      const url = rawTemplateUrl.includes("${") ? (templateUrl || fallbackImageUrl) : (fallbackImageUrl || templateUrl);
      const explicitAlt = readAidotRichFormString(form, ["alt", "title", "text"]);
      const alt = explicitAlt ? richFormTemplateText(explicitAlt, row) : "";
      return renderRichFormImageElement(url, cleanRichFormText(alt));
    }
    if (type === "HTML") {
      const html = richFormTemplateText(readAidotRichFormString(form, ["html", "text", "value"], fallbackText), row);
      return renderRichFormLines(html);
    }
    const text = richFormTemplateText(readAidotRichFormString(form, ["text", "title", "value"], fallbackText), row);
    return renderRichFormLines(text);
  }

  const type = String(value || "").trim().toUpperCase();
  if (type === "IMAGE" || fallbackImageUrl) return renderRichFormImageElement(fallbackImageUrl || normalizeRichFormAssetUrl(fallbackText), type === "IMAGE" ? "" : fallbackText);
  if (type === "HTML") return renderRichFormLines(fallbackText);
  return <span>{fallbackText}</span>;
}

function renderSimulatorRichFormTable(
  component: AidotRichFormComponent,
  disabled: boolean,
  onAction: (value: string) => void,
  formApi?: RichFormFormApi,
) {
  return <SimulatorRichFormTableView component={component} disabled={disabled} onAction={onAction} formApi={formApi} />;
}

function SimulatorRichFormTableView({
  component,
  disabled,
  onAction,
  formApi,
}: {
  component: AidotRichFormComponent;
  disabled: boolean;
  onAction: (value: string) => void;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const rows = richFormDataRows(component);
  const pageSize = richFormPageSize(component, rows.length);
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const visibleRows = rows.slice(safePage * pageSize, safePage * pageSize + pageSize);
  const keys = richFormDataKeys(component, rows).slice(0, 8);
  const titles = richFormDataTitles(component, keys);
  const forms = readAidotRichFormArray(component, ["dataValueForm"]);
  const selectable = richFormIsSelectable(component);
  if (keys.length === 0) return <p className="simulator-rich-form__muted">{getSimulatorLabel(uiLanguage, "표시할 항목이 없습니다.")}</p>;
  return (
    <div className="simulator-rich-form__table">
      <table>
        <thead><tr>{titles.map((column) => <th key={column}>{column}</th>)}</tr></thead>
        <tbody>
          {visibleRows.length > 0 ? visibleRows.map((row, rowIndex) => {
            const absoluteIndex = safePage * pageSize + rowIndex;
            return (
              <tr
                key={absoluteIndex}
                className={selectable ? "is-selectable" : undefined}
                onClick={selectable && !disabled ? () => (formApi ? formApi.setValue(component, richFormSelectedDataValue(component, row), `row${absoluteIndex + 1}`) : onAction(simulatorRichFormResultPayload(component, richFormSelectedDataValue(component, row), `row${absoluteIndex + 1}`))) : undefined}
              >
                {keys.map((column, columnIndex) => <td key={column}>{renderSimulatorDataValueForm(forms[columnIndex], row, row[column])}</td>)}
              </tr>
            );
          }) : <tr><td colSpan={Math.max(keys.length, 1)}>{getSimulatorLabel(uiLanguage, "표시할 항목이 없습니다.")}</td></tr>}
        </tbody>
      </table>
      <RichFormPager className="simulator-rich-form__pager" page={safePage} pageCount={pageCount} onPageChange={setPage} />
    </div>
  );
}

function SimulatorRichFormCardsView({
  component,
  title,
  text,
  type,
  disabled,
  onAction,
  formApi,
}: {
  component: AidotRichFormComponent;
  title: string;
  text: string;
  type: string;
  disabled: boolean;
  onAction: (value: string) => void;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const rootRows = richFormDataRows(component);
  const childRows = simulatorObjectItems(component.multiLevelDataValue || component.multilevelDataValue);
  const forms = richFormListForms(component);
  const selectable = richFormIsSelectable(component);
  const isImageList = type === "IMAGE_LIST";
  const multiLevel = type === "CAROUSEL" ? richFormPositiveInteger(component.multiLevel ?? component.multilevel, 1) : 1;
  const [page, setPage] = useState(0);
  const [path, setPath] = useState<Record<string, unknown>[]>([]);
  const parentId = path.length > 0 ? String(path[path.length - 1].id ?? "") : "root";
  const currentRows = path.length > 0 && childRows.length > 0
    ? childRows.filter((row) => String(row.pid ?? "") === parentId)
    : rootRows;
  const rows = currentRows.length > 0 ? currentRows : rootRows;
  const pageSize = richFormPageSize(component, rows.length);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const visibleRows = rows.slice(safePage * pageSize, safePage * pageSize + pageSize);
  const canGoBack = path.length > 0;
  return (
    <div className={`simulator-rich-form__cards ${isImageList ? "simulator-rich-form__cards--image-list" : ""}`}>
      {title ? <strong>{title}</strong> : null}{renderRichFormLines(text)}
      {canGoBack ? <button type="button" className="simulator-rich-form__back-button" onClick={() => { setPath((current) => current.slice(0, -1)); setPage(0); }}>{getSimulatorLabel(uiLanguage, "이전으로")}</button> : null}
      <div>{visibleRows.map((row, rowIndex) => {
        const absoluteIndex = safePage * pageSize + rowIndex;
        const rowId = String(row.id ?? "");
        const childCandidates = rowId ? childRows.filter((child) => String(child.pid ?? "") === rowId) : [];
        const canDrillDown = type === "CAROUSEL" && multiLevel > path.length + 1 && childCandidates.length > 0;
        const url = normalizeRichFormAssetUrl(richFormRecordImageUrl(row));
        const link = cleanRichFormText(row.link || row.urlLink || row.href);
        const label = cleanRichFormText(row.alt || row.title || row.label || row.value || row.name || `${getSimulatorLabel(uiLanguage, "항목")} ${absoluteIndex + 1}`);
        const canOpenLink = isImageList && Boolean(link);
        const content = forms.length > 0
          ? <>{isImageList && url ? renderRichFormImageElement(url, label, "", { link }) : null}<div className="simulator-rich-form__card-form">{forms.map((form, formIndex) => <Fragment key={`card-form-${absoluteIndex}-${formIndex}`}>{renderSimulatorDataValueForm(form, row, label || formIndex)}</Fragment>)}</div></>
          : <>{renderRichFormImageElement(url, label, "", { link })}<strong>{label}</strong>{renderRichFormLines(row.text || row.description)}</>;
        return (
          <article key={`${type}-${parentId}-${absoluteIndex}`} className={selectable || canDrillDown || canOpenLink ? "is-selectable" : undefined}>
            <button
              type="button"
              disabled={disabled || component.disabled === true || (!selectable && !canDrillDown && !canOpenLink)}
              onClick={() => {
                if (canOpenLink) {
                  window.open(link, "_blank", "noopener,noreferrer");
                  return;
                }
                if (canDrillDown) {
                  setPath((current) => [...current, row]);
                  setPage(0);
                  return;
                }
                if (formApi) { formApi.setValue(component, richFormSelectedDataValue(component, row), `item${absoluteIndex + 1}`); return; }
                onAction(simulatorRichFormResultPayload(component, richFormSelectedDataValue(component, row), `item${absoluteIndex + 1}`));
              }}
            >{content}</button>
          </article>
        );
      })}</div>
      <RichFormPager className="simulator-rich-form__pager" page={safePage} pageCount={pageCount} onPageChange={setPage} />
    </div>
  );
}

function SimulatorRichFormMapView({
  component,
  title,
  text,
  disabled,
  onAction,
  formApi,
}: {
  component: AidotRichFormComponent;
  title: string;
  text: string;
  disabled: boolean;
  onAction: (value: string) => void;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const rows = richFormDataRows(component);
  const pageSize = richFormPageSize(component, rows.length);
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const visibleRows = rows.slice(safePage * pageSize, safePage * pageSize + pageSize);
  const selectable = richFormIsSelectable(component);
  return (
    <div className="simulator-rich-form__map">
      <strong>{title || getSimulatorLabel(uiLanguage, "카카오맵")}</strong>{renderRichFormLines(text)}
      <div className="simulator-rich-form__map-box"><span>{getSimulatorLabel(uiLanguage, "지도")}</span><span>{getSimulatorLabel(uiLanguage, "스카이뷰")}</span><i /></div>
      {visibleRows.map((row, rowIndex) => {
        const absoluteIndex = safePage * pageSize + rowIndex;
        return (
          <button key={absoluteIndex} className={selectable ? "is-selectable" : undefined} type="button" disabled={disabled || component.disabled === true || !selectable} onClick={() => (formApi ? formApi.setValue(component, richFormSelectedDataValue(component, row), `map${absoluteIndex + 1}`) : onAction(simulatorRichFormResultPayload(component, richFormSelectedDataValue(component, row), `map${absoluteIndex + 1}`)))}>
            <strong>{cleanRichFormText(row.branchName || row.branchPrdNm || row.title || `${getSimulatorLabel(uiLanguage, "위치")} ${absoluteIndex + 1}`)}</strong>
            {renderRichFormLines(`${row.distance ? `${row.distance}km
` : ""}${row.telNo || ""}
${row.addr1 || ""}
${row.addr2 || ""}`)}
          </button>
        );
      })}
      <RichFormPager className="simulator-rich-form__pager" page={safePage} pageCount={pageCount} onPageChange={setPage} />
    </div>
  );
}

function richFormPopupComponent(action: AidotRichFormComponent): AidotRichFormComponent {
  const popup = action.popup || action.popUp || action.Popup || action.POPUP || action.popupContent || action.popUpContent || action.popupMessage;
  if (popup && typeof popup === "object" && !Array.isArray(popup)) return popup as AidotRichFormComponent;
  return action;
}

function richFormPopupContentComponents(component: AidotRichFormComponent): AidotRichFormComponent[] {
  return readAidotRichFormArray(component, ["content", "contents", "components", "items"])
    .map((item) => (item && typeof item === "object" && !Array.isArray(item) ? item as AidotRichFormComponent : ({ type: "TEXT", text: String(item ?? "") } as AidotRichFormComponent)));
}

function richFormPopupDataComponent(component: AidotRichFormComponent): AidotRichFormComponent {
  const content = richFormPopupContentComponents(component);
  const candidate = content.find((item) => {
    const type = readAidotRichFormString(item, ["type"]).toUpperCase();
    return type === "TABLE" || type === "CAROUSEL" || type === "KAKAOMAP";
  });
  return candidate || component;
}

function SimulatorRichFormInputPopupView({
  component,
  title,
  disabled,
  formApi,
}: {
  component: AidotRichFormComponent;
  title: string;
  disabled: boolean;
  onAction: (value: string) => void;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const popupComponent = richFormPopupComponent(component);
  const dataComponent = richFormPopupDataComponent(popupComponent);
  const rows = richFormDataRows(dataComponent);
  const pageSize = richFormPageSize(dataComponent, rows.length);
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const visibleRows = rows.slice(safePage * pageSize, safePage * pageSize + pageSize);
  const keys = richFormDataKeys(dataComponent, rows).slice(0, 8);
  const titles = richFormDataTitles(dataComponent, keys);
  const forms = readAidotRichFormArray(dataComponent, ["dataValueForm"]);
  const selectedDataKey = readAidotRichFormString(component, ["selectedDataKey"]) || readAidotRichFormString(dataComponent, ["selectedDataKey"]);
  const placeholder = readAidotRichFormString(component, ["hint", "placeholder"], title || getSimulatorLabel(uiLanguage, "선택하세요"));
  const selectedValue = selected
    ? cleanRichFormText(selectedDataKey ? selected[selectedDataKey] : richFormSelectedDataValue(dataComponent, selected))
    : readAidotRichFormString(component, ["value", "defaultValue"]);
  const isDisabled = disabled || component.disabled === true;

  function selectRow(row: Record<string, unknown>) {
    setSelected(row);
    setOpen(false);
    if (formApi) formApi.setValue(component, richFormSelectedDataValue(dataComponent, row), "popup");
  }

  return (
    <div className="simulator-rich-form__input-popup">
      <label>
        {title ? <span>{title}{component.required === true ? " *" : ""}</span> : null}
        <div className="simulator-rich-form__input-popup-field">
          <input readOnly placeholder={placeholder} value={selectedValue} disabled={isDisabled} />
          <button type="button" disabled={isDisabled} aria-label={getSimulatorLabel(uiLanguage, "팝업 열기")} onClick={() => setOpen(true)}>⌕</button>
        </div>
      </label>
      {open ? (
        <div className="simulator-rich-form__modal" role="dialog" aria-modal="true">
          <div className="simulator-rich-form__modal-panel">
            <button type="button" className="simulator-rich-form__modal-close" aria-label={getSimulatorLabel(uiLanguage, "닫기")} onClick={() => setOpen(false)}>×</button>
            <strong>{readAidotRichFormString(popupComponent, ["popupTitle", "title", "popupName"], getSimulatorLabel(uiLanguage, "팝업타이틀"))}</strong>
            {renderRichFormLines(readAidotRichFormString(popupComponent, ["popupText", "description"]))}
            {rows.length > 0 ? (
              <div className="simulator-rich-form__table simulator-rich-form__table--popup">
                <table>
                  <thead><tr>{titles.map((column) => <th key={column}>{column}</th>)}</tr></thead>
                  <tbody>{visibleRows.map((row, rowIndex) => {
                    const absoluteIndex = safePage * pageSize + rowIndex;
                    const isSelected = selected === row;
                    return (
                      <tr key={absoluteIndex} className={isSelected ? "is-selected" : undefined} onClick={() => selectRow(row)}>
                        {keys.map((fieldKey, fieldIndex) => <td key={fieldKey}>{renderSimulatorDataValueForm(forms[fieldIndex], row, row[fieldKey] ?? (fieldIndex === 0 ? `${getSimulatorLabel(uiLanguage, "항목")} ${absoluteIndex + 1}` : "-"))}</td>)}
                      </tr>
                    );
                  })}</tbody>
                </table>
                <RichFormPager className="simulator-rich-form__pager" page={safePage} pageCount={pageCount} onPageChange={setPage} />
              </div>
            ) : <p className="simulator-rich-form__muted">{getSimulatorLabel(uiLanguage, "팝업에 표시할 데이터가 없습니다.")}</p>}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function SimulatorRichFormButtonPopupAction({
  action,
  fallback,
  label,
  className,
  disabled,
  onAction,
  formApi,
}: {
  parent: AidotRichFormComponent;
  action: AidotRichFormComponent;
  fallback: string;
  label: string;
  className: string;
  disabled: boolean;
  onAction: (value: string) => void;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const popupComponent = richFormPopupComponent(action);
  const popupContent = richFormPopupContentComponents(popupComponent);
  const dataComponent = richFormPopupDataComponent(popupComponent);
  const rows = richFormDataRows(dataComponent);
  const pageSize = richFormPageSize(dataComponent, rows.length);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const visibleRows = rows.slice(safePage * pageSize, safePage * pageSize + pageSize);
  const keys = richFormDataKeys(dataComponent, rows).slice(0, 8);
  const titles = richFormDataTitles(dataComponent, keys);
  const forms = readAidotRichFormArray(dataComponent, ["dataValueForm"]);
  const showDataTable = popupContent.length === 0 && rows.length > 0;

  function selectRow(row: Record<string, unknown>) {
    setSelected(row);
    if (formApi) formApi.setValue(dataComponent, richFormSelectedDataValue(dataComponent, row), fallback);
    else onAction(simulatorRichFormResultPayload(dataComponent, richFormSelectedDataValue(dataComponent, row), fallback));
  }

  return (
    <div className="simulator-rich-form__button-popup">
      <button className={className} type="button" disabled={disabled || action.disabled === true} onClick={() => setOpen(true)}>
        <span>{label}</span><span aria-hidden="true" className="simulator-rich-form__popup-icon">□</span>
      </button>
      {open ? (
        <div className="simulator-rich-form__modal" role="dialog" aria-modal="true">
          <div className="simulator-rich-form__modal-panel simulator-rich-form__popup-panel--button">
            <button type="button" className="simulator-rich-form__modal-close" aria-label={getSimulatorLabel(uiLanguage, "닫기")} onClick={() => setOpen(false)}>×</button>
            <strong>{readAidotRichFormString(popupComponent, ["popupTitle", "title", "popupName"], label || getSimulatorLabel(uiLanguage, "팝업"))}</strong>
            {renderRichFormLines(readAidotRichFormString(popupComponent, ["popupText", "description"]))}
            {popupContent.length > 0 ? (
              <div className="simulator-rich-form__popup-content">
                {popupContent.map((item, contentIndex) => (
                  <Fragment key={`popup-content-${contentIndex}`}>{renderSimulatorRichFormComponent(item, contentIndex, disabled || action.disabled === true, onAction, uiLanguage, formApi)}</Fragment>
                ))}
              </div>
            ) : null}
            {showDataTable ? (
              <div className="simulator-rich-form__table simulator-rich-form__table--popup">
                <table>
                  <thead><tr>{titles.map((column) => <th key={column}>{column}</th>)}</tr></thead>
                  <tbody>{visibleRows.map((row, rowIndex) => {
                    const absoluteIndex = safePage * pageSize + rowIndex;
                    const isSelected = selected === row;
                    return (
                      <tr key={absoluteIndex} className={isSelected ? "is-selected" : undefined} onClick={() => selectRow(row)}>
                        {keys.map((fieldKey, fieldIndex) => <td key={fieldKey}>{renderSimulatorDataValueForm(forms[fieldIndex], row, row[fieldKey] ?? (fieldIndex === 0 ? `${getSimulatorLabel(uiLanguage, "항목")} ${absoluteIndex + 1}` : "-"))}</td>)}
                      </tr>
                    );
                  })}</tbody>
                </table>
                <RichFormPager className="simulator-rich-form__pager" page={safePage} pageCount={pageCount} onPageChange={setPage} />
              </div>
            ) : null}
            {popupContent.length === 0 && rows.length === 0 ? <p className="simulator-rich-form__muted">{getSimulatorLabel(uiLanguage, "팝업에 표시할 데이터가 없습니다.")}</p> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
function SimulatorRichFormChoiceView({
  component,
  index,
  type,
  title,
  disabled,
  onAction,
  formApi,
}: {
  component: AidotRichFormComponent;
  index: number;
  type: string;
  title: string;
  disabled: boolean;
  onAction: (value: string) => void;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const rows = richFormChoiceRows(component);
  const isCombo = type.includes("COMBO");
  const dataDirection = readAidotRichFormString(component, ["dataDirection", "direction"], isCombo ? "vertical" : "horizontal").toLowerCase();
  const selectedIndexes = readAidotRichFormArray(component, ["selectedIndex"])
    .map((item) => Number(item))
    .filter((item) => Number.isFinite(item))
    .map((item) => (item > 0 ? item - 1 : item));
  const [checkedIndexes, setCheckedIndexes] = useState<number[]>(selectedIndexes);
  const inputKind = type.includes("CHECK") ? "checkbox" : "radio";
  const fieldFallback = `choice${index + 1}`;

  function commitSelection(nextIndexes: number[]) {
    const selectedRows = nextIndexes.map((itemIndex) => rows[itemIndex]).filter(Boolean);
    const value = inputKind === "checkbox" ? selectedRows : selectedRows[0];
    if (formApi) {
      formApi.setValue(component, value, fieldFallback);
      return;
    }
    if (value != null) onAction(simulatorRichFormResultPayload(component, value, fieldFallback));
  }

  if (isCombo) {
    const selectedIndex = checkedIndexes[0] ?? -1;
    return (
      <div className="simulator-rich-form__choice simulator-rich-form__choice--vertical">
        {title ? <strong>{title}</strong> : null}
        <select
          value={selectedIndex >= 0 ? String(selectedIndex) : ""}
          disabled={disabled || component.disabled === true}
          onChange={(event) => {
            const nextIndex = Number(event.currentTarget.value);
            if (!Number.isFinite(nextIndex) || nextIndex < 0) {
              setCheckedIndexes([]);
              return;
            }
            setCheckedIndexes([nextIndex]);
            commitSelection([nextIndex]);
          }}
        >
          <option value="">{getSimulatorLabel(uiLanguage, "선택하세요")}</option>
          {rows.map((row, rowIndex) => <option key={`combo-${index}-${rowIndex}`} value={rowIndex}>{richFormChoiceLabel(row, rowIndex, uiLanguage)}</option>)}
        </select>
      </div>
    );
  }

  return (
    <div className={`simulator-rich-form__choice simulator-rich-form__choice--${dataDirection === "vertical" ? "vertical" : "horizontal"}`}>
      {title ? <strong>{title}</strong> : null}
      <div>{rows.map((row, rowIndex) => {
        const label = richFormChoiceLabel(row, rowIndex, uiLanguage);
        const checked = checkedIndexes.includes(rowIndex);
        return (
          <label key={`choice-${index}-${rowIndex}`} className={`simulator-rich-form__option${checked ? " is-selected" : ""}`}>
            <input
              type={inputKind}
              name={`choice-${index}`}
              checked={checked}
              disabled={disabled || component.disabled === true}
              onChange={(event) => {
                const nextIndexes = inputKind === "checkbox"
                  ? event.currentTarget.checked
                    ? Array.from(new Set([...checkedIndexes, rowIndex])).sort((a, b) => a - b)
                    : checkedIndexes.filter((item) => item !== rowIndex)
                  : [rowIndex];
                setCheckedIndexes(nextIndexes);
                commitSelection(nextIndexes);
              }}
            />
            <span>{label}</span>
          </label>
        );
      })}</div>
    </div>
  );
}

function richFormStarMaxScore(component: AidotRichFormComponent, title: string, text: string) {
  const explicitCandidates = [
    component.max,
    component.maxValue,
    component.maxScore,
    component.scoreMax,
    component.count,
    component.length,
    component.starCount,
    component.rateMax,
    component.scale,
  ];
  for (const candidate of explicitCandidates) {
    const score = richFormPositiveInteger(candidate, 0);
    if (score > 0) return Math.max(1, Math.min(10, score));
  }

  const itemCount = Math.max(
    simulatorObjectItems(component.dataValue).length,
    simulatorObjectItems(component.items).length,
    simulatorObjectItems(component.options).length,
  );
  if (itemCount > 0) return Math.max(1, Math.min(10, itemCount));

  const textCandidates = `${title} ${text} ${String(component.label ?? "")} ${String(component.description ?? "")}`
    .match(/\d+/g)
    ?.map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value > 0) ?? [];
  if (textCandidates.length > 0) return Math.max(1, Math.min(10, Math.max(...textCandidates)));

  return 5;
}
function SimulatorRichFormStarView({
  component,
  index,
  title,
  text,
  disabled,
  onAction,
  formApi,
}: {
  component: AidotRichFormComponent;
  index: number;
  title: string;
  text: string;
  disabled: boolean;
  onAction: (value: string) => void;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const maxScore = richFormStarMaxScore(component, title, text);
  const defaultScore = Math.max(0, Math.min(maxScore, Number(component.selectedValue ?? component.selectedScore ?? component.score ?? component.value ?? 0)));
  const [selectedScore, setSelectedScore] = useState(defaultScore);
  return (
    <div className="simulator-rich-form__star">
      <strong>{title || cleanRichFormText(text)}</strong>
      <div>{Array.from({ length: maxScore }, (_item, itemIndex) => itemIndex + 1).map((score) => (
        <button
          key={score}
          type="button"
          className={score <= selectedScore ? "is-selected" : ""}
          aria-label={formatStudioRuntimeMessage(uiLanguage, "{score}점", { score })}
          disabled={disabled || component.disabled === true}
          onClick={() => {
            setSelectedScore(score);
            if (formApi) formApi.setValue(component, score, `star${index + 1}`);
            else onAction(simulatorRichFormResultPayload(component, score, `star${index + 1}`));
          }}
        >★</button>
      ))}</div>
    </div>
  );
}
function SimulatorRichFormTabsView({
  component,
  index,
  disabled,
  onAction,
  formApi,
}: {
  component: AidotRichFormComponent;
  index: number;
  disabled: boolean;
  onAction: (value: string) => void;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const tabs = simulatorObjectItems(component.tab || component.tabs);
  const [activeIndex, setActiveIndex] = useState(0);
  if (tabs.length === 0) return null;
  const safeActiveIndex = Math.min(activeIndex, tabs.length - 1);
  const activeTab = tabs[safeActiveIndex] || tabs[0];
  const tabRootKey = richFormFieldName(component, `tab${index + 1}`);
  const activeTabKey = cleanRichFormText(activeTab.key || activeTab.id || activeTab.title || `tab${safeActiveIndex + 1}`);
  const responsePath = [{ key: tabRootKey }, { key: activeTabKey }];
  const scopedFormApi = formApi ? {
    ...formApi,
    setValue: (child: AidotRichFormComponent, value: unknown, fallback: string) => formApi.setNestedValue(responsePath, child, value, fallback),
    submit: (child: AidotRichFormComponent, value: unknown, fallback: string, options = {}) => formApi.submitNested(responsePath, child, value, fallback, options),
  } satisfies RichFormFormApi : undefined;
  const children = simulatorObjectItems(activeTab.content || activeTab.contents || activeTab.items);
  return (
    <div className="simulator-rich-form__tabs" data-rich-form-tab={tabRootKey}>
      <div className="simulator-rich-form__tab-list" role="tablist">
        {tabs.map((tab, tabIndex) => (
          <button
            key={`tab-${index}-${tabIndex}`}
            type="button"
            role="tab"
            aria-selected={tabIndex === safeActiveIndex}
            className={`simulator-rich-form__tab-button${tabIndex === safeActiveIndex ? " is-active" : ""}`}
            onClick={() => setActiveIndex(tabIndex)}
          >
            {cleanRichFormText(tab.title || tab.label || tab.key || formatStudioRuntimeMessage(uiLanguage, "탭 {index}", { index: tabIndex + 1 }))}
          </button>
        ))}
      </div>
      <div className="simulator-rich-form__tab-panel" role="tabpanel">
        {children.map((child, childIndex) => renderSimulatorRichFormComponent(child as AidotRichFormComponent, childIndex, disabled, onAction, uiLanguage, scopedFormApi))}
      </div>
    </div>
  );
}
function SimulatorRichFormToggleButton({
  component,
  index,
  title,
  disabled,
  onAction,
  formApi,
}: {
  component: AidotRichFormComponent;
  index: number;
  title: string;
  disabled: boolean;
  onAction: (value: string) => void;
  formApi?: RichFormFormApi;
}) {
  const { language: uiLanguage } = useI18n();
  const [checked, setChecked] = useState(component.value === true);
  const isDisabled = disabled || component.disabled === true;
  const fallback = `toggle${index + 1}`;
  const nextValue = () => {
    if (isDisabled) return;
    const value = !checked;
    setChecked(value);
    if (formApi) formApi.setValue(component, value, fallback);
  };
  return (
    <button
      type="button"
      className={`simulator-rich-form__toggle${checked ? " is-on" : ""}`}
      role="switch"
      aria-checked={checked}
      disabled={isDisabled}
      onClick={nextValue}
    >
      <span>{title || getSimulatorLabel(uiLanguage, "토글")}</span>
      <i aria-hidden="true" />
    </button>
  );
}

function renderSimulatorRichFormComponent(
  component: AidotRichFormComponent,
  index: number,
  disabled: boolean,
  onAction: (value: string) => void,
  language: SupportedLanguage,
  formApi?: RichFormFormApi,
): ReactNode {
  const type = readAidotRichFormString(component, ["type"]).toUpperCase();
  const title = cleanRichFormText(readAidotRichFormString(component, ["title", "label", "name"]));
  const text = readAidotRichFormString(component, ["text", "contents", "description", "value"]);
  const key = `${type || "component"}-${index}`;

  if (type === "HR") return <hr key={key} className="simulator-rich-form__divider" />;
  if (type === "FORM_TITLE") {
    const link = readAidotRichFormString(component, ["link", "url"]);
    return (
      <div key={key} className="simulator-rich-form__title">
        {link ? <a href={link} target="_blank" rel="noreferrer">{title || link}</a> : <strong>{title || "RichForm"}</strong>}
        {component.divider ? <hr className="simulator-rich-form__divider" /> : null}
      </div>
    );
  }
  if (type === "TEXT") return <div key={key} className="simulator-rich-form__text">{title ? <strong>{title}</strong> : null}{renderRichFormLines(text)}</div>;
  if (type === "IMAGE") {
    const url = richFormImageUrl(component);
    const link = readAidotRichFormString(component, ["link", "href", "targetUrl", "targetURL", "actionUrl", "actionURL"]);
    const description = cleanRichFormText(text);
    return (
      <figure key={key} className="simulator-rich-form__media">
        {renderRichFormImageElement(url, title || description || getSimulatorLabel(language, "이미지"), "", { caption: "", link })}
        {title ? <figcaption>{title}</figcaption> : null}
        {description ? renderRichFormLines(description, "simulator-rich-form__media-description") : null}
      </figure>
    );
  }
  if (type === "VIDEO") {
    const url = normalizeRichFormVideoUrl(component);
    const isEmbed = /(?:youtube\.com\/embed\/|player\.vimeo\.com\/video\/)/i.test(url);
    return <div key={key} className="simulator-rich-form__media simulator-rich-form__media--video">{url ? isEmbed ? <iframe src={url} title={title || getSimulatorLabel(language, "동영상")} allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowFullScreen /> : <video src={url} controls preload="metadata" /> : null}{title || text ? renderRichFormLines(title || text) : null}</div>;
  }
  if (type === "ADDRESS") return renderSimulatorAddressComponent(component, index, disabled, onAction, language, formApi);
  if (type === "DATEPICKER" || type === "TIMEPICKER") return renderSimulatorDateTimeComponent(component, index, disabled, onAction, type, formApi);
  if (["INPUT", "TEXTAREA"].includes(type)) {
    const placeholder = readAidotRichFormString(component, ["placeholder", "hint"]);
    const value = readAidotRichFormString(component, ["value", "defaultValue"]);
    const inputType = "text";
    const inputProps = {
      defaultValue: value,
      placeholder,
      disabled: disabled || component.disabled === true,
    };
    return (
      <div key={key} className="simulator-rich-form__field">
        <label>
          {title ? <span>{title}</span> : null}
          {type === "TEXTAREA" ? <textarea {...inputProps} onChange={(event) => formApi?.setValue(component, event.currentTarget.value.trim(), `field${index + 1}`)} /> : <input type={inputType} {...inputProps} onChange={(event) => formApi?.setValue(component, event.currentTarget.value.trim(), `field${index + 1}`)} />}
        </label>
      </div>
    );
  }
  if (["CHECK", "CHECKBOX", "RADIO", "RADIOBUTTON", "COMBO", "COMBOBOX"].includes(type)) return <SimulatorRichFormChoiceView key={key} component={component} index={index} type={type} title={title} disabled={disabled} onAction={onAction} formApi={formApi} />;
  if (type === "TOGGLEBUTTON") {
    return <SimulatorRichFormToggleButton key={key} component={component} index={index} title={title} disabled={disabled} onAction={onAction} formApi={formApi} />;
  }
  if (type === "BUTTON" || type.startsWith("BUTTON")) {
    const items = simulatorRichFormItems(component);
    const direction = readAidotRichFormString(component, ["direction"], "horizontal").toLowerCase();
    const actionClassName = `simulator-rich-form__actions simulator-rich-form__actions--${direction === "vertical" ? "vertical" : "horizontal"}`;
    if (items.length > 0) {
      return <div key={key} className={actionClassName}>{items.map((item, itemIndex) => {
        const itemRecord = item && typeof item === "object" ? item as AidotRichFormComponent : { title: String(item ?? "") };
        const label = cleanRichFormText(readAidotRichFormString(itemRecord, ["title", "label", "text", "value"], String(itemIndex + 1)));
        const itemType = readAidotRichFormString(itemRecord, ["type", "buttonType"]).toLowerCase();
        const className = richFormButtonClassName(`simulator-rich-form__action-button`, itemRecord);
        if (itemType.includes("popup") || itemRecord.popup || itemRecord.popupTitle) {
          return <SimulatorRichFormButtonPopupAction key={`${key}-${itemIndex}`} parent={component} action={itemRecord} fallback={`button${itemIndex + 1}`} label={label} className={className} disabled={disabled} onAction={onAction} formApi={formApi} />;
        }
        return <button key={`${key}-${itemIndex}`} className={className} type="button" disabled={disabled || itemRecord.disabled === true} onClick={() => {
          const value = simulatorRichFormValue(itemRecord);
          const fieldSource = readAidotRichFormString(itemRecord, ["key", "name", "id", "variableName", "field"]) ? itemRecord : component;
          if (formApi) formApi.submit(fieldSource, value, `button${itemIndex + 1}`, { buttonValue: value, validate: !itemType.includes("reject"), buttonOnly: itemType.includes("reject"), includeField: !itemType.includes("reject") });
          else onAction(simulatorRichFormActionPayload(component, itemRecord, `button${itemIndex + 1}`, { buttonValue: true }));
        }}><span>{label}</span></button>;
      })}</div>;
    }
    const className = richFormButtonClassName(`simulator-rich-form__button`, component);
    return <button key={key} className={className} type="button" disabled={disabled} onClick={() => {
      const value = simulatorRichFormValue(component);
      if (formApi) formApi.submit(component, value, "buttonValue", { buttonValue: value, validate: !readAidotRichFormString(component, ["type", "buttonType"]).toLowerCase().includes("reject"), buttonOnly: readAidotRichFormString(component, ["type", "buttonType"]).toLowerCase().includes("reject"), includeField: !readAidotRichFormString(component, ["type", "buttonType"]).toLowerCase().includes("reject") });
      else onAction(simulatorRichFormActionPayload(component, component, "buttonValue", { buttonValue: true }));
    }}><span>{title || cleanRichFormText(text) || getSimulatorLabel(language, "확인")}</span></button>;
  }
  if (type === "INPUT_POPUP" || type === "INPUTPOPUP" || type === "INPUT POPUP") {
    return <SimulatorRichFormInputPopupView key={key} component={component} title={title} disabled={disabled} onAction={onAction} formApi={formApi} />;
  }
  if (type === "TABLE") return <div key={key} className="simulator-rich-form__section">{title ? <strong>{title}</strong> : null}{renderRichFormLines(text)}{renderSimulatorRichFormTable(component, disabled, onAction, formApi)}</div>;
  if (type === "FIELDS") {
    const rows = richFormDataRows(component);
    const keys = richFormDataKeys(component, rows);
    const titles = richFormDataTitles(component, keys);
    const forms = readAidotRichFormArray(component, ["dataValueForm"]);
    return <div key={key} className="simulator-rich-form__fields">{title ? <strong>{title}</strong> : null}{rows.map((row, rowIndex) => <dl key={rowIndex}>{keys.map((fieldKey, fieldIndex) => <Fragment key={fieldKey}><dt>{renderRichFormLines(titles[fieldIndex]) || titles[fieldIndex]}</dt><dd>{renderSimulatorDataValueForm(forms[fieldIndex], row, row[fieldKey])}</dd></Fragment>)}</dl>)}</div>;
  }
  if (type === "CAROUSEL" || type === "IMAGE_LIST") {
    return <SimulatorRichFormCardsView key={key} component={component} title={title} text={text} type={type} disabled={disabled} onAction={onAction} formApi={formApi} />;
  }
  if (type === "TAB") return <SimulatorRichFormTabsView key={key} component={component} index={index} disabled={disabled} onAction={onAction} formApi={formApi} />;
  if (type === "KAKAOMAP") {
    return <SimulatorRichFormMapView key={key} component={component} title={title} text={text} disabled={disabled} onAction={onAction} formApi={formApi} />;
  }
  if (type === "STAR_RATE" || type === "STARRATE" || type === "STAR") return <SimulatorRichFormStarView key={key} component={component} index={index} title={title} text={text} disabled={disabled} onAction={onAction} formApi={formApi} />;
  return <div key={key} className="simulator-rich-form__unsupported">{title || cleanRichFormText(text) || formatStudioRuntimeMessage(language, "{type} 컴포넌트", { type: type || "UNKNOWN" })}</div>;
}

function richFormInputPayload(component: AidotRichFormComponent, value: string, fallback: string) {
  return simulatorRichFormResultPayload(component, value, fallback);
}

function AdaptiveCardRenderer({
  template,
  disabled,
  onAction,
  onRenderError,
  includeSubmittedInputs = false,
}: {
  template: NonNullable<SimulatorMessage["template"]>;
  disabled: boolean;
  includeSubmittedInputs?: boolean;
  onAction: (value: string) => void;
  onRenderError: (error: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const onActionRef = useRef(onAction);
  const onRenderErrorRef = useRef(onRenderError);

  useEffect(() => {
    onActionRef.current = onAction;
    onRenderErrorRef.current = onRenderError;
  }, [onAction, onRenderError]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || template.kind !== "adaptive-card" || !template.rawJson) return;
    container.replaceChildren();
    try {
      const card = new AdaptiveCards.AdaptiveCard();
      card.hostConfig = new AdaptiveCards.HostConfig({ supportsInteractivity: !disabled });
      card.onExecuteAction = (action) => {
        if (disabled) return;
        const source = action.toJSON();
        const data = includeSubmittedInputs && action instanceof AdaptiveCards.SubmitAction
          ? action.data
          : source && typeof source === "object" ? (source as Record<string, unknown>).data : undefined;
        if (typeof data === "string") return onActionRef.current(data);
        if (data && typeof data === "object") return onActionRef.current(JSON.stringify(data));
        const title = typeof action.title === "string" ? action.title.trim() : "";
        onActionRef.current(title || JSON.stringify(source));
      };
      card.parse(template.rawJson);
      const rendered = card.render();
      if (!rendered) {
        onRenderErrorRef.current("Adaptive Card render returned empty element");
        return;
      }
      container.appendChild(rendered);
      if (disabled) {
        container.querySelectorAll("button, input, select, textarea").forEach((element) => {
          (element as HTMLButtonElement | HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement).disabled = true;
        });
      }
    } catch (error) {
      onRenderErrorRef.current(error instanceof Error ? error.message : "Adaptive Card render error");
    }
  }, [disabled, template, includeSubmittedInputs]);

  return <div ref={containerRef} className="simulator-adaptive-card" />;
}

function RichFormRenderer({
  template,
  disabled,
  onAction,
}: {
  template: NonNullable<SimulatorMessage["template"]>;
  disabled: boolean;
  onAction: (value: string) => void;
}) {
  const { language: uiLanguage } = useI18n();
  const components = getAidotRichFormComponents(template.rawJson);
  const initialDraft = useMemo(() => richFormInitialDraftFromComponents(components), [template.rawJson]);
  const [draft, setDraft] = useState<RichFormResponseDraft>(() => initialDraft);
  const [validationError, setValidationError] = useState("");
  useEffect(() => {
    setDraft(initialDraft);
    setValidationError("");
  }, [initialDraft]);
  const formApi = useMemo<RichFormFormApi>(() => ({
    setValue: (component, value, fallback) => {
      setDraft((current) => {
        const [fieldName, entry] = richFormResponseEntry(component, value, fallback, current);
        return { ...current, [fieldName]: entry };
      });
      setValidationError("");
    },
    submit: (component, value, fallback, options = {}) => {
      const [fieldName, entry] = richFormResponseEntry(component, value, fallback, draft);
      const nextDraft = options.includeField === false ? { ...draft } : { ...draft, [fieldName]: entry };
      if (options.validate !== false) {
        const error = richFormValidateDraftComponents(components, nextDraft, uiLanguage);
        if (error) {
          setValidationError(error);
          return;
        }
      }
      setValidationError("");
      onAction(richFormPayloadFromDraft(nextDraft, options));
    },
    setNestedValue: (responsePath, component, value, fallback) => {
      setDraft((current) => {
        const [fieldName, entry] = richFormResponseEntry(component, value, fallback, current);
        return richFormNestedDraft(current, responsePath, fieldName, entry);
      });
      setValidationError("");
    },
    submitNested: (responsePath, component, value, fallback, options = {}) => {
      const [fieldName, entry] = richFormResponseEntry(component, value, fallback, draft);
      const nextDraft = options.includeField === false ? { ...draft } : richFormNestedDraft(draft, responsePath, fieldName, entry);
      if (options.validate !== false) {
        const error = richFormValidateDraftComponents(components, nextDraft, uiLanguage);
        if (error) {
          setValidationError(error);
          return;
        }
      }
      setValidationError("");
      onAction(richFormPayloadFromDraft(nextDraft, options));
    },
  }), [components, draft, onAction, uiLanguage]);
  if (components.length === 0) return null;
  return <div className="simulator-rich-form">{components.map((component, index) => {
    const componentKey = `${readAidotRichFormString(component, ["type"])}-${readAidotRichFormString(component, ["key", "name", "id"])}-${index}`;
    return <Fragment key={componentKey}>{renderSimulatorRichFormComponent(component, index, disabled, onAction, uiLanguage, formApi)}</Fragment>;
  })}{validationError ? <p className="simulator-rich-form__validation-error">{validationError}</p> : null}</div>;
}

function buildTalkMessages(node: Extract<DialogFlowNode, { kind: "talk" }>, variables: Record<string, string>, language: SupportedLanguage) {
  const config = getTalkConfigForChannel(node, "SM_CHAT");
  const channelNode = { ...node, config };
  const baseMessages = config.basicMessages.map((item) => replaceVariables(item, variables).trim()).filter(Boolean);

  if (baseMessages.length > 0 && config.messageType === "text") {
    return baseMessages.map((text) => makeMessage("bot", text, { sourceTalkNodeId: node.id }));
  }

  const messages = config.messages.map((item) => replaceVariables(item, variables));
  const first = messages[0]?.trim() ?? "";

  switch (config.messageType) {
    case "html":
      return [makeMessage("bot", "", { html: first, sourceTalkNodeId: node.id })];
    case "card":
      return [
        makeMessage("bot", first || getStudioRuntimeMessage(language, "Card 메시지입니다."), {
          cardTitle: messages[0] || "Card",
          cardImageUrl: messages[1]?.trim() || "",
          cardDescription: messages[2]?.trim() || "",
          sourceTalkNodeId: node.id,
        }),
      ];
    case "table":
      const tableMessage = buildTableMessage(channelNode, variables, language);
      return [
        makeMessage("bot", "", {
          table: {
            columns: tableMessage.columns,
            rows: tableMessage.rows,
            selectable: config.responseType === "single-select",
          },
          sourceTalkNodeId: node.id,
          debugLogs: tableMessage.debugLogs,
        }),
      ];
    case "button":
      return [
        makeMessage("bot", baseMessages[0] || getStudioRuntimeMessage(language, "선택하세요."), {
          quickReplies: messages.filter(Boolean),
          sourceTalkNodeId: node.id,
        }),
      ];
    case "link-button":
      return [
        makeMessage("bot", baseMessages[0] || getStudioRuntimeMessage(language, "링크 버튼 메시지입니다."), {
          cardTitle: "Link Button",
          cardItems: config.linkButtonItems.map((item) => {
            const url = item.url.trim();
            return {
              label: item.label || "Label",
              value: url || "-",
              href: /^https?:\/\//i.test(url) ? url : undefined,
            };
          }),
          sourceTalkNodeId: node.id,
        }),
      ];
    case "form":
      const richFormSource = collectRichFormSource(messages) || fallbackRichFormSource(first || node.title) || first;
      const formTemplate = buildTemplateMessage("rich-form", richFormSource, "Rich Form", channelNode);
      return [
        makeMessage("bot", "", {
          template: formTemplate,
          sourceTalkNodeId: node.id,
          debugLogs: formTemplate.debugLogs,
        }),
      ];
    case "carousel":
      return [
        makeMessage("bot", first || getStudioRuntimeMessage(language, "Carousel 메시지입니다."), {
          cardTitle: messages[2]?.trim() || first || "Carousel",
          cardImageUrl: messages[1]?.trim() || "",
          cardDescription: messages[3]?.trim() || "",
          sourceTalkNodeId: node.id,
        }),
      ];
    case "dtmf": {
      const numberAt = (index: number, defaultValue: number) => Math.max(1, Number.parseInt(messages[index] || String(defaultValue), 10) || defaultValue);
      const maxLength = numberAt(0, 1);
      const minLength = Math.min(maxLength, numberAt(1, 1));
      const configuredEndCharacter = messages[2]?.trim();
      const endCharacter = configuredEndCharacter === "*" || configuredEndCharacter === "#" ? configuredEndCharacter : "#";
      return [makeMessage("bot", baseMessages[0] || getStudioRuntimeMessage(language, "번호를 입력하세요."), {
        dtmf: {
          minLength,
          maxLength,
          endCharacter,
          firstInputTimeoutMs: numberAt(3, 10) * 100,
          overallInputTimeoutMs: numberAt(4, 10) * 100,
        },
        sourceTalkNodeId: node.id,
      })];
    }
    case "form-a-card":
      const resolvedMsgKey = (messages[1] ?? "").trim();
      const msgKeyTemplateSource =
        resolvedMsgKey.startsWith("{") || resolvedMsgKey.startsWith("[") ? resolvedMsgKey : "";
      const adaptiveTemplate = buildTemplateMessage(
        "adaptive-card",
        msgKeyTemplateSource || first,
        "Form(A Card)",
        channelNode,
        resolvedMsgKey,
      );
      if (msgKeyTemplateSource) {
        adaptiveTemplate.debugLogs.push({
          level: "debug",
          event: "simulator.form_a_card_msgkey_resolved",
          detail: { nodeId: node.id, nodeTitle: node.title, msgKey: resolvedMsgKey },
        });
      }
      return [
        makeMessage("bot", "", {
          template: adaptiveTemplate,
          sourceTalkNodeId: node.id,
          debugLogs: adaptiveTemplate.debugLogs,
        }),
      ];
    default:
      return baseMessages.length > 0 ? baseMessages.map((text) => makeMessage("bot", text)) : [];
  }
}

function evaluateCondition(operator: string, currentValue: string, compareValue: string) {
  const left = currentValue.trim();
  const right = compareValue.trim();
  const leftNumber = Number(left);
  const rightNumber = Number(right);

  switch (operator) {
    case "exists":
      return Boolean(left);
    case "not-exists":
      return !left;
    case "equals":
      return left === right;
    case "not-equals":
      return left !== right;
    case "contains":
      return left.includes(right);
    case "greater-than":
      return Number.isFinite(leftNumber) && Number.isFinite(rightNumber) && leftNumber > rightNumber;
    case "greater-or-equal":
      return Number.isFinite(leftNumber) && Number.isFinite(rightNumber) && leftNumber >= rightNumber;
    case "less-than":
      return Number.isFinite(leftNumber) && Number.isFinite(rightNumber) && leftNumber < rightNumber;
    case "less-or-equal":
      return Number.isFinite(leftNumber) && Number.isFinite(rightNumber) && leftNumber <= rightNumber;
    case "regex":
      try {
        return new RegExp(right).test(left);
      } catch {
        return false;
      }
    case "else":
      return true;
    default:
      return false;
  }
}

function runScriptNode(
  node: Extract<DialogFlowNode, { kind: "script" }>,
  variables: Record<string, string>,
  language: SupportedLanguage,
) {
  const params = Object.fromEntries(
    node.config.parameters.map((item) => [item.name, replaceVariables(item.value, variables)]),
  );
  const workingVariables = { ...variables };
  const changedVariables: Record<string, string> = {};
  const consoleLogs: string[] = [];
  const serializeScriptValue = (value: unknown) => {
    if (typeof value === "string") {
      return value;
    }
    if (value == null) {
      return "";
    }
    return JSON.stringify(value);
  };
  const setVariableValue = (name: string, value: unknown) => {
    const variableName = stripVariablePrefix(name);
    if (!variableName) {
      return value;
    }
    const serialized = serializeScriptValue(value);
    workingVariables[variableName] = serialized;
    changedVariables[variableName] = serialized;
    return value;
  };
  const getVariableValue = (name: string) => workingVariables[stripVariablePrefix(name)] ?? "";
  const normalizeAdaptiveCard = (value: unknown) => {
    const source = typeof value === "string" ? JSON.parse(value) : value;
    if (!source || typeof source !== "object" || Array.isArray(source)) {
      throw new Error(getStudioRuntimeMessage(language, "Adaptive Card JSON은 객체여야 합니다."));
    }
    return {
      $schema: "https://adaptivecards.io/schemas/adaptive-card.json",
      type: "AdaptiveCard",
      version: "1.6",
      ...source,
    };
  };
  const context = {
    variables: workingVariables,
    params,
    get: getVariableValue,
    set: setVariableValue,
    setJson: (name: string, value: unknown) => setVariableValue(name, serializeScriptValue(value)),
    card: {
      normalize: normalizeAdaptiveCard,
      stringify: (value: unknown) => JSON.stringify(normalizeAdaptiveCard(value)),
      set: (name: string, value: unknown) => setVariableValue(name, JSON.stringify(normalizeAdaptiveCard(value))),
    },
    console: {
      log: (...items: unknown[]) => {
        consoleLogs.push(items.map((item) => serializeScriptValue(item)).join(" "));
      },
    },
  };
  const scriptVariableSnapshot = Object.fromEntries(
    node.config.returnVariables
      .map((item) => item.scriptVariableName.trim())
      .filter((name) => /^[A-Za-z_$][0-9A-Za-z_$]*$/.test(name))
      .map((name) => [
        name,
        `typeof ${name} === "undefined" ? undefined : ${name}`,
      ]),
  );
  const scriptVariableReturnExpression = Object.entries(scriptVariableSnapshot)
    .map(([name, expression]) => `${JSON.stringify(name)}: ${expression}`)
    .join(", ");

  try {
    const result = Function(
      "variables",
      "params",
      "context",
      "get",
      "set",
      "setJson",
      "card",
      "console",
      `"use strict";
${node.config.code}
return { variables, params, scriptVariables: { ${scriptVariableReturnExpression} } };`,
    )(
      workingVariables,
      params,
      context,
      context.get,
      context.set,
      context.setJson,
      context.card,
      context.console,
    ) as
      | {
          variables?: Record<string, unknown>;
          params?: Record<string, unknown>;
          scriptVariables?: Record<string, unknown>;
          [key: string]: unknown;
        }
      | undefined;
    const resultRecord =
      result && typeof result === "object" && !Array.isArray(result)
        ? (result as Record<string, unknown>)
        : {};
    const resultLooksLikeRuntimeEnvelope =
      "variables" in resultRecord || "params" in resultRecord || "scriptVariables" in resultRecord;
    const nextVariables = { ...workingVariables };
    Object.entries(result?.variables ?? {}).forEach(([name, value]) => {
      nextVariables[stripVariablePrefix(name)] = serializeScriptValue(value);
    });

    node.config.returnVariables.forEach((item) => {
      const variableName = stripVariablePrefix(item.variableName);
      const scriptVariableName = item.scriptVariableName.trim();
      const rawValue =
        result?.scriptVariables?.[scriptVariableName] ??
        result?.variables?.[scriptVariableName] ??
        result?.params?.[scriptVariableName] ??
        result?.[scriptVariableName] ??
        result?.variables?.[variableName] ??
        result?.[variableName] ??
        (node.config.returnVariables.length === 1 && result != null && !resultLooksLikeRuntimeEnvelope
          ? result
          : undefined);
      if (rawValue != null) {
        const serialized = serializeScriptValue(rawValue);
        const returnId = item.id.trim();
        nextVariables[variableName] = serialized;
        if (returnId) {
          nextVariables[returnId] = serialized;
          nextVariables[`${node.id}-${returnId}`] = serialized;
        }
      }
    });
    Object.entries(nextVariables).forEach(([name, value]) => {
      if (variables[name] !== value) {
        changedVariables[name] = value;
      }
    });

    return { variables: nextVariables, error: "", changedVariables, consoleLogs };
  } catch (error) {
    return {
      variables,
      error: error instanceof Error ? error.message : SIMULATOR_RUNTIME_MESSAGES.scriptException,
      changedVariables,
      consoleLogs,
    };
  }
}

type SimulatorPageProps = {
  embedded?: boolean;
  startDialogId?: string;
  botIdOverride?: string;
  versionIdOverride?: string;
  onClose?: () => void;
};

// 샘플 서버가 지정한 봇만 지속 경로로 연결한다. 기존 CGA runtime은 아래에 보존한다.
export function SimulatorPage(props: SimulatorPageProps = {}) {
  const { language: uiLanguage } = useI18n();
  const params = useParams<{botId:string}>();
  const router = useRouter();
  const botId = props.botIdOverride || (typeof params.botId === "string" ? decodeURIComponent(params.botId) : "");
  const [context, setContext] = useState<{session:AuthSession; config:PersistentConfig} | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const session = loadAuthSession();
    if (!session) { router.replace("/login"); return; }
    setContext(null); setError("");
    fetchStudioPersistentConfig(session.access_token, botId, controller.signal).then(config => {
      if (!controller.signal.aborted) setContext({session, config});
    }).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : getStudioRuntimeMessage(uiLanguage, "대화 설정 조회 실패"));
    });
    return () => controller.abort();
  }, [botId, router, revision]);
  // 조회 실패/기능 OFF의 보호 방을 단발 경로로 우회하지 않는다.
  if (error) return <div className="simulator-alert" role="alert">{error}<button type="button" onClick={() => setRevision(value => value + 1)}>{getStudioRuntimeMessage(uiLanguage, "설정 다시 조회")}</button></div>;
  if (!context) return <div className="simulator-empty">{getStudioRuntimeMessage(uiLanguage, "대화 설정 확인 중")}</div>;
  return context.config.enabled
    ? <PersistentSimulator key={`${context.session.user.id}:${botId}`} {...props} botId={botId} session={context.session} config={context.config} />
    : <LegacySimulatorPage {...props} />;
}

function PersistentSimulator({botId, session, config, embedded = false, onClose, startDialogId = ""}: SimulatorPageProps & {
  botId:string; session:AuthSession; config:PersistentConfig;
}) {
  const { language: uiLanguage } = useI18n();
  const router = useRouter();
  const token = session.access_token;
  const storageKey = persistentRoomStorageKey(session.user.id, botId);
  const [rooms, setRooms] = useState<PersistentRoom[]>([]);
  const [room, setRoom] = useState<PersistentRoom | null>(null);
  const [transcript, setTranscript] = useState<PersistentTranscript["messages"]>([]);
  const [turn, setTurn] = useState<PersistentTurn | null>(null);
  const [dialogId, setDialogId] = useState(startDialogId || Object.keys(config.profiles)[0] || "");
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [uncertainKey, setUncertainKey] = useState("");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [loadingRooms, setLoadingRooms] = useState(true);
  const displayRef = useRef<HTMLSpanElement | null>(null);
  const ackAttempts = useRef(new Set<string>());
  const roomScope = useRef("");
  roomScope.current = room ? `${room.id}:${room.generation}` : "";
  const live = useRef(true);
  const busyRef = useRef(false);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  function remember(next: PersistentRoom, key = "") {
    // 서버가 매번 소유권을 검증한다. 이 값은 자격 증명이 아닌 재접속 힌트다.
    sessionStorage.setItem(storageKey, JSON.stringify({room_id:next.id,generation:next.generation,pending_key:key}));
  }
  useEffect(() => {
    const controller = new AbortController();
    fetchStudioPersistentRooms(token, botId, controller.signal).then(data => {
      if (controller.signal.aborted) return;
      setRooms(data.rooms);
      let hint: {room_id?:string;generation?:number;pending_key?:string} = {};
      try { hint = JSON.parse(sessionStorage.getItem(storageKey) || "{}"); } catch { /* 인증은 서버가 담당한다. */ }
      const restored = data.rooms.find(row => row.id === hint.room_id && row.generation === hint.generation);
      if (restored) { setRoom(restored); setDialogId(restored.dialog_id); setUncertainKey(hint.pending_key || ""); }
    }).catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : getStudioRuntimeMessage(uiLanguage, "이력 조회 실패")); })
      .finally(() => { if (!controller.signal.aborted) setLoadingRooms(false); });
    return () => controller.abort();
  }, [token, botId, storageKey]);
  const roomId = room?.id;
  const generation = room?.generation;
  useEffect(() => {
    if (!roomId || !generation) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const data = await fetchStudioPersistentRoom(token, botId, roomId!, generation!, controller.signal);
        const next = data.room.current_event_id ? await fetchStudioPersistentTurn(token, botId, roomId!, data.room.current_event_id, controller.signal) : null;
        if (controller.signal.aborted) return;
        if (persistentTranscriptNeedsRefresh(data, next)) {
          timer = setTimeout(() => { void poll(); }, 250);
          return;
        }
        setRoom(data.room); setTranscript(data.messages); setTurn(next);
        setRooms(items => [data.room, ...items.filter(item => item.id !== data.room.id || item.generation !== data.room.generation)]);
        if (uncertainKey && data.room.current_message_key === uncertainKey) { setUncertainKey(""); remember(data.room); }
        if (next && ["reserved", "conversation_creating", "history_check", "submitting", "submitted", "generated", "delivery_confirmed", "memory_syncing"].includes(next.phase)) {
          timer = setTimeout(() => { void poll(); }, 1000);
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : getStudioRuntimeMessage(uiLanguage, "상태 조회 실패"));
      }
    }
    void poll();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [token, botId, roomId, generation, revision, uncertainKey]);
  async function acknowledge() {
    if (!room || !turn) return;
    const receipt = persistentDisplayReceipt(turn, displayRef.current);
    if (!receipt) return;
    const expectedScope = `${room.id}:${room.generation}`;
    try {
      const next = await ackStudioPersistentTurn(token, botId, room.id, turn.event_id, receipt);
      if (live.current && roomScope.current === expectedScope) { setTurn(next); setRevision(value => value + 1); }
    } catch (cause) { if (live.current) setError(cause instanceof Error ? cause.message : getStudioRuntimeMessage(uiLanguage, "표출 확인 실패")); }
    // 실패 시 자동 재시도하지 않는다. 같은 서버 receipt의 명시적 재확인만 허용한다.
  }
  useEffect(() => {
    if (room?.status !== "open" || !turn || turn.phase !== "pending_delivery" || !persistentDisplayReceipt(turn, displayRef.current)) return;
    const key = `${room.id}:${room.generation}:${turn.event_id}`;
    if (ackAttempts.current.has(key)) return;
    ackAttempts.current.add(key);
    void acknowledge();
  }, [turn, room?.id, room?.generation, room?.status]);
  const profiles = room?.status === "open" ? room.profiles : config.profiles;
  const locked = !canSendPersistentTurn(room, turn, busy || Boolean(uncertainKey));
  async function action(operation: "start" | "new" | "end" | "cancel" | "reconcile") {
    if (busyRef.current) return;
    if (operation === "new" && !window.confirm(getStudioRuntimeMessage(uiLanguage, "이전 기록은 조회용으로 남고 LLM 문맥은 초기화됩니다. 최신 운영 버전으로 새 대화를 시작할까요?"))) return;
    if (operation === "end" && !window.confirm(getStudioRuntimeMessage(uiLanguage, "이 방을 종료할까요? 기록은 보관되며 다음 대화에 자동 반영되지 않습니다."))) return;
    busyRef.current = true; setBusy(true); setError("");
    try {
      if (operation === "start" || !room) {
        const data = await startStudioPersistentRoom(token, botId, dialogId);
        if (!live.current) return;
        remember(data.room); setRoom(data.room); setTranscript([]); setTurn(null); setUncertainKey("");
      } else if (operation === "new" || operation === "end") {
        const data = operation === "new"
          ? await resetStudioPersistentRoom(token, botId, room.id, room.generation, Object.keys(config.profiles).includes(dialogId) ? dialogId : Object.keys(config.profiles)[0])
          : await closeStudioPersistentRoom(token, botId, room.id, room.generation);
        if (!live.current) return;
        remember(data.room); setRoom(data.room); setDialogId(data.room.dialog_id); setTurn(null); setUncertainKey("");
      } else if (turn) {
        const result = await manageStudioPersistentTurn(token, botId, room.id, turn.event_id, operation);
        if (!live.current) return;
        setTurn(result);
      }
      if (live.current) { setInput(""); setRevision(value => value + 1); }
    } catch (cause) { if (live.current) setError(cause instanceof Error ? cause.message : getStudioRuntimeMessage(uiLanguage, "요청 처리 실패")); }
    finally { busyRef.current = false; if (live.current) setBusy(false); }
  }
  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (busyRef.current || locked || !room || !isPersistentQuestionValid(input) || !profiles[dialogId]) return;
    busyRef.current = true; setBusy(true); setError("");
    const messageKey = crypto.randomUUID();
    // 전송 전 키를 저장한다. timeout 뒤 모델 POST를 자동 반복하지 않는다.
    remember(room, messageKey); setUncertainKey(messageKey);
    try {
      const result = await submitStudioPersistentTurn(token, botId, room.id, {expected_generation:room.generation,message_key:messageKey,question:input.trim(),dialog_id:dialogId});
      if (live.current) { remember(room); setUncertainKey(""); setInput(""); setTurn(result); }
    } catch (cause) {
      if (live.current) {
        const message = cause instanceof Error ? cause.message : getStudioRuntimeMessage(uiLanguage, "전송 실패");
        if (isPersistentSubmissionRejected(cause)) {
          remember(room); setUncertainKey(""); setError(message);
        } else {
          setError(`${message} ${getStudioRuntimeMessage(uiLanguage, "결과를 조회해 확인해 주세요. 자동 재전송하지 않습니다.")}`);
        }
      }
    }
    finally { busyRef.current = false; if (live.current) { setBusy(false); setRevision(value => value + 1); } }
  }
  const display = turn?.phase === "pending_delivery" && room?.status === "open" ? turn.message : null;
  const rows = transcript.filter(message => message.id !== display?.id);
  return <div className={`simulator-workbench${embedded ? " simulator-workbench--popup" : ""} simulator-workbench--debug-open`}>
    <div className="simulator-stage simulator-stage--flat simulator-stage--messenger"><div className="simulator-window simulator-window--messenger">
      <div className="simulator-window__header"><div className="simulator-title"><strong>{getStudioRuntimeMessage(uiLanguage, "지속 LLM 대화")}</strong><small>{getStudioRuntimeMessage(uiLanguage, "Simulator / Webchat 공통 대화")}</small></div>
        <button className="icon-button" type="button" aria-label={getStudioRuntimeMessage(uiLanguage, "시뮬레이터 닫기")} onClick={() => embedded ? onClose?.() : router.back()}>×</button></div>
      {error ? <div className="simulator-alert" role="alert">{error}</div> : null}
      {!config.ready ? <div className="simulator-alert">{config.reason || getStudioRuntimeMessage(uiLanguage, "새 대화 준비가 완료되지 않았습니다.")}</div> : null}
      <div className="simulator-window__body simulator-window__body--scroll"><div className="chat-stack" aria-live="polite">
        {rows.map(message => <div className={`chat-row chat-row--${message.sender}`} key={message.id}>
          {message.sender === "bot" ? <span className="chat-message-avatar" /> : null}
          <div className="chat-message"><div className="chat-message__line"><div className={`chat-bubble chat-bubble--${message.sender === "user" ? "user" : "bot"}`}>
          <span style={{whiteSpace:"pre-wrap"}}>{message.text}</span></div></div><small>{message.memory_committed ? getStudioRuntimeMessage(uiLanguage, "문맥 반영 완료") : getStudioRuntimeMessage(uiLanguage, "문맥 미반영")}</small></div></div>)}
        {display ? <div className="chat-row chat-row--bot"><span className="chat-message-avatar" /><div className="chat-message"><div className="chat-message__line"><div className="chat-bubble chat-bubble--bot"><span ref={displayRef} data-persistent-message-id={display.id} style={{whiteSpace:"pre-wrap"}}>{display.text}</span></div></div><small>{getStudioRuntimeMessage(uiLanguage, "표출 확인 대기 · 문맥 미반영")}</small></div></div> : null}
        {!room ? <p>{getStudioRuntimeMessage(uiLanguage, "모듈을 선택한 뒤 대화를 시작해 주세요.")}</p> : null}
      </div></div>
      <form className="simulator-composer simulator-composer--messenger" onSubmit={submit}>
        <textarea className="textarea-control textarea-control--flat" aria-label={getStudioRuntimeMessage(uiLanguage, "질문")} maxLength={4000} placeholder={getStudioRuntimeMessage(uiLanguage, "봇에게 질문해 주세요")} value={input} disabled={locked} onChange={event => setInput(event.target.value)} onKeyDown={event => {if(event.key === "Enter" && !event.shiftKey){event.preventDefault();void submit();}}} />
        <button className="primary-action" type="submit" disabled={locked || !isPersistentQuestionValid(input)}>{getStudioRuntimeMessage(uiLanguage, "전송")}</button>
      </form>
    </div></div>
    <aside className="simulator-analysis"><div className="simulator-analysis__header"><strong>{getStudioRuntimeMessage(uiLanguage, "대화 상태")}</strong></div><div className="simulator-analysis__block">
      <label>{getStudioRuntimeMessage(uiLanguage, "답변 모듈")}<select className="select-control" aria-label={getStudioRuntimeMessage(uiLanguage, "답변 모듈")} value={dialogId} disabled={busy || Boolean(turn && turn.phase !== "committed")} onChange={event => setDialogId(event.target.value)}>{Object.entries(profiles).map(([id, profile]) => <option key={id} value={id}>{profile.name} ({profile.uses_llm ? "LLM" : getStudioRuntimeMessage(uiLanguage, "고정 답변")})</option>)}</select></label>
      <p>{room ? formatStudioRuntimeMessage(uiLanguage, "고정 버전 {version} / 대화 {generation} / {status}", {version:room.version_id,generation:room.generation,status:room.status}) : getStudioRuntimeMessage(uiLanguage, "시작 전")}</p>
      <p role="status">{uncertainKey ? getStudioRuntimeMessage(uiLanguage, "전송 결과 확인 필요 · 자동 재호출 안 함") : turn?.phase || getStudioRuntimeMessage(uiLanguage, "대기")}</p>
      {room?.scenario_ended ? <p>{getStudioRuntimeMessage(uiLanguage, "시나리오 종료 · LLM 문맥 유지")}</p> : null}
      <button className="secondary-action" type="button" disabled={busy || loadingRooms || !config.ready || !dialogId || room?.status === "open"} onClick={() => void action("start")}>{getStudioRuntimeMessage(uiLanguage, "새 방 시작")}</button>
      <button className="secondary-action" type="button" disabled={busy || !room || room.status !== "open" || !config.ready} onClick={() => void action("new")}>{getStudioRuntimeMessage(uiLanguage, "새 대화 · 최신 버전 적용")}</button>
      <button className="secondary-action" type="button" disabled={busy || !room || room.status !== "open"} onClick={() => void action("end")}>{getStudioRuntimeMessage(uiLanguage, "방 종료")}</button>
      <button className="secondary-action" type="button" disabled={busy || !room} onClick={() => {setError("");setRevision(value => value + 1);}}>{getStudioRuntimeMessage(uiLanguage, "상태 다시 조회")}</button>
      <button className="secondary-action" type="button" disabled={busy || !turn || room?.status !== "open"} onClick={() => void action("reconcile")}>{getStudioRuntimeMessage(uiLanguage, "결과 확인 · 생성 재호출 없음")}</button>
      <button className="secondary-action" type="button" disabled={busy || !turn || turn.phase === "committed" || room?.status !== "open"} onClick={() => void action("cancel")}>{getStudioRuntimeMessage(uiLanguage, "진행 요청 취소")}</button>
      {display ? <button className="secondary-action" type="button" disabled={busy} onClick={() => void acknowledge()}>{getStudioRuntimeMessage(uiLanguage, "표출 다시 확인")}</button> : null}
      <label>{getStudioRuntimeMessage(uiLanguage, "이전 대화 조회")}<select className="select-control" aria-label={getStudioRuntimeMessage(uiLanguage, "이전 대화 조회")} value={room ? `${room.id}:${room.generation}` : ""} disabled={busy || Boolean(uncertainKey)} onChange={event => {
        const selected = rooms.find(item => `${item.id}:${item.generation}` === event.target.value);
        if (selected) {remember(selected);setRoom(selected);setTurn(null);setTranscript([]);setUncertainKey("");setDialogId(selected.dialog_id);setError("");setRevision(value => value + 1);}
      }}><option value="" disabled>{getStudioRuntimeMessage(uiLanguage, "대화 선택")}</option>{rooms.map(item => <option key={`${item.id}:${item.generation}`} value={`${item.id}:${item.generation}`}>{item.id.slice(0,8)} / {item.generation} / {item.status}</option>)}</select></label>
      <p>{getStudioRuntimeMessage(uiLanguage, "이전 기록은 조회용입니다. 새 LLM 문맥에 자동으로 넣지 않습니다.")}</p>
    </div></aside>
  </div>;
}

function LegacySimulatorPage({ embedded = false, startDialogId: startDialogIdProp = "", botIdOverride = "", versionIdOverride = "", onClose }: SimulatorPageProps = {}) {
  const { language: uiLanguage } = useI18n();
  const copy = SIMULATOR_CATALOGS[uiLanguage];
  const params = useParams<{ botId: string; versionId: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const botId = botIdOverride || (typeof params.botId === "string" ? decodeURIComponent(params.botId) : "");
  const versionId = versionIdOverride || (typeof params.versionId === "string" ? decodeURIComponent(params.versionId) : "v1");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [authSession, setAuthSession] = useState<AuthSession | null>(null);
  const [bot, setBot] = useState<StudioBotApiItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState("");
  const [messages, setMessages] = useState<SimulatorMessage[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [dtmfValues, setDtmfValues] = useState<Record<string, string>>({});
  const [dtmfErrors, setDtmfErrors] = useState<Record<string, string>>({});
  const [runtime, setRuntime] = useState<RuntimeState | null>(null);
  const [analyses, setAnalyses] = useState<SimulatorAnalysis[]>([]);
  const [selectedAnalysisId, setSelectedAnalysisId] = useState<string | null>(null);
  const [analysisOpen, setAnalysisOpen] = useState(false);
  const [loadedScript, setLoadedScript] = useState<LoadedScript | null>(null);
  const [scriptViewerOpen, setScriptViewerOpen] = useState(false);
  const [floatingButtonsVisible, setFloatingButtonsVisible] = useState(true);
  const [pendingIntentSelection, setPendingIntentSelection] = useState<PendingIntentSelection | null>(null);
  const [nluSupplementSaving, setNluSupplementSaving] = useState(false);
  const [nluSupplementMessage, setNluSupplementMessage] = useState("");
  const [sharedMode, setSharedMode] = useState(false);
  const analysisVisible = !embedded || analysisOpen;
  const [sharedMember, setSharedMember] = useState<AidotSharedRoomIdentity | null>(null);
  const [sharedHistory, setSharedHistory] = useState<AidotSharedRoomHistoryResponse | null>(null);
  const [sharedRoomInput, setSharedRoomInput] = useState("");
  const [sharedName, setSharedName] = useState("");
  const [sharedError, setSharedError] = useState("");
  const [sharedSendError, setSharedSendError] = useState("");
  const [sharedConnecting, setSharedConnecting] = useState(false);
  const [sharedSendingBot, setSharedSendingBot] = useState(false);
  const [sharedSendingGeneral, setSharedSendingGeneral] = useState(false);
  const sharedEpoch = useRef(0);
  const sharedReadSequence = useRef(0);
  const sharedSendGuard = useRef({ bot: false, general: false });
  const sharedConnectGuard = useRef(false);
  const sharedStorageKey = authSession ? `aidot.simulator.shared.${authSession.user.id}.${botId}` : "";
  const sharedConnected = Boolean(sharedMember && sharedHistory);
  const sharedCanAsk = canAskAidotRoomBot(sharedHistory?.turn, sharedMember?.participantId || "", sharedConnected, sharedSendingBot);
  const sharedCanChat = canSendAidotRoomChat(sharedConnected, sharedSendingGeneral);
  const canUseMessageCard = (message: SimulatorMessage) => sharedMode
    ? canRespondAidotRoomCard(sharedHistory?.runtime, message.sourceTalkNodeId, sharedCanAsk, message.id, sharedHistory?.messages)
    : Boolean(message.sourceTalkNodeId && runtime?.waitingTalkNodeId === message.sourceTalkNodeId && !runtime.ended);
  const sharedTemplateCache = useRef<SharedSimulatorTemplateCache>(new Map());
  const displayedMessages = sharedMode ? sharedMessagesForSimulator(sharedHistory?.messages || [], sharedTemplateCache.current) : messages;

  useEffect(() => {
    if (!sharedStorageKey) return;
    sharedEpoch.current += 1;
    setSharedMember(null); setSharedHistory(null); setSharedError("");
    try {
      const saved = JSON.parse(sessionStorage.getItem(sharedStorageKey) || "null") as AidotSharedRoomIdentity | null;
      if (saved && typeof saved.roomId === "string" && typeof saved.participantId === "string" && typeof saved.conversationToken === "string") {
        setSharedMember(saved); setSharedRoomInput(saved.roomId);
      }
    } catch { sessionStorage.removeItem(sharedStorageKey); }
    return () => { sharedEpoch.current += 1; };
  }, [sharedStorageKey]);

  async function refreshSharedRoom(member: AidotSharedRoomIdentity, epoch: number, signal?: AbortSignal) {
    const sequence = ++sharedReadSequence.current;
    try {
      const history = await fetchAidotSharedRoom(window.location.origin, botId, member, signal);
      if (sharedEpoch.current === epoch && sharedReadSequence.current === sequence) {
        setSharedHistory(history); setSharedError("");
      }
    } catch (error) {
      if (sharedEpoch.current !== epoch || sharedReadSequence.current !== sequence) return;
      throw error;
    }
  }

  useEffect(() => {
    if (!sharedMode || !sharedMember) return;
    const member = sharedMember; const epoch = sharedEpoch.current;
    let disposed = false; let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | null = null;
    async function poll() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 10000);
      try { await refreshSharedRoom(member, epoch, controller.signal); }
      catch (error) {
        if (!disposed && sharedEpoch.current === epoch) {
          setSharedHistory(null);
          setSharedError(error instanceof Error ? error.message : getSimulatorLabel(uiLanguage, "공유방 상태를 확인하지 못했습니다."));
        }
      } finally {
        clearTimeout(timeout);
        if (!disposed) timer = setTimeout(poll, 1500);
      }
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); sharedReadSequence.current += 1; };
  }, [sharedMode, sharedMember, botId]);

  async function connectSharedRoom(join: boolean) {
    if (sharedConnectGuard.current || sharedSendingBot || sharedSendingGeneral || !sharedStorageKey) return;
    sharedConnectGuard.current = true;
    const epoch = ++sharedEpoch.current;
    setSharedConnecting(true); setSharedError(""); setSharedSendError(""); setSharedHistory(null); setSharedMember(null);
    try {
      const member = join
        ? await joinAidotSharedRoom(window.location.origin, botId, sharedRoomInput.trim(), sharedName || authSession?.user.name || "사용자")
        : await createAidotSharedRoom(window.location.origin, botId, sharedName || authSession?.user.name || "사용자");
      if (sharedEpoch.current !== epoch) return;
      sessionStorage.setItem(sharedStorageKey, JSON.stringify({ roomId: member.roomId, participantId: member.participantId, conversationToken: member.conversationToken }));
      setSharedMember(member); setSharedRoomInput(member.roomId);
    } catch (error) {
      if (sharedEpoch.current === epoch) setSharedError(error instanceof Error ? error.message : getSimulatorLabel(uiLanguage, "공유방 연결에 실패했습니다."));
    } finally { sharedConnectGuard.current = false; if (sharedEpoch.current === epoch) setSharedConnecting(false); }
  }

  async function sendSharedMessage(value: string, addressed: boolean) {
    const member = sharedMember;
    const kind = addressed ? "bot" : "general";
    if (!member || !value.trim() || sharedSendGuard.current[kind] || (addressed ? !sharedCanAsk : !sharedCanChat)) return;
    const epoch = sharedEpoch.current;
    sharedSendGuard.current[kind] = true;
    if (addressed) setSharedSendingBot(true); else setSharedSendingGeneral(true);
    setSharedSendError(""); setInputValue("");
    try {
      await sendAidotSharedRoomMessage(window.location.origin, botId, member, value, addressed, AbortSignal.timeout(60000));
    } catch (error) {
      if (sharedEpoch.current === epoch) setSharedSendError(`${error instanceof Error ? error.message : getSimulatorLabel(uiLanguage, "메시지 전송을 확인하지 못했습니다.")} ${getSimulatorLabel(uiLanguage, "자동으로 다시 보내지 않습니다. 기록을 확인해 주세요.")}`);
    } finally {
      sharedSendGuard.current[kind] = false;
      if (sharedEpoch.current === epoch) {
        if (addressed) setSharedSendingBot(false); else setSharedSendingGeneral(false);
        try { await refreshSharedRoom(member, epoch); }
        catch { if (sharedEpoch.current === epoch) setSharedHistory(null); }
      }
    }
  }
  const simulatorSessionIdRef = useRef(crypto.randomUUID());
  const transcriptLogCursorRef = useRef({ sessionId: simulatorSessionIdRef.current, count: 0 });
  const intentFallbackCountRef = useRef(0);
  useEffect(() => {
    const session = loadAuthSession();
    if (!session) {
      router.replace("/login");
      return;
    }

    setAuthSession(session);
  }, [router]);

  useEffect(() => {
    if (!authSession) {
      return;
    }

    let ignore = false;
    const accessToken = authSession.access_token;

    async function loadBot() {
      setLoading(true);
      setErrorMessage("");
      try {
        const nextContext = await refreshStudioBotSelectedVersion(accessToken, botId, versionId, null, true);
        if (ignore) {
          return;
        }
        setBot(nextContext.bot);
        saveLastBotScreen(pathname);
      } catch (error) {
        if (ignore) {
          return;
        }
        setErrorMessage(error instanceof Error ? error.message : copy.loadFailed);
      } finally {
        if (!ignore) {
          setLoading(false);
        }
      }
    }

    void loadBot();

    return () => {
      ignore = true;
    };
  }, [authSession, botId, copy.loadFailed, pathname, versionId]);

  useEffect(() => {
    const sessionId = simulatorSessionIdRef.current;
    if (transcriptLogCursorRef.current.sessionId !== sessionId) {
      transcriptLogCursorRef.current = { sessionId, count: 0 };
    }
    if (messages.length <= transcriptLogCursorRef.current.count) {
      return;
    }
    const nextMessages = messages.slice(transcriptLogCursorRef.current.count);
    nextMessages
      .filter((message) => message.sender === "user" || isVisibleSimulatorMessage(message))
      .forEach((message) => {
        const participantKind = message.sender === "system" ? "system" : message.sender;
        writeSimulatorLog("debug", "simulator.transcript_message", {
          createdAt: new Date().toISOString(),
          participantKind,
          participantName: participantKind === "bot" ? (bot?.name ?? getSimulatorLabel(uiLanguage, "봇")) : participantKind === "user" ? getStudioRuntimeMessage(uiLanguage, "사용자") : getStudioRuntimeMessage(uiLanguage, "시스템"),
          message,
        });
      });
    transcriptLogCursorRef.current.count = messages.length;
  }, [bot?.name, messages]);

  const versionDocument = useMemo(
    () => normalizeVersionDocument(bot?.active_version?.version_json),
    [bot],
  );
  const apiAssets = useMemo(() => normalizeApiAssets(versionDocument.apis), [versionDocument.apis]);
  const versionSettings = useMemo(() => getBotVersionSettings(bot ?? {}), [bot]);
  const secondaryConfig = secondaryConfigForVersion(bot?.data_json, versionDocument.system_config);
  const runtimeLanguage = normalizeSupportedLanguage(bot?.data_json?.language);
  const runtimeCopy = SIMULATOR_CATALOGS[runtimeLanguage];
  const nluModel = useMemo(
    () =>
      trainIntentClassifier(versionDocument, bot?.data_json, {
        imbalanceOversampling: versionSettings.conversationDefaults.validation.imbalanceOversampling,
      }),
    [bot?.data_json, versionDocument, versionSettings.conversationDefaults.validation.imbalanceOversampling],
  );
  const simulatorRuntimeBlockMessage = useMemo(() => {
    const canRunSelectedNlu =
      (nluModel.type === "ml" && nluModel.model === "deep_learning_lite") ||
      nluModel.type === "llm" ||
      isSemanticNluType(nluModel.type);
    if (!canRunSelectedNlu) {
      return SIMULATOR_RUNTIME_MESSAGES.unsupportedNluMode;
    }
    const answerMode = normalizeAnswerMode(bot?.data_json?.answer_mode);
    const canRunSelectedAnswer = answerMode === "fixed" || answerMode === "semantic_rag" || answerMode === "llm_rag" || answerMode === "llm";
    if (!canRunSelectedAnswer) {
      return SIMULATOR_RUNTIME_MESSAGES.unsupportedAnswerMode;
    }
    return "";
  }, [bot?.data_json?.answer_mode, nluModel.model, nluModel.type]);
  const scoreCutoff = useMemo(
    () => normalizeScoreSetting(versionSettings.conversationDefaults.ml.cutOffScore),
    [versionSettings.conversationDefaults.ml.cutOffScore],
  );
  const similarIntentScore = useMemo(
    () => normalizeScoreSetting(versionSettings.conversationDefaults.ml.similarIntentScore),
    [versionSettings.conversationDefaults.ml.similarIntentScore],
  );
  const maxIntentResults = useMemo(
    () => Math.max(1, Math.round(versionSettings.conversationDefaults.ml.maxIntentResults || 3)),
    [versionSettings.conversationDefaults.ml.maxIntentResults],
  );
  const maxCardsBetweenUserResponses = useMemo(
    () => Math.max(1, Math.round(versionSettings.conversationDefaults.runtime.maxCardsBetweenUserResponses || 100)),
    [versionSettings.conversationDefaults.runtime.maxCardsBetweenUserResponses],
  );
  const dialogs = useMemo(() => getBotDialogs(bot), [bot]);
  const startDialogId =
    startDialogIdProp ||
    searchParams.get("dialogId") ||
    searchParams.get("intentId") ||
    searchParams.get("moduleId") ||
    "";
  const startDialog = useMemo(
    () => dialogs.find((dialog) => dialog.id === startDialogId) ?? null,
    [dialogs, startDialogId],
  );
  const selectedAnalysis = useMemo(
    () => analyses.find((item) => item.id === selectedAnalysisId) ?? analyses.at(-1) ?? null,
    [analyses, selectedAnalysisId],
  );
  const nluValidationDiagnostics = useMemo(
    () => buildNluValidationDiagnostics(versionDocument, nluModel, scoreCutoff, similarIntentScore, maxIntentResults, uiLanguage),
    [maxIntentResults, nluModel, scoreCutoff, similarIntentScore, uiLanguage, versionDocument],
  );
  const nluTrainingSupplementCandidates = useMemo(
    () =>
      nluValidationDiagnostics.filter(
        (item) => item.status !== "정상" && item.diagnosisType === "학습문장 부족" && item.utterance.trim(),
      ),
    [nluValidationDiagnostics],
  );
  const scenarioValidation = useMemo(() => {
    const versionValidation = bot?.active_version?.scenario_validation;
    const documentValidation = (versionDocument.system_config as { scenario_validation?: typeof versionValidation } | undefined)?.scenario_validation;
    return versionValidation ?? documentValidation ?? null;
  }, [bot?.active_version?.scenario_validation, versionDocument.system_config]);
  const blockedDialogIds = useMemo(
    () => new Set((scenarioValidation?.blocked_dialog_ids ?? []).map((value) => String(value))),
    [scenarioValidation],
  );

  function getScenarioBlockMessage(dialog: VersionDialogAsset) {
    if (!blockedDialogIds.has(dialog.id)) {
      return "";
    }
    const item = scenarioValidation?.items?.find((entry) => String(entry.dialog_id ?? "") === dialog.id);
    const detail = typeof item?.message === "string" ? item.message : getStudioRuntimeMessage(uiLanguage, "대화 설계 오류가 있습니다.");
    return formatStudioRuntimeMessage(uiLanguage, "{dialog} {type}는 {detail} 오류를 수정한 뒤 봇 테스트를 실행할 수 있습니다.", { dialog: dialog.name, type: getStudioRuntimeMessage(uiLanguage, dialog.dialogType === 0 ? "모듈" : "의도"), detail });
  }

  async function handleApplyNluTrainingSupplements() {
    if (!authSession || !bot?.active_version || nluSupplementSaving) {
      return;
    }

    const candidateMap = new Map<string, string[]>();
    nluTrainingSupplementCandidates.slice(0, 30).forEach((item) => {
      const text = item.utterance.trim();
      if (!text) {
        return;
      }
      candidateMap.set(item.dialogId, Array.from(new Set([...(candidateMap.get(item.dialogId) ?? []), text])));
    });

    if (candidateMap.size === 0) {
      setNluSupplementMessage(getStudioRuntimeMessage(uiLanguage, "추가할 학습문장 후보가 없습니다."));
      return;
    }

    const nextDialogs = versionDocument.dialogs.map((dialog) => {
      const candidates = candidateMap.get(dialog.id);
      if (!candidates || candidates.length === 0) {
        return dialog;
      }

      const existingTextKeys = new Set(dialog.utterances.map((utterance) => normalizeText(utterance.text)));
      const nextUtterances: VersionDialogUtterance[] = [...dialog.utterances];
      candidates.forEach((text) => {
        const textKey = normalizeText(text);
        if (existingTextKeys.has(textKey)) {
          return;
        }
        existingTextKeys.add(textKey);
        nextUtterances.push({
          id: crypto.randomUUID(),
          text,
          utteranceType: "T",
        });
      });

      if (nextUtterances.length === dialog.utterances.length) {
        return dialog;
      }

      return {
        ...dialog,
        utterances: nextUtterances,
        updatedAt: nowText(),
      };
    });

    const addedCount = nextDialogs.reduce(
      (count, dialog, index) => count + Math.max(0, dialog.utterances.length - versionDocument.dialogs[index].utterances.length),
      0,
    );

    if (addedCount === 0) {
      setNluSupplementMessage(getStudioRuntimeMessage(uiLanguage, "이미 같은 문장이 등록되어 있어 추가하지 않았습니다."));
      return;
    }

    setNluSupplementSaving(true);
    setNluSupplementMessage("");
    try {
      const response = await updateStudioBotVersionDialogs(authSession.access_token, bot.id, bot.active_version.id, nextDialogs);
      const updatedVersion: StudioBotVersionApiItem = {
        ...response.version,
        version_json: withUpdatedDialogs(versionDocument, normalizeVersionDialogs(response.items)),
        asset_counts: response.asset_counts,
        scenario_validation: response.scenario_validation,
      };
      setBot((current) => (current ? applyUpdatedVersionToBot(current, updatedVersion) : current));
      setNluSupplementMessage(formatStudioRuntimeMessage(uiLanguage, "학습문장 {count}건을 T 문장으로 추가했습니다. 학습하기를 다시 실행하세요.", { count: addedCount }));
    } catch (error) {
      setNluSupplementMessage(error instanceof Error ? error.message : getStudioRuntimeMessage(uiLanguage, "학습문장 보강 저장에 실패했습니다."));
    } finally {
      setNluSupplementSaving(false);
    }
  }

  function writeSimulatorLog(
    level: SimulatorLogLevel,
    event: string,
    detail: Record<string, unknown> = {},
  ) {
    postSimulatorLog({
      level,
      event,
      simulatorSessionId: simulatorSessionIdRef.current,
      clientTime: nowText(),
      botId: bot?.id ?? botId,
      botName: bot?.name,
      versionId,
      embedded,
      startDialogId,
      startDialogName: startDialog?.name ?? "",
      startDialogType: startDialog?.dialogType ?? null,
      runtime: runtime
        ? {
            dialogId: runtime.dialog.id,
            dialogName: runtime.dialog.name,
            dialogType: runtime.dialog.dialogType,
            nextNodeId: runtime.nextNodeId,
            waitingTalkNodeId: runtime.waitingTalkNodeId,
            ended: runtime.ended,
            variables: runtime.variables,
          }
        : null,
      analysisCount: analyses.length,
      messageCount: messages.length,
      detail,
    });
  }

  function isCurrentSimulatorSession(sessionId: string) {
    return simulatorSessionIdRef.current === sessionId;
  }

  const floatingButtons = useMemo(() => {
    const rawItems =
      versionSettings.floatingButtons.length > 0
        ? versionSettings.floatingButtons.filter((item) => item.enabled)
        : versionDocument.floating_buttons;
    if (!Array.isArray(rawItems)) {
      return [];
    }
    return rawItems
      .map((item) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) {
          return null;
        }
        const source = item as Record<string, unknown>;
        const label =
          typeof source.label === "string"
            ? source.label
            : typeof source.name === "string"
              ? source.name
              : "";
        const key = typeof source.key === "string" ? source.key : label;
        const actionValue = typeof source.actionValue === "string" ? source.actionValue : key;
        return label.trim() ? { label: label.trim(), key: actionValue.trim() || label.trim() } : null;
      })
      .filter((item): item is { label: string; key: string } => item !== null);
  }, [versionDocument.floating_buttons, versionSettings.floatingButtons]);

  useEffect(() => {
    if (!loading && bot && messages.length === 0) {
      startConversation();
    }
  }, [
    bot,
    loading,
    maxCardsBetweenUserResponses,
    messages.length,
    scoreCutoff,
    similarIntentScore,
    simulatorRuntimeBlockMessage,
    startDialog,
    versionDocument,
    versionSettings.messages.greeting,
  ]);

  useEffect(() => {
    window.setTimeout(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
    }, 50);
  }, [messages]);

  useEffect(() => {
    if (sharedMode) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [sharedMode, sharedHistory?.messages.at(-1)?.id]);

  function buildInitialVariables(dialog: VersionDialogAsset, graph: DialogFlowGraph, utterance: string) {
    const variables: Record<string, string> = {
      LAST_USER_MESSAGE: utterance,
    };

    detectEntities(versionDocument, dialog, utterance).forEach((entity) => {
      if (entity.matched) {
        const name = stripVariablePrefix(entity.variableName);
        setVariableValue(variables, name, entity.value, entity.targetValue);
      }
    });

    collectDialogFlowVariables(dialog, graph).forEach((variable) => {
      const name = stripVariablePrefix(variable.name);
      if (name && variables[name] == null && variable.value) {
        variables[name] = variable.value;
      }
    });

    return variables;
  }

  function startConversation() {
    simulatorSessionIdRef.current = crypto.randomUUID();
    intentFallbackCountRef.current = 0;
    if (simulatorRuntimeBlockMessage) {
      writeSimulatorLog("error", "simulator.runtime_unsupported", {
        reason: simulatorRuntimeBlockMessage,
        nluType: nluModel.type,
        nluModel: nluModel.model,
        answerMode: bot?.data_json?.answer_mode ?? "fixed",
      });
      setRuntime(null);
      setAnalyses([]);
      setSelectedAnalysisId(null);
      setLoadedScript(null);
      setPendingIntentSelection(null);
      setFloatingButtonsVisible(true);
      setInputValue("");
      setMessages([makeMessage("system", simulatorRuntimeBlockMessage)]);
      return;
    }
    writeSimulatorLog("info", "simulator.conversation_start", {
      startDialogId,
      greetingMode: versionSettings.messages.greeting.mode,
      greetingValue: versionSettings.messages.greeting.value,
    });
    const greetingMessage = getConfiguredTextMessage(versionSettings.messages.greeting);
    const preprocessModule = versionSettings.conversationDefaults.intentDetection.preprocessModule.trim();
    const startsWithPreprocessModule = !startDialog && Boolean(preprocessModule);
    const startsWithGreetingModule = !startDialog && !startsWithPreprocessModule && versionSettings.messages.greeting.mode === "module";
    const initialMessages: SimulatorMessage[] = startsWithGreetingModule
      ? []
      : [
          makeMessage(
            "bot",
            greetingMessage ||
              formatSimulatorText(runtimeCopy.defaultGreeting, { bot: bot?.name ?? getStudioRuntimeMessage(uiLanguage, "챗봇") }),
          ),
        ];

    if (startDialog) {
      initialMessages.push(
        makeMessage(
          "system",
          formatStudioRuntimeMessage(uiLanguage, "{type} '{dialog}' 기준으로 봇 테스트를 시작합니다.", { type: getStudioRuntimeMessage(uiLanguage, startDialog.dialogType === 0 ? "모듈" : "의도"), dialog: startDialog.name }),
        ),
      );
    }

    setRuntime(null);
    setAnalyses([]);
    setSelectedAnalysisId(null);
    setLoadedScript(null);
    setPendingIntentSelection(null);
    setFloatingButtonsVisible(true);
    setInputValue("");
    setMessages(initialMessages);

    if (startsWithPreprocessModule) {
      runModuleByName(preprocessModule, `startup-${crypto.randomUUID()}`);
    } else if (startsWithGreetingModule) {
      runConfiguredModule(versionSettings.messages.greeting, `startup-${crypto.randomUUID()}`);
    }

    if (startDialog?.dialogType === 0) {
      const graph = getDialogFlowGraph(versionDocument, startDialog);
      void continueRuntime(
        {
          dialog: startDialog,
          graph,
          nextNodeId: getGraphStartNodeId(graph),
          variables: {},
          ended: false,
        },
        `startup-${crypto.randomUUID()}`,
      );
    }
  }

  function appendAnalysisLog(
    analysisId: string,
    log: { cardType: string; cardName: string; description: string },
    variables: Record<string, string>,
    options: { visible?: boolean } = {},
  ) {
    writeSimulatorLog("debug", "simulator.analysis_log", {
      analysisId,
      log,
      variables,
      visible: options.visible !== false,
    });
    if (options.visible === false) {
      return;
    }
    const nextCardLog = { ...log, variables: { ...variables } };
    const nextSnapshot = { label: `${log.cardType} / ${log.cardName}`, variables: { ...variables } };
    setAnalyses((current) => {
      const existing = current.find((item) => item.id === analysisId);
      if (!existing) {
        return [
          ...current,
          {
            id: analysisId,
            utterance: "대화 시작",
            nluType: nluModel.type,
            selectedIntentName: runtime?.dialog.name ?? startDialog?.name ?? getStudioRuntimeMessage(uiLanguage, "시나리오 실행"),
            cutoffScore: scoreCutoff,
            similarIntentScore,
            scoreIn: [],
            scoreOut: [],
            entities: [],
            morphemes: [],
            cardLogs: [nextCardLog],
            variableSnapshots: [nextSnapshot],
          },
        ];
      }
      return current.map((item) =>
        item.id === analysisId
          ? {
              ...item,
              cardLogs: [...item.cardLogs, nextCardLog],
              variableSnapshots: [...item.variableSnapshots, nextSnapshot],
            }
          : item,
      );
    });
    setSelectedAnalysisId(analysisId);
  }

  function appendUserUtteranceLog(
    analysisId: string,
    utterance: string,
    selectedIntentName: string,
    resultType: "정상분류" | "의도 추출 오류" | "유사의도발생",
  ) {
    writeSimulatorLog("debug", "simulator.user_utterance", {
      analysisId,
      userUtterance: utterance,
      utterance,
      selectedIntentName,
      resultType,
      classificationMethod: "M/L",
    });
  }

  function refreshFormACardMessage(
    waitingNode: Extract<DialogFlowNode, { kind: "talk" }>,
    variables: Record<string, string>,
    analysisId: string,
  ) {
    if (waitingNode.config.messageType !== "form-a-card") {
      return false;
    }

    const resolvedMsgKey = replaceVariables(waitingNode.config.messages[1] ?? "", variables).trim();
    if (!resolvedMsgKey || (!resolvedMsgKey.startsWith("{") && !resolvedMsgKey.startsWith("["))) {
      writeSimulatorLog("debug", "simulator.form_a_card_msgkey_skip", {
        analysisId,
        nodeId: waitingNode.id,
        nodeTitle: waitingNode.title,
        resolvedMsgKey,
        reason: "MsgKey is not JSON content",
      });
      return false;
    }

    const refreshedTemplate = buildTemplateMessage(
      "adaptive-card",
      resolvedMsgKey,
      "Form(A Card)",
      waitingNode,
      resolvedMsgKey,
    );
    refreshedTemplate.debugLogs.forEach((log) => {
      writeSimulatorLog(log.level, log.event, {
        analysisId,
        ...log.detail,
        variables,
      });
      const visibleLog = describeSimulatorDebugLog(log);
      if (visibleLog) {
        appendAnalysisLog(analysisId, visibleLog, variables);
      }
    });
    if (!refreshedTemplate.valid) {
      return false;
    }

    setMessages((current) => {
      const targetIndex = current.findLastIndex(
        (message) =>
          message.sender === "bot" &&
          message.sourceTalkNodeId === waitingNode.id &&
          message.template?.kind === "adaptive-card",
      );
      if (targetIndex < 0) {
        return current;
      }

      return current.map((message, index) =>
        index === targetIndex
          ? {
              ...message,
              template: refreshedTemplate,
              sourceTalkNodeId: waitingNode.id,
            }
          : message,
      );
    });

    writeSimulatorLog("debug", "simulator.form_a_card_refreshed_in_place", {
      analysisId,
      nodeId: waitingNode.id,
      nodeTitle: waitingNode.title,
      variables,
    });
    return true;
  }

  function getConfiguredModuleDialog(item: MessageItemConfig) {
    if (!item.enabled || item.mode !== "module" || !item.value.trim()) {
      return null;
    }

    const moduleName = item.value.trim();
    return (
      dialogs.find(
        (dialog) =>
          dialog.dialogType === 0 &&
          (dialog.name === moduleName || dialog.displayName === moduleName || dialog.dialogKey === moduleName),
      ) ?? null
    );
  }

  function runConfiguredModule(item: MessageItemConfig, analysisId: string, variables: Record<string, string> = {}) {
    const moduleDialog = getConfiguredModuleDialog(item);
    if (!moduleDialog) {
      writeSimulatorLog("warn", "simulator.configured_module_missing", {
        analysisId,
        messageItem: item,
        variables,
      });
      return false;
    }

    const graph = getDialogFlowGraph(versionDocument, moduleDialog);
    writeSimulatorLog("info", "simulator.configured_module_start", {
      analysisId,
      moduleDialogId: moduleDialog.id,
      moduleDialogName: moduleDialog.name,
      startNodeId: getGraphStartNodeId(graph),
      variables,
    });
    void continueRuntime(
      {
        dialog: moduleDialog,
        graph,
        nextNodeId: getGraphStartNodeId(graph),
        variables,
        ended: false,
      },
      analysisId,
    );
    return true;
  }

  function startSecondaryRuntime(analysisId: string, variables: Record<string, string>) {
    if (!secondaryConfig || !secondaryOutcomeMapping(secondaryConfig)) return false;
    const dialog = dialogs.find(item => item.dialogType === 0 && item.id === secondaryConfig.module_id);
    if (!dialog) return false;
    const graph = getDialogFlowGraph(versionDocument, dialog);
    if (!graph.nodes.length) return false;
    void continueRuntime({dialog, graph, nextNodeId: getGraphStartNodeId(graph),
      variables: prepareSecondaryVariables(variables, secondaryConfig, normalizeRuntimeVariableName),
      ended: false, secondaryConfig: JSON.parse(JSON.stringify(secondaryConfig)), secondaryError: false}, analysisId);
    return true;
  }

  function finishSecondaryFallback(analysisId: string, variables: Record<string, string>, config: SecondaryModuleConfig) {
    intentFallbackCountRef.current += 1;
    const retryCount = Math.max(1, Math.round(versionSettings.conversationDefaults.intentDetection.retryCount || 2));
    const overflowModule = versionSettings.conversationDefaults.intentDetection.overflowModule.trim();
    const secondaryDialog = dialogs.find(item => item.id === config.module_id);
    const excluded = [config.module_id, secondaryDialog?.name, secondaryDialog?.displayName, secondaryDialog?.dialogKey];
    if (intentFallbackCountRef.current >= retryCount && overflowModule && !excluded.includes(overflowModule)
        && runModuleByName(overflowModule, analysisId, variables)) {
      intentFallbackCountRef.current = 0;
      return;
    }
    setMessages(current => [...current, makeMessage("bot", getConfiguredTextMessage(versionSettings.messages.fallback) || runtimeCopy.intentFallback, {analysisId})]);
    setRuntime(null);
  }

  function runModuleByName(moduleName: string, analysisId: string, variables: Record<string, string> = {}) {
    const normalizedModuleName = moduleName.trim();
    if (!normalizedModuleName) {
      return false;
    }

    const moduleDialog =
      dialogs.find(
        (dialog) =>
          dialog.dialogType === 0 &&
          (dialog.name === normalizedModuleName ||
            dialog.displayName === normalizedModuleName ||
            dialog.dialogKey === normalizedModuleName),
      ) ?? null;

    if (!moduleDialog) {
      writeSimulatorLog("warn", "simulator.named_module_missing", {
        analysisId,
        moduleName: normalizedModuleName,
        variables,
      });
      return false;
    }

    const graph = getDialogFlowGraph(versionDocument, moduleDialog);
    void continueRuntime(
      {
        dialog: moduleDialog,
        graph,
        nextNodeId: getGraphStartNodeId(graph),
        variables,
        ended: false,
      },
      analysisId,
    );
    return true;
  }

  async function executeFunctionNode(
    node: Extract<DialogFlowNode, { kind: "function" }>,
    variables: Record<string, string>,
    analysisId: string,
    sessionId: string,
  ) {
    const api = apiAssets.find((item) => item.id === node.config.apiId) ?? null;
    const method = api?.methods.find((item) => item.id === node.config.methodId) ?? null;
    const resultVariableName = stripVariablePrefix(node.config.resultVariableName) || "apiResult";

    if (!api || !method) {
      const message = getStudioRuntimeMessage(uiLanguage, "Function 카드의 API 또는 Method가 설정되어 있지 않습니다.");
      writeSimulatorLog("error", "simulator.function_config_missing", {
        analysisId,
        nodeId: node.id,
        nodeTitle: node.title,
        config: node.config,
        variables,
      });
      appendAnalysisLog(analysisId, {
        cardType: "실행 오류",
        cardName: node.title,
        description: message,
      }, variables);
      return { ok: false, variables, message };
    }

    const parameterValues = Object.fromEntries(
      method.parameters.map((parameter) => {
        const mapping = node.config.parameterMappings.find(
          (item) => item.parameterId === parameter.id || item.name === parameter.name,
        );
        return [parameter.name, replaceVariables(mapping?.value ?? parameter.defaultValue, variables)];
      }),
    );

    writeSimulatorLog("info", "simulator.function_call_start", {
      analysisId,
      nodeId: node.id,
      nodeTitle: node.title,
      apiId: api.id,
      apiName: api.name,
      methodId: method.id,
      methodName: method.name,
      parameterValues,
      variables,
    });

    try {
      const response = await fetch("/api/function/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api,
          methodId: method.id,
          parameterValues,
          variables,
        }),
      });
      const payload = await response.json().catch(() => null) as {
        ok?: boolean;
        status?: number;
        elapsedMs?: number;
        body?: unknown;
        text?: string;
        message?: string;
      } | null;

      if (!isCurrentSimulatorSession(sessionId)) {
        return { ok: false, variables, message: "" };
      }

      if (!response.ok || !payload?.ok) {
        const message = payload?.message || formatStudioRuntimeMessage(uiLanguage, "API 호출 실패({status})", { status: payload?.status ?? response.status });
        writeSimulatorLog("error", "simulator.function_call_failed", {
          analysisId,
          nodeId: node.id,
          nodeTitle: node.title,
          apiId: api.id,
          methodId: method.id,
          payload,
          variables,
        });
        appendAnalysisLog(analysisId, {
          cardType: "실행 오류",
          cardName: node.title,
          description: message,
        }, variables);
        return { ok: false, variables, message };
      }

      const resultValue =
        typeof payload.body === "string"
          ? payload.body
          : payload.body == null
            ? payload.text ?? ""
            : JSON.stringify(payload.body);
      const resultSource = payload.body ?? payload.text ?? "";
      const selectedOutputMappings = node.config.outputMappings.filter((mapping) =>
        stripVariablePrefix(mapping.variableName),
      );
      const mappedOutputVariables = Object.fromEntries(
        selectedOutputMappings.flatMap((mapping) => {
          const variableName = stripVariablePrefix(mapping.variableName);
          const outputValue =
            getFunctionOutputValue(payload, mapping.path || "root") ??
            getFunctionOutputValue(payload.body, mapping.path || "root");
          return flattenFunctionOutputVariables(variableName, outputValue);
        }),
      );
      const nextVariables =
        selectedOutputMappings.length > 0
          ? {
              ...variables,
              ...Object.fromEntries(flattenFunctionOutputVariables("result", resultSource)),
              ...mappedOutputVariables,
              result: resultValue,
              "result.__status": String(payload.status ?? 200),
              [`${resultVariableName}.__status`]: String(payload.status ?? 200),
            }
          : withFunctionResultAlias(variables, resultVariableName, resultValue, String(payload.status ?? 200), resultSource);
      const savedVariableNames =
        selectedOutputMappings.length > 0
          ? selectedOutputMappings
              .map((mapping) => formatVariableName(mapping.variableName))
              .filter(Boolean)
              .join(", ")
          : formatVariableName(resultVariableName);

      writeSimulatorLog("info", "simulator.function_call_success", {
        analysisId,
        nodeId: node.id,
        nodeTitle: node.title,
        apiId: api.id,
        methodId: method.id,
        status: payload.status,
        elapsedMs: payload.elapsedMs,
        resultVariableName: savedVariableNames,
        resultValue,
      });
      appendAnalysisLog(analysisId, {
        cardType: FLOW_CARD_LABELS[node.kind],
        cardName: node.title,
        description: formatStudioRuntimeMessage(uiLanguage, "{api} / {method} 호출 결과를 {variables} 변수에 저장했습니다.", { api: api.name, method: method.name, variables: savedVariableNames }),
      }, nextVariables);

      return { ok: true, variables: nextVariables, message: "" };
    } catch (error) {
      if (!isCurrentSimulatorSession(sessionId)) {
        return { ok: false, variables, message: "" };
      }
      const message = error instanceof Error ? error.message : SIMULATOR_RUNTIME_MESSAGES.functionException;
      writeSimulatorLog("error", "simulator.function_call_exception", {
        analysisId,
        nodeId: node.id,
        nodeTitle: node.title,
        apiId: api.id,
        methodId: method.id,
        error: message,
        variables,
      });
      appendAnalysisLog(analysisId, {
        cardType: "실행 오류",
        cardName: node.title,
        description: message,
      }, variables);
      return { ok: false, variables, message };
    }
  }

  async function continueRuntime(currentRuntime: RuntimeState, analysisId: string) {
    const sessionId = simulatorSessionIdRef.current;
    const nextMessages: SimulatorMessage[] = [];
    let nextRuntime = { ...currentRuntime };
    let secondaryError = currentRuntime.secondaryError === true;
    let guard = 0;

    while (!nextRuntime.ended && nextRuntime.nextNodeId && guard < maxCardsBetweenUserResponses) {
      guard += 1;
        const node = getNodeById(nextRuntime.graph, nextRuntime.nextNodeId);
        if (!node) {
          secondaryError = true;
          writeSimulatorLog("error", "simulator.next_node_missing", {
            analysisId,
            nextRuntime,
          });
          nextMessages.push(makeMessage("system", getStudioRuntimeMessage(runtimeLanguage, "다음 대화 카드가 연결되어 있지 않습니다.")));
        nextRuntime = { ...nextRuntime, ended: true, nextNodeId: "" };
        break;
      }

        appendAnalysisLog(analysisId, {
          cardType: FLOW_CARD_LABELS[node.kind],
          cardName: node.title,
          description: "카드를 실행했습니다.",
        }, nextRuntime.variables);

      if (node.kind === "talk") {
        const talkMessages = buildTalkMessages(node, nextRuntime.variables, runtimeLanguage);
        if (talkMessages.length === 0) {
          writeSimulatorLog("warn", "simulator.talk_empty_output", {
            analysisId,
            nodeId: node.id,
            nodeTitle: node.title,
            config: node.config,
            variables: nextRuntime.variables,
          });
          appendAnalysisLog(analysisId, {
            cardType: FLOW_CARD_LABELS[node.kind],
            cardName: node.title,
            description: "출력할 메시지가 없습니다. 변수 치환 결과나 Talk 카드 메시지 설정을 확인해주세요.",
          }, nextRuntime.variables);
        }
        talkMessages.forEach((message) => {
          message.debugLogs?.forEach((log) => {
            writeSimulatorLog(log.level, log.event, {
              analysisId,
              ...log.detail,
              variables: nextRuntime.variables,
            });
            const visibleLog = describeSimulatorDebugLog(log);
            if (visibleLog) {
              appendAnalysisLog(analysisId, visibleLog, nextRuntime.variables);
            }
          });
        });
        nextMessages.push(...talkMessages);
        const nextNodeId = getNextNodeId(nextRuntime.graph, node.id, "next");
        const channelConfig = getTalkConfigForChannel(node, "SM_CHAT");
        if (shouldWaitForTalkResponse(channelConfig, talkMessages)) {
          writeSimulatorLog("debug", "simulator.talk_wait", {
            analysisId,
            nodeId: node.id,
            nodeTitle: node.title,
            responseType: channelConfig.responseType,
            responseEntityExtractions: channelConfig.responseEntityExtractions,
            nextNodeId,
            variables: nextRuntime.variables,
          });
          nextRuntime = {
            ...nextRuntime,
            waitingTalkNodeId: node.id,
            nextNodeId,
          };
          appendAnalysisLog(analysisId, {
            cardType: FLOW_CARD_LABELS[node.kind],
            cardName: node.title,
            description: "사용자 입력을 기다립니다.",
          }, nextRuntime.variables);
          break;
        }
        nextRuntime = { ...nextRuntime, nextNodeId };
        continue;
      }

      if (node.kind === "variable") {
        const variables = { ...nextRuntime.variables };
        node.config.items.forEach((item) => {
          const name = stripVariablePrefix(item.variableName);
          const value = replaceVariables(item.value, variables);
          if (name) {
            variables[name] = value;
          }
          const itemId = item.id.trim();
          if (itemId) {
            variables[itemId] = value;
            variables[`${node.id}-${itemId}`] = value;
          }
        });
        appendAnalysisLog(analysisId, {
          cardType: FLOW_CARD_LABELS[node.kind],
          cardName: node.title,
          description: "변수값을 갱신했습니다.",
        }, variables);
        nextRuntime = { ...nextRuntime, variables, nextNodeId: getNextNodeId(nextRuntime.graph, node.id, "next") };
        continue;
      }

      if (node.kind === "condition") {
        const variableValue = resolveRuntimeVariableValue(nextRuntime.variables, node.config.variableName);
        const matchedBranch =
          node.config.branches.find((branch) =>
            branch.operator !== "else"
              ? evaluateCondition(branch.operator, variableValue, replaceVariables(branch.compareValue, nextRuntime.variables))
              : false,
          ) ?? node.config.branches.find((branch) => branch.operator === "else");
        if (!matchedBranch) {
          secondaryError = true;
          const detailMessage = SIMULATOR_RUNTIME_MESSAGES.conditionNoElseDetail;
          nextMessages.push(makeMessage("system", SIMULATOR_RUNTIME_MESSAGES.runtimeFlowError));
          writeSimulatorLog("error", "simulator.condition_no_else", {
            analysisId,
            nodeId: node.id,
            nodeTitle: node.title,
            message: detailMessage,
            variableName: node.config.variableName,
            variableValue,
            branches: node.config.branches,
            variables: nextRuntime.variables,
          });
          appendAnalysisLog(analysisId, {
            cardType: "실행 오류",
            cardName: node.title,
            description: detailMessage,
          }, nextRuntime.variables);
          nextRuntime = { ...nextRuntime, ended: true, nextNodeId: "", waitingTalkNodeId: undefined };
          continue;
        }
        const branchTargetNodeId = getNextNodeId(nextRuntime.graph, node.id, `branch:${matchedBranch.id}`);
        if (!branchTargetNodeId) {
          secondaryError = true;
          const detailMessage = SIMULATOR_RUNTIME_MESSAGES.conditionTargetMissingDetail;
          nextMessages.push(makeMessage("system", SIMULATOR_RUNTIME_MESSAGES.runtimeFlowError));
          writeSimulatorLog("error", "simulator.condition_target_missing", {
            analysisId,
            nodeId: node.id,
            nodeTitle: node.title,
            message: detailMessage,
            branchId: matchedBranch.id,
            branchLabel: matchedBranch.label,
            operator: matchedBranch.operator,
            variables: nextRuntime.variables,
          });
          appendAnalysisLog(analysisId, {
            cardType: "실행 오류",
            cardName: node.title,
            description: detailMessage,
          }, nextRuntime.variables);
          nextRuntime = { ...nextRuntime, ended: true, nextNodeId: "", waitingTalkNodeId: undefined };
          continue;
        }
        appendAnalysisLog(analysisId, {
          cardType: FLOW_CARD_LABELS[node.kind],
          cardName: node.title,
          description: formatStudioRuntimeMessage(uiLanguage, "분기 '{branch}' 조건으로 이동합니다.", { branch: matchedBranch.label || matchedBranch.operator }),
        }, nextRuntime.variables);
        nextRuntime = {
          ...nextRuntime,
          nextNodeId: branchTargetNodeId,
        };
        continue;
      }

      if (node.kind === "script") {
        const result = runScriptNode(node, nextRuntime.variables, uiLanguage);
        if (result.error) {
          secondaryError = true;
          writeSimulatorLog("error", "simulator.script_error", {
            analysisId,
            nodeId: node.id,
            nodeTitle: node.title,
            error: result.error,
            variables: nextRuntime.variables,
            changedVariables: result.changedVariables,
            consoleLogs: result.consoleLogs,
          });
        } else {
          writeSimulatorLog("debug", "simulator.script_executed", {
            analysisId,
            nodeId: node.id,
            nodeTitle: node.title,
            changedVariables: result.changedVariables,
            consoleLogs: result.consoleLogs,
          });
        }
        nextRuntime = {
          ...nextRuntime,
          variables: result.variables,
          nextNodeId: getNextNodeId(nextRuntime.graph, node.id, "next"),
        };
        appendAnalysisLog(analysisId, {
          cardType: FLOW_CARD_LABELS[node.kind],
          cardName: node.title,
          description: result.error
            ? formatStudioRuntimeMessage(uiLanguage, "Script 실행 오류가 발생했습니다: {error}", { error: result.error })
            : Object.keys(result.changedVariables).length > 0
              ? formatStudioRuntimeMessage(uiLanguage, "Script 실행 후 변수 {variables} 값을 반영했습니다.", { variables: Object.keys(result.changedVariables).map((name) => `$${name}`).join(", ") })
              : getStudioRuntimeMessage(uiLanguage, "Script를 실행했습니다. 변경된 변수값은 없습니다."),
        }, result.variables);
        continue;
      }

      if (node.kind === "function") {
        const result = await executeFunctionNode(node, nextRuntime.variables, analysisId, sessionId);
        if (!isCurrentSimulatorSession(sessionId)) {
          return;
        }
        if (!result.ok) {
          secondaryError = true;
          const exceptionNodeId = getNextNodeId(nextRuntime.graph, node.id, "exception");
          if (exceptionNodeId) {
            appendAnalysisLog(analysisId, {
              cardType: FLOW_CARD_LABELS[node.kind],
              cardName: node.title,
              description: "Function 실행 실패 후 예외 흐름으로 이동합니다.",
            }, result.variables);
            nextRuntime = {
              ...nextRuntime,
              variables: result.variables,
              nextNodeId: exceptionNodeId,
            };
            continue;
          }
          nextMessages.push(makeMessage("system", SIMULATOR_RUNTIME_MESSAGES.systemError));
          appendAnalysisLog(analysisId, {
            cardType: "실행 오류",
            cardName: node.title,
            description: "Function 실행 실패 후 예외 흐름이 없어 대화를 종료합니다.",
          }, result.variables);
          nextRuntime = {
            ...nextRuntime,
            variables: result.variables,
            ended: true,
            nextNodeId: "",
            waitingTalkNodeId: undefined,
          };
          continue;
        }
        nextRuntime = {
          ...nextRuntime,
          variables: result.variables,
          nextNodeId: getNextNodeId(nextRuntime.graph, node.id, "next"),
        };
        continue;
      }

      if (node.kind === "jump") {
        if (node.config.targetCardId) {
          const targetNode = getNodeByIdOrTitle(nextRuntime.graph, node.config.targetCardId);
          if (targetNode) {
            nextRuntime = { ...nextRuntime, nextNodeId: targetNode.id };
            continue;
          }
        }

        const targetDialog = dialogs.find((dialog) => dialog.id === node.config.targetDialogId || dialog.name === node.config.targetDialogName);
        if (targetDialog) {
          const scenarioBlockMessage = getScenarioBlockMessage(targetDialog);
          if (scenarioBlockMessage) {
            nextMessages.push(makeMessage("system", scenarioBlockMessage));
            appendAnalysisLog(analysisId, {
              cardType: "설계 오류",
              cardName: targetDialog.name,
              description: scenarioBlockMessage,
            }, nextRuntime.variables, { visible: true });
            writeSimulatorLog("error", "simulator.scenario_validation_blocked", {
              analysisId,
              dialogId: targetDialog.id,
              dialogName: targetDialog.name,
              sourceNodeId: node.id,
              sourceNodeTitle: node.title,
              scenarioValidation,
            });
            nextRuntime = { ...nextRuntime, ended: true, nextNodeId: "" };
            continue;
          }
          const targetGraph = getDialogFlowGraph(versionDocument, targetDialog);
          appendAnalysisLog(analysisId, {
            cardType: FLOW_CARD_LABELS[node.kind],
            cardName: node.title,
            description: formatStudioRuntimeMessage(uiLanguage, "{type} '{dialog}'로 이동합니다.", { type: getStudioRuntimeMessage(uiLanguage, targetDialog.dialogType === 0 ? "모듈" : "의도"), dialog: targetDialog.name }),
          }, nextRuntime.variables);
          nextRuntime = {
            ...nextRuntime,
            dialog: targetDialog,
            graph: targetGraph,
            nextNodeId: getGraphStartNodeId(targetGraph),
            transitionLocked: Boolean(nextRuntime.transitionLocked || targetDialog.transitionLocked),
            returnBlocked: targetDialog.returnBlocked,
          };
          continue;
        }

        secondaryError = true;
        const detailMessage = SIMULATOR_RUNTIME_MESSAGES.jumpTargetMissingDetail;
        nextMessages.push(makeMessage("system", SIMULATOR_RUNTIME_MESSAGES.runtimeModuleNotFound));
        writeSimulatorLog("error", "simulator.jump_target_missing", {
          analysisId,
          nodeId: node.id,
          nodeTitle: node.title,
          message: detailMessage,
          config: node.config,
          variables: nextRuntime.variables,
        });
        appendAnalysisLog(analysisId, {
          cardType: "실행 오류",
          cardName: node.title,
          description: detailMessage,
        }, nextRuntime.variables);
        nextRuntime = { ...nextRuntime, ended: true, nextNodeId: "", waitingTalkNodeId: undefined };
        continue;
      }

      if (node.kind === "end") {
        if (node.config.message.trim()) {
          nextMessages.push(makeMessage("bot", replaceVariables(node.config.message, nextRuntime.variables)));
        }
        const sessionEnded = node.config.endSessionImmediately === true;
        const beforeSessionEndModule = versionSettings.conversationDefaults.intentDetection.beforeSessionEndModule.trim();
        if (sessionEnded && beforeSessionEndModule && !nextRuntime.beforeSessionEndModuleRan) {
          const moduleDialog =
            dialogs.find(
              (dialog) =>
                dialog.dialogType === 0 &&
                (dialog.id === beforeSessionEndModule ||
                  dialog.name === beforeSessionEndModule ||
                  dialog.displayName === beforeSessionEndModule ||
                  dialog.dialogKey === beforeSessionEndModule),
            ) ?? null;
          if (moduleDialog) {
            const moduleGraph = getDialogFlowGraph(versionDocument, moduleDialog);
            appendAnalysisLog(analysisId, {
              cardType: FLOW_CARD_LABELS[node.kind],
              cardName: node.title,
              description: formatStudioRuntimeMessage(uiLanguage, "세션 종료 전 모듈 '{module}'을 실행합니다.", { module: moduleDialog.name }),
            }, nextRuntime.variables);
            nextRuntime = {
              ...nextRuntime,
              dialog: moduleDialog,
              graph: moduleGraph,
              nextNodeId: getGraphStartNodeId(moduleGraph),
              waitingTalkNodeId: undefined,
              beforeSessionEndModuleRan: true,
            };
            continue;
          }
          writeSimulatorLog("warn", "simulator.before_session_end_module_missing", {
            analysisId,
            moduleName: beforeSessionEndModule,
          });
        }
        const variables = sessionEnded && !nextRuntime.secondaryConfig ? {} : nextRuntime.variables;
        const restoredRuntime = !sessionEnded ? restoreRuntimeReturn({ ...nextRuntime, variables }) : null;
        if (restoredRuntime) {
          secondaryError = restoredRuntime.secondaryError === true;
          appendAnalysisLog(analysisId, {
            cardType: FLOW_CARD_LABELS[node.kind],
            cardName: node.title,
            description: formatStudioRuntimeMessage(uiLanguage, "대화 흐름이 종료되어 이전 {type} '{dialog}'로 복귀합니다.", { type: getStudioRuntimeMessage(uiLanguage, restoredRuntime.dialog.dialogType === 0 ? "모듈" : "의도"), dialog: restoredRuntime.dialog.name }),
          }, restoredRuntime.variables);
          if (restoredRuntime.waitingTalkNodeId) {
            nextRuntime = {
              ...restoredRuntime,
              nextNodeId: restoredRuntime.waitingTalkNodeId,
              waitingTalkNodeId: undefined,
            };
          } else {
            nextRuntime = restoredRuntime;
          }
          continue;
        }
        nextRuntime = {
          ...nextRuntime,
          variables,
          ended: true,
          sessionEnded,
          nextNodeId: "",
          waitingTalkNodeId: undefined,
        };
        appendAnalysisLog(analysisId, {
          cardType: FLOW_CARD_LABELS[node.kind],
          cardName: node.title,
          description: sessionEnded
            ? getStudioRuntimeMessage(uiLanguage, "세션이 종료되어 변수값을 초기화했습니다.")
            : getStudioRuntimeMessage(uiLanguage, "대화 흐름이 종료되었습니다. 세션 변수값은 유지됩니다."),
        }, variables);
        break;
      }

      nextRuntime = { ...nextRuntime, nextNodeId: getNextNodeId(nextRuntime.graph, node.id, "next") };
    }

    if (!isCurrentSimulatorSession(sessionId)) {
      return;
    }

    if (guard >= maxCardsBetweenUserResponses) {
          secondaryError = true;
      writeSimulatorLog("error", "simulator.guard_exceeded", {
        analysisId,
        nextRuntime,
        maxCardsBetweenUserResponses,
      });
      appendAnalysisLog(analysisId, {
        cardType: "실행 오류",
        cardName: "루핑 방지",
        description: formatStudioRuntimeMessage(uiLanguage, "사용자 응답 사이 실행 카드 수가 설정값 {count}개를 초과하여 시나리오를 중단했습니다. 링크 순환 또는 사용자 응답 대기 Talk 카드 누락 여부를 확인하세요.", { count: maxCardsBetweenUserResponses }),
      }, nextRuntime.variables);
      nextRuntime = { ...nextRuntime, ended: true, nextNodeId: "" };
    }

    if (!nextRuntime.ended && !nextRuntime.waitingTalkNodeId && !nextRuntime.nextNodeId) {
      nextRuntime = { ...nextRuntime, ended: true };
    }

    if (nextRuntime.secondaryConfig) {
      const config = nextRuntime.secondaryConfig;
      const result = evaluateSecondaryOutcome(config, nextRuntime.variables,
        {waiting: Boolean(nextRuntime.waitingTalkNodeId), executionError: secondaryError}, resolveRuntimeVariableValue);
      writeSimulatorLog("info", "simulator.secondary_result", {analysisId, status: result.status, moduleId: config.module_id});
      if (result.status === "unrecognized") {
        finishSecondaryFallback(analysisId, nextRuntime.sessionEnded ? {} : nextRuntime.variables, config);
        return;
      }
      if (result.status === "error") {
        setMessages(current => [...current, makeMessage("system", SIMULATOR_RUNTIME_MESSAGES.systemError, {analysisId})]);
        setRuntime(null);
        return;
      }
      nextRuntime = {...nextRuntime, secondaryError, secondaryConfig: result.status === "pending" ? config : undefined};
      if (result.status === "answered") {
        intentFallbackCountRef.current = 0;
        if (nextRuntime.sessionEnded) nextRuntime.variables = {};
      }
    }
    setMessages((current) => [...current, ...nextMessages]);
    setRuntime(nextRuntime);
  }

  function startDialogRuntime(
    utterance: string,
    selected: VersionDialogAsset,
    scores: IntentClassificationScore[] = [],
    options: {
      logUserUtterance?: boolean;
      variables?: Record<string, string>;
      decisionLog?: { cardType: string; cardName: string; description: string };
      parentRuntime?: RuntimeState | null;
      scoreCutoff?: number;
      similarIntentScore?: number;
    } = {},
  ) {
    const analysis = buildAnalysis(
      versionDocument,
      utterance,
      scores,
      selected,
      options.scoreCutoff ?? scoreCutoff,
      options.similarIntentScore ?? similarIntentScore,
      [],
      nluModel.type,
    );
    setAnalyses((current) => [...current, analysis]);
    setSelectedAnalysisId(analysis.id);
    if (options.decisionLog) {
      appendAnalysisLog(analysis.id, options.decisionLog, options.variables || {});
    }
    const scenarioBlockMessage = getScenarioBlockMessage(selected);
    if (scenarioBlockMessage) {
      appendUserUtteranceLog(analysis.id, utterance, selected.name, "의도 추출 오류");
      appendAnalysisLog(analysis.id, {
        cardType: "설계 오류",
        cardName: selected.name,
        description: scenarioBlockMessage,
      }, {}, { visible: true });
      writeSimulatorLog("error", "simulator.scenario_validation_blocked", {
        analysisId: analysis.id,
        dialogId: selected.id,
        dialogName: selected.name,
        scenarioValidation,
      });
      setMessages((current) => [...current, makeMessage("system", scenarioBlockMessage)]);
      setRuntime(null);
      return;
    }
    if (options.logUserUtterance !== false) {
      appendUserUtteranceLog(analysis.id, utterance, selected.name, "정상분류");
    }

    const graph = getDialogFlowGraph(versionDocument, selected);
    const nextNodeId = getGraphStartNodeId(graph);
    const nextRuntime: RuntimeState = {
      dialog: selected,
      graph,
      nextNodeId,
      variables: { ...buildInitialVariables(selected, graph, utterance), ...(options.variables || {}) },
      ended: false,
      returnStack: queueRuntimeReturn(options.parentRuntime ?? runtime, selected),
      transitionLocked:
        selected.transitionLocked ||
        Boolean((options.parentRuntime ?? runtime)?.transitionLocked) ||
        Boolean((options.parentRuntime ?? runtime)?.dialog.transitionLocked),
      returnBlocked: selected.returnBlocked,
    };
    void continueRuntime(nextRuntime, analysis.id);
  }

  async function buildAnswerRagRuntimeVariables(utterance: string, selected: VersionDialogAsset) {
    const answerMode = normalizeAnswerMode(bot?.data_json?.answer_mode);
    if (answerMode !== "semantic_rag" && answerMode !== "llm_rag" && answerMode !== "llm") {
      return {};
    }
    if (!authSession || !bot?.id || !bot.active_version?.id) {
      return {};
    }
    try {
      const requestPayload = {
        query: utterance,
        intentId: selected.id,
        intentName: selected.displayName || selected.name,
      };
      const result = answerMode === "llm"
        ? await generateStudioBotVersionLlmAnswer(authSession.access_token, bot.id, bot.active_version.id, requestPayload)
        : await searchStudioBotVersionRagAnswers(authSession.access_token, bot.id, bot.active_version.id, {
          ...requestPayload,
          topK: 3,
        });
      return normalizeRuntimeVariableValues(result.variables);
    } catch (error) {
      writeSimulatorLog("error", answerMode === "llm" ? "simulator.llm_answer_generate_failed" : "simulator.answer_rag_search_failed", {
        botId: bot?.id,
        versionId: bot?.active_version?.id,
        dialogId: selected.id,
        message: error instanceof Error ? error.message : String(error),
      });
      return {};
    }
  }

  async function startDialogRuntimeWithAnswerVariables(
    utterance: string,
    selected: VersionDialogAsset,
    scores: IntentClassificationScore[] = [],
    options: {
      logUserUtterance?: boolean;
      variables?: Record<string, string>;
      decisionLog?: { cardType: string; cardName: string; description: string };
      parentRuntime?: RuntimeState | null;
      scoreCutoff?: number;
      similarIntentScore?: number;
    } = {},
  ) {
    const ragVariables = await buildAnswerRagRuntimeVariables(utterance, selected);
    startDialogRuntime(utterance, selected, scores, {
      ...options,
      variables: { ...(options.variables || {}), ...ragVariables },
    });
  }

  function findExactingMatchingDialog(utterance: string) {
    if (versionSettings.conversationDefaults.exactingMatching.enabled === false) {
      return null;
    }
    const normalizedUtterance = normalizeText(utterance);
    if (!normalizedUtterance) {
      return null;
    }
    return versionDocument.dialogs.find((dialog) =>
      dialog.dialogType === 1 &&
      dialog.utterances.some((item) => normalizeText(item.text) === normalizedUtterance),
    ) ?? null;
  }

  function applyBlocklistPatterns(utterance: string) {
    let effectiveUtterance = utterance;

    versionSettings.blocklists.forEach((item) => {
      const pattern = item.pattern.trim();
      if (!item.enabled || !pattern) {
        return;
      }

      if (item.type === "regex") {
        try {
          const expression = createRuntimeRegex(pattern);
          const flags = expression.flags.includes("g") ? expression.flags : `${expression.flags}g`;
          effectiveUtterance = effectiveUtterance.replace(new RegExp(expression.source, flags), " ");
        } catch {
          return;
        }
      } else {
        effectiveUtterance = effectiveUtterance.replace(new RegExp(escapeRegExp(pattern), "gi"), " ");
      }
    });

    return effectiveUtterance.replace(/\s+/g, " ").trim();
  }

  function findSmalltalk(utterance: string) {
    if (!versionSettings.smalltalk.enabled) {
      return null;
    }
    const normalizedUtterance = normalizeText(utterance);
    for (const item of versionSettings.smalltalk.items) {
      if (!item.enabled) {
        continue;
      }
      const userMessages = smalltalkTextValues(item.userMessages, item.utterance);
      const botMessages = smalltalkTextValues(item.botMessages, item.response);
      const matchedUtterance = userMessages.find((message) => normalizeText(message) === normalizedUtterance);
      if (matchedUtterance && botMessages.length > 0) {
        return {
          ...item,
          utterance: matchedUtterance,
          response: pickSmalltalkResponse(botMessages),
          userMessages,
          botMessages,
        };
      }
    }
    return null;
  }

  function smalltalkTextValues(value: unknown, fallback = "") {
    const rawValues = Array.isArray(value) ? value : typeof value === "string" ? [value] : [];
    const values = rawValues
      .map((item) => String(item ?? "").trim())
      .filter(Boolean);
    const fallbackText = fallback.trim();
    if (values.length === 0 && fallbackText) {
      values.push(fallbackText);
    }
    return Array.from(new Set(values));
  }

  function pickSmalltalkResponse(values: string[]) {
    return values[Math.floor(Math.random() * values.length)] ?? "";
  }

  async function classifyMlIntentForSimulator(utterance: string): Promise<{
    scores: IntentClassificationScore[];
    scoreCutoff: number;
    similarIntentScore: number;
    maxIntentResults: number;
  }> {
    if (!authSession || !bot?.id || !bot.active_version?.id) {
      return { scores: [], scoreCutoff, similarIntentScore, maxIntentResults };
    }
    const result = await testStudioBotVersionMlIntent(authSession.access_token, bot.id, bot.active_version.id, {
      utterance,
      top_k: maxIntentResults,
    });
    const scores = result.candidates
      .map((candidate) => {
        const dialog = dialogs.find(
          (item) =>
            item.id === candidate.intent_id ||
            item.id === candidate.intentId ||
            item.name === candidate.intent_name ||
            item.displayName === candidate.intent_name ||
            item.name === candidate.intentName ||
            item.displayName === candidate.intentName,
        );
        if (!dialog) {
          return null;
        }
        const confidence = Number(candidate.confidence || 0);
        return {
          dialog,
          score: Math.round(Math.max(0, Math.min(1, confidence)) * 10000) / 100,
          features: candidate.reason ? [candidate.reason] : ["DeepLearning Lite"],
        };
      })
      .filter((item): item is IntentClassificationScore => item !== null)
      .sort((left, right) => right.score - left.score);

    return {
      scores,
      scoreCutoff: Math.max(0, Math.min(100, Number(result.cutoff_score ?? scoreCutoff / 100) * 100)),
      similarIntentScore: Math.max(0, Math.min(100, Number(result.similar_intent_score ?? similarIntentScore / 100) * 100)),
      maxIntentResults: Math.max(1, Number(result.max_intent_results ?? maxIntentResults)),
    };
  }
  async function classifyLlmIntentForSimulator(utterance: string): Promise<IntentClassificationScore[]> {
    if (!authSession || !bot?.id || !bot.active_version?.id) {
      return [];
    }
    const result = await testStudioBotVersionLlmIntent(authSession.access_token, bot.id, bot.active_version.id, {
      utterance,
      top_k: maxIntentResults,
    });
    return result.candidates
      .map((candidate) => {
        const dialog = dialogs.find(
          (item) =>
            item.id === candidate.intent_id ||
            item.id === candidate.intentId ||
            item.name === candidate.intent_name ||
            item.displayName === candidate.intent_name ||
            item.name === candidate.intentName ||
            item.displayName === candidate.intentName,
        );
        if (!dialog) {
          return null;
        }
        const confidence = Number(candidate.confidence || 0);
        return {
          dialog,
          score: Math.round(Math.max(0, Math.min(1, confidence)) * 10000) / 100,
          features: candidate.reason ? [candidate.reason] : ["LLM"],
        };
      })
      .filter((item): item is IntentClassificationScore => item !== null)
      .sort((left, right) => right.score - left.score);
  }

  async function classifySemanticIntentForSimulator(utterance: string): Promise<IntentClassificationScore[]> {
    if (!authSession || !bot?.id || !bot.active_version?.id) {
      return [];
    }
    const result = await testStudioBotVersionSemanticIntent(authSession.access_token, bot.id, bot.active_version.id, {
      utterance,
      top_k: maxIntentResults,
    });
    return result.candidates
      .map((candidate) => {
        const dialog = dialogs.find(
          (item) =>
            item.id === candidate.intent_id ||
            item.id === candidate.intentId ||
            item.name === candidate.intent_name ||
            item.displayName === candidate.intent_name ||
            item.name === candidate.intentName ||
            item.displayName === candidate.intentName,
        );
        if (!dialog) {
          return null;
        }
        const confidence = Number(candidate.confidence || 0);
        return {
          dialog,
          score: Math.round(Math.max(0, Math.min(1, confidence)) * 10000) / 100,
          features: candidate.reason ? [candidate.reason] : ["Semantic"],
        };
      })
      .filter((item): item is IntentClassificationScore => item !== null)
      .sort((left, right) => right.score - left.score);
  }

  function findRuleTargetDialog(utterance: string) {
    const rule = versionSettings.rules.find((item) => {
      const expression = item.expression.trim();
      if (!item.enabled || !expression || !item.target.trim()) {
        return false;
      }
      try {
        return createRuntimeRegex(expression).test(utterance);
      } catch {
        return normalizeText(utterance).includes(normalizeText(expression));
      }
    });
    if (!rule) {
      return null;
    }
    const dialog = dialogs.find(
      (dialog) =>
        dialog.id === rule.target ||
        dialog.name === rule.target ||
        dialog.displayName === rule.target ||
        dialog.dialogKey === rule.target,
    ) ?? null;
    return dialog ? { rule, dialog } : null;
  }

  async function startIntentRuntime(
    utterance: string,
    variables: Record<string, string> = {},
    options: {
      skipStartDialog?: boolean;
      suppressFallbackOnNoMatch?: boolean;
      fallbackRuntime?: RuntimeState;
      fallbackNextNodeId?: string;
      fallbackCardName?: string;
      fallbackDescription?: string;
    } = {},
  ) {
    const activeRuntime = options.fallbackRuntime ?? runtime;
    if (activeRuntime && !activeRuntime.ended && (activeRuntime.transitionLocked || activeRuntime.dialog.transitionLocked)) {
      return false;
    }
    if (startDialog && options.skipStartDialog !== true) {
      await startDialogRuntimeWithAnswerVariables(utterance, startDialog, [], { variables, parentRuntime: activeRuntime });
      return true;
    }

    const effectiveUtterance = applyBlocklistPatterns(utterance);

    const smalltalk = findSmalltalk(utterance);
    if (smalltalk) {
      const analysis = buildAnalysis(versionDocument, utterance, [], null, scoreCutoff, similarIntentScore, [], nluModel.type);
      setAnalyses((current) => [...current, analysis]);
      setSelectedAnalysisId(analysis.id);
      appendAnalysisLog(analysis.id, {
        cardType: "스몰토크",
        cardName: smalltalk.title,
        description: "스몰토크에 매칭되어 응답을 반환합니다.",
      }, variables);
      setMessages((current) => [...current, makeMessage("bot", smalltalk.response)]);
      setRuntime(null);
      return true;
    }

    const exactDialog = findExactingMatchingDialog(effectiveUtterance);
    if (exactDialog) {
      intentFallbackCountRef.current = 0;
      await startDialogRuntimeWithAnswerVariables(effectiveUtterance, exactDialog, [], {
        variables,
        parentRuntime: activeRuntime,
        decisionLog: {
          cardType: "Exacting Matching",
          cardName: exactDialog.name,
          description: "사용자 발화가 등록된 학습문장과 완전히 일치하여 Score 계산 전에 해당 의도로 진입했습니다.",
        },
      });
      return true;
    }

    const ruleMatch = findRuleTargetDialog(utterance);
    if (ruleMatch) {
      intentFallbackCountRef.current = 0;
      await startDialogRuntimeWithAnswerVariables(utterance, ruleMatch.dialog, [], {
        variables,
        parentRuntime: activeRuntime,
        decisionLog: {
          cardType: "룰",
          cardName: ruleMatch.rule.name,
          description: formatStudioRuntimeMessage(uiLanguage, "룰 표현식 \"{rule}\"이 사용자 발화와 매칭되어 \"{intent}\" 의도로 진입했습니다.", { rule: ruleMatch.rule.expression, intent: ruleMatch.dialog.name }),
        },
      });
      return true;
    }

    let scores: IntentClassificationScore[] = [];
    let effectiveScoreCutoff = scoreCutoff;
    let effectiveSimilarIntentScore = similarIntentScore;
    let effectiveMaxIntentResults = maxIntentResults;
    if (nluModel.type === "ml") {
      try {
        const result = await classifyMlIntentForSimulator(effectiveUtterance);
        scores = result.scores;
        effectiveScoreCutoff = result.scoreCutoff;
        effectiveSimilarIntentScore = result.similarIntentScore;
        effectiveMaxIntentResults = result.maxIntentResults;
      } catch (error) {
        if (options.suppressFallbackOnNoMatch) {
          return false;
        }
        const analysis = buildAnalysis(versionDocument, utterance, [], null, scoreCutoff, similarIntentScore, [], nluModel.type);
        setAnalyses((current) => [...current, analysis]);
        setSelectedAnalysisId(analysis.id);
        appendAnalysisLog(analysis.id, {
          cardType: "ML NLU",
          cardName: "의도분류",
          description: error instanceof Error ? error.message : getStudioRuntimeMessage(uiLanguage, "ML 의도분류 중 오류가 발생했습니다."),
        }, variables);
        setMessages((current) => [
          ...current,
          makeMessage(secondaryConfig ? "system" : "bot", secondaryConfig ? SIMULATOR_RUNTIME_MESSAGES.systemError : getConfiguredTextMessage(versionSettings.messages.fallback) || runtimeCopy.intentFallback),
        ]);
        setRuntime(null);
        return true;
      }
    } else if (isSemanticNluType(nluModel.type)) {
      try {
        scores = await classifySemanticIntentForSimulator(effectiveUtterance);
      } catch (error) {
        if (options.suppressFallbackOnNoMatch) {
          return false;
        }
        const analysis = buildAnalysis(versionDocument, utterance, [], null, scoreCutoff, similarIntentScore, [], nluModel.type);
        setAnalyses((current) => [...current, analysis]);
        setSelectedAnalysisId(analysis.id);
        appendAnalysisLog(analysis.id, {
          cardType: "Semantic NLU",
          cardName: "의도분류",
          description: error instanceof Error ? error.message : getStudioRuntimeMessage(uiLanguage, "Semantic 의도분류 중 오류가 발생했습니다."),
        }, variables);
        setMessages((current) => [
          ...current,
          makeMessage(secondaryConfig ? "system" : "bot", secondaryConfig ? SIMULATOR_RUNTIME_MESSAGES.systemError : getConfiguredTextMessage(versionSettings.messages.fallback) || runtimeCopy.intentFallback),
        ]);
        setRuntime(null);
        return true;
      }
    } else if (nluModel.type === "llm") {
      try {
        scores = await classifyLlmIntentForSimulator(effectiveUtterance);
      } catch (error) {
        if (options.suppressFallbackOnNoMatch) {
          return false;
        }
        const analysis = buildAnalysis(versionDocument, utterance, [], null, scoreCutoff, similarIntentScore, [], nluModel.type);
        setAnalyses((current) => [...current, analysis]);
        setSelectedAnalysisId(analysis.id);
        appendAnalysisLog(analysis.id, {
          cardType: "LLM NLU",
          cardName: "의도분류",
          description: error instanceof Error ? error.message : getStudioRuntimeMessage(uiLanguage, "LLM 의도분류 중 오류가 발생했습니다."),
        }, variables);
        setMessages((current) => [
          ...current,
          makeMessage(secondaryConfig ? "system" : "bot", secondaryConfig ? SIMULATOR_RUNTIME_MESSAGES.systemError : getConfiguredTextMessage(versionSettings.messages.fallback) || runtimeCopy.intentFallback),
        ]);
        setRuntime(null);
        return true;
      }
    }
    const selectedScore = scores[0] ?? null;
    const selectedDialog = selectedScore && selectedScore.score >= effectiveScoreCutoff ? selectedScore.dialog : null;
    const similarCandidates = getSimilarCandidates(
      scores,
      effectiveScoreCutoff,
      effectiveSimilarIntentScore,
      effectiveMaxIntentResults,
    );
    const isAmbiguous = selectedDialog !== null && similarCandidates.length > 1;

    if (selectedDialog && !isAmbiguous) {
      intentFallbackCountRef.current = 0;
      await startDialogRuntimeWithAnswerVariables(effectiveUtterance, selectedDialog, scores, {
        variables,
        parentRuntime: activeRuntime,
        scoreCutoff: effectiveScoreCutoff,
        similarIntentScore: effectiveSimilarIntentScore,
      });
      return true;
    }

    const analysis = buildAnalysis(versionDocument, utterance, scores, null, effectiveScoreCutoff, effectiveSimilarIntentScore, [], nluModel.type);
    setAnalyses((current) => [...current, analysis]);
    setSelectedAnalysisId(analysis.id);

    if (isAmbiguous) {
      appendUserUtteranceLog(analysis.id, utterance, similarCandidates[0]?.dialog.name ?? "유사의도", "유사의도발생");
      const candidates = similarCandidates;
      const candidatePayload = candidates.map((item) => ({
        id: item.dialog.id,
        name: item.dialog.name,
        displayName: item.dialog.displayName,
        score: item.score,
      }));
      const nluVariables = {
        ...variables,
        nluCandidates: JSON.stringify(candidatePayload),
        nluTopIntent: candidates[0]?.dialog.name ?? "",
        nluSecondIntent: candidates[1]?.dialog.name ?? "",
      };

      if (
        runModuleByName(
          versionSettings.conversationDefaults.intentDetection.multiIntentButtonModule,
          analysis.id,
          nluVariables,
        )
      ) {
        setPendingIntentSelection(null);
        return true;
      }

      const guide = versionSettings.messages.multiIntentGuide;
      const noIntentButtonLabel = guide.noIntentButtonLabel || getStudioRuntimeMessage(runtimeLanguage, "원하는 의도 없음");
      const noIntentButtonMessage = guide.noIntentButtonMessage || getStudioRuntimeMessage(runtimeLanguage, "다시 질문해주시면 다른 의도를 찾겠습니다.");
      setPendingIntentSelection({
        scores,
        candidates,
        noIntentButtonLabel,
        noIntentButtonMessage,
      });
      setMessages((current) => [
        ...current,
        makeMessage("bot", guide.enabled ? guide.message : runtimeCopy.multiIntentGuide, {
          analysisId: analysis.id,
          quickReplies: [
            ...candidates.map((item) => item.dialog.displayName || item.dialog.name),
            noIntentButtonLabel,
          ],
        }),
      ]);
      setRuntime(null);
      return true;
    }

    if (options.suppressFallbackOnNoMatch) {
      return false;
    }

    if (secondaryConfig) {
      if (!startSecondaryRuntime(analysis.id, {...variables, input: utterance})) {
        setMessages(current => [...current, makeMessage("system", SIMULATOR_RUNTIME_MESSAGES.systemError, {analysisId: analysis.id})]);
        setRuntime(null);
      }
      return true;
    }

    appendUserUtteranceLog(analysis.id, utterance, "의도 미분류", "의도 추출 오류");
    intentFallbackCountRef.current += 1;
    const retryCount = Math.max(1, Math.round(versionSettings.conversationDefaults.intentDetection.retryCount || 2));
    if (intentFallbackCountRef.current >= retryCount) {
      const overflowModule = versionSettings.conversationDefaults.intentDetection.overflowModule.trim();
      if (overflowModule && runModuleByName(overflowModule, analysis.id, variables)) {
        intentFallbackCountRef.current = 0;
        return true;
      }
    }
    if (options.fallbackRuntime && options.fallbackNextNodeId) {
      const fallbackVariables = { ...options.fallbackRuntime.variables, ...variables };
      const fallbackMessage = getConfiguredTextMessage(versionSettings.messages.fallback);
      setMessages((current) => [
        ...current,
        makeMessage(
          "bot",
          fallbackMessage || runtimeCopy.intentFallback,
          { analysisId: analysis.id },
        ),
      ]);
      appendAnalysisLog(analysis.id, {
        cardType: "의도 미분류",
        cardName: options.fallbackCardName || "사용자 입력",
        description: options.fallbackDescription || "의도분류에 실패하여 안내 메시지를 출력한 뒤 원래 카드 흐름의 다음 링크로 진행합니다.",
      }, fallbackVariables);
      void continueRuntime({
        ...options.fallbackRuntime,
        variables: fallbackVariables,
        waitingTalkNodeId: undefined,
        ended: false,
        nextNodeId: options.fallbackNextNodeId,
      }, analysis.id);
      return true;
    }

    const fallbackMessage = getConfiguredTextMessage(versionSettings.messages.fallback);
    setMessages((current) => [
      ...current,
      makeMessage(
        "bot",
        fallbackMessage || runtimeCopy.intentFallback,
        { analysisId: analysis.id },
      ),
    ]);
    setRuntime(null);
    return true;
  }

  function parseJsonInput(value: string) {
    try {
      return JSON.parse(value) as Record<string, unknown>;
    } catch {
      return null;
    }
  }

  function richFormResultVariables(message: SimulatorMessage, value: string) {
    if (!message.sourceTalkNodeId) return {} as Record<string, string>;
    const sourceNode = runtime ? getNodeById(runtime.graph, message.sourceTalkNodeId) : null;
    if (sourceNode?.kind !== "talk") return {} as Record<string, string>;
    const config = getTalkConfigForChannel(sourceNode, "SM_CHAT");
    const isFormMessage = config.messageType === "form" || config.messageType === "form-a-card";
    const shouldStoreFormResult = config.responseType === "form-relay" || config.responseType === "single-select";
    if (!isFormMessage || !shouldStoreFormResult) {
      return {} as Record<string, string>;
    }
    const variableName = stripVariablePrefix(config.responseVariableName);
    if (!variableName) return {} as Record<string, string>;
    const parsed = parseJsonInput(value);
    const parsedRecord = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
    const response = parsedRecord && "response" in parsedRecord ? parsedRecord.response : (parsed ?? value);
    const payload = parsedRecord && "webchatRichFormVersion" in parsedRecord && "response" in parsedRecord
      ? parsedRecord
      : { webchatRichFormVersion: "1.0", response };
    const variables: Record<string, string> = {};
    flattenRuntimeValue(variableName, payload, variables);
    if (response != null) {
      flattenRuntimeValue(`${variableName}.response`, response, variables);
      if (typeof response === "object" && !Array.isArray(response)) {
        Object.entries(response as Record<string, unknown>).forEach(([key, childValue]) => {
          flattenRuntimeValue(`${variableName}.${key}`, childValue, variables);
        });
      }
    }
    return variables;
  }

  function richFormResultDisplayValue(value: string) {
    const parsed = parseJsonInput(value);
    const response = parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? ("response" in parsed ? (parsed.response as unknown) : parsed)
      : null;
    if (response && typeof response === "object" && !Array.isArray(response)) {
      const record = response as Record<string, unknown>;
      if (typeof record.buttonValue === "string" && record.buttonValue.trim()) {
        return record.buttonValue.trim();
      }
      for (const item of Object.values(record)) {
        if (item && typeof item === "object" && !Array.isArray(item)) {
          const itemValue = (item as Record<string, unknown>).value;
          if (typeof itemValue === "string" && itemValue.trim()) {
            return itemValue.trim();
          }
        }
      }
    }
    return value;
  }

  function isEmptyRichFormResultPayload(value: string) {
    const parsed = parseJsonInput(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return false;
    const record = parsed as Record<string, unknown>;
    if (!("webchatRichFormVersion" in record) || !("response" in record)) return false;
    const response = record.response;
    return Boolean(response && typeof response === "object" && !Array.isArray(response) && Object.keys(response as Record<string, unknown>).length === 0);
  }
  function submitTemplateAction(message: SimulatorMessage, value: string) {
    if (sharedMode) {
      if (!canUseMessageCard(message)) return;
      let parsed: unknown;
      try { parsed = JSON.parse(value); } catch { parsed = { response: value }; }
      const envelope = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : { response: parsed };
      void sendSharedMessage(JSON.stringify({ ...envelope, sourceTalkNodeId: message.sourceTalkNodeId }), true);
      return;
    }
    const variables = richFormResultVariables(message, value);
    const sourceNode = message.sourceTalkNodeId && runtime ? getNodeById(runtime.graph, message.sourceTalkNodeId) : null;
    const sourceConfig = sourceNode?.kind === "talk" ? getTalkConfigForChannel(sourceNode, "SM_CHAT") : null;
    const isTemplateAction =
      sourceNode?.kind === "talk" &&
      Boolean(message.template) &&
      (message.template?.kind === "rich-form" || message.template?.kind === "adaptive-card");

    if (runtime && sourceNode?.kind === "talk" && isTemplateAction) {
      const fallbackNextNodeId = getNextNodeId(runtime.graph, sourceNode.id, "next");
      const sourceResponseType = String(sourceConfig?.responseType || "none");
      const displayValue = richFormResultDisplayValue(value);
      setInputValue("");

      if (sourceResponseType === "single-select") {
        const showUserBubble = Boolean(displayValue.trim()) && !isEmptyRichFormResultPayload(value);
        if (showUserBubble) {
          setMessages((current) => [...current, makeMessage("user", displayValue)]);
        }
        const analysisId = selectedAnalysisId || analyses.at(-1)?.id || crypto.randomUUID();
        void continueRuntime({
          ...runtime,
          variables: { ...runtime.variables, ...variables },
          waitingTalkNodeId: undefined,
          ended: false,
          nextNodeId: fallbackNextNodeId,
        }, analysisId).finally(() => {
          advanceLoadedScript();
        });
        return;
      }

      const analysisId = selectedAnalysisId || analyses.at(-1)?.id || crypto.randomUUID();
      void continueRuntime({
        ...runtime,
        variables: { ...runtime.variables, ...variables },
        waitingTalkNodeId: undefined,
        ended: false,
        nextNodeId: fallbackNextNodeId,
      }, analysisId);
      advanceLoadedScript();
      return;
    }

    submitUtterance(value, variables);
  }
  function submitUtterance(
    rawUtterance: string,
    initialVariables: Record<string, string> = {},
    sourceTalkNodeId?: string,
  ) {
    const utterance = rawUtterance.trim();
    if (sharedMode) {
      if (sourceTalkNodeId && !canRespondAidotRoomCard(sharedHistory?.runtime, sourceTalkNodeId, sharedCanAsk)) return;
      const wire = sourceTalkNodeId
        ? JSON.stringify({ webchatRichFormVersion: "1.0", sourceTalkNodeId, response: { buttonValue: utterance } })
        : utterance;
      void sendSharedMessage(wire, true); return;
    }
    if (!utterance || loading) {
      return;
    }

    if (simulatorRuntimeBlockMessage) {
      writeSimulatorLog("error", "simulator.runtime_unsupported", {
        reason: simulatorRuntimeBlockMessage,
        nluType: nluModel.type,
        nluModel: nluModel.model,
        answerMode: bot?.data_json?.answer_mode ?? "fixed",
      });
      setRuntime(null);
      setPendingIntentSelection(null);
      setMessages((current) => [...current, makeMessage("system", simulatorRuntimeBlockMessage)]);
      return;
    }

    setMessages((current) => [...current, makeMessage("user", utterance)]);
    setInputValue("");
    writeSimulatorLog("debug", "simulator.user_message", {
      utterance,
      userUtterance: utterance,
      sourceTalkNodeId: sourceTalkNodeId || "",
      waitingTalkNodeId: runtime?.waitingTalkNodeId || "",
      pendingIntentSelection: Boolean(pendingIntentSelection),
    });

    if (pendingIntentSelection && !runtime) {
      const matchedCandidate =
        pendingIntentSelection.candidates.find((item) => {
          const names = [item.dialog.name, item.dialog.displayName, item.dialog.dialogKey]
            .map((name) => normalizeText(name))
            .filter(Boolean);
          return names.includes(normalizeText(utterance));
        }) ?? null;

      if (matchedCandidate) {
        setPendingIntentSelection(null);
        void startDialogRuntimeWithAnswerVariables(utterance, matchedCandidate.dialog, pendingIntentSelection.scores, {
          logUserUtterance: false,
        }).finally(() => {
          advanceLoadedScript();
        });
        return;
      }

      if (normalizeText(utterance) === normalizeText(pendingIntentSelection.noIntentButtonLabel)) {
        setPendingIntentSelection(null);
        setMessages((current) => [
          ...current,
          makeMessage("bot", pendingIntentSelection.noIntentButtonMessage),
        ]);
        setRuntime(null);
        advanceLoadedScript();
        return;
      }

      setPendingIntentSelection(null);
    }

    const fallbackTalkNodeId =
      sourceTalkNodeId && runtime && !runtime.ended ? sourceTalkNodeId : undefined;
    const effectiveWaitingTalkNodeId =
      runtime?.waitingTalkNodeId || fallbackTalkNodeId;
    const runtimeForStructuredResponse =
      runtime && !runtime.ended && effectiveWaitingTalkNodeId
        ? {
            ...runtime,
            waitingTalkNodeId: effectiveWaitingTalkNodeId,
            nextNodeId: runtime.nextNodeId || getNextNodeId(runtime.graph, effectiveWaitingTalkNodeId, "next"),
          }
        : null;

    const waitingTalkAllowsIntentTransition =
      !sourceTalkNodeId &&
      runtimeForStructuredResponse?.waitingTalkNodeId &&
      !runtimeForStructuredResponse.ended &&
      !runtimeForStructuredResponse.dialog.transitionLocked &&
      (() => {
        const waitingNode = getNodeById(runtimeForStructuredResponse.graph, runtimeForStructuredResponse.waitingTalkNodeId);
        const waitingConfig = waitingNode?.kind === "talk" ? getTalkConfigForChannel(waitingNode, "SM_CHAT") : null;
        if (waitingNode?.kind !== "talk" || !waitingConfig) {
          return false;
        }
        if (!talkResponseTypeSupportsIntentInterrupt(waitingConfig.responseType)) {
          return false;
        }
        return waitingConfig.intentTransitionLocked !== true;
      })();

    if (waitingTalkAllowsIntentTransition && runtimeForStructuredResponse) {
      void startIntentRuntime(
        utterance,
        { ...runtimeForStructuredResponse.variables, ...initialVariables },
        {
          skipStartDialog: true,
          suppressFallbackOnNoMatch: true,
        },
      ).then((handled) => {
        if (handled) {
          advanceLoadedScript();
          return;
        }
        const waitingNode = getNodeById(runtimeForStructuredResponse.graph, runtimeForStructuredResponse.waitingTalkNodeId!);
        const waitingConfig = waitingNode?.kind === "talk" ? getTalkConfigForChannel(waitingNode, "SM_CHAT") : null;
        if (
          waitingNode?.kind === "talk" &&
          waitingConfig?.responseType === "single-select" &&
          waitingConfig.messages.filter((item) => item.trim()).length > 0
        ) {
          const options = waitingConfig.messages.map((item) => item.trim()).filter(Boolean);
          const matched =
            versionSettings.conversationDefaults.buttonSelection.option === "contains"
              ? options.some((option) => utterance.includes(option))
              : options.some((option) => option === utterance);
          const enforceSingleSelectMatch =
            !runtimeForStructuredResponse.transitionLocked &&
            !runtimeForStructuredResponse.dialog.transitionLocked &&
            waitingConfig.intentTransitionLocked !== true;
          if (!matched && enforceSingleSelectMatch) {
            const analysis = buildAnalysis(
              versionDocument,
              utterance,
              [],
              runtimeForStructuredResponse.dialog,
              scoreCutoff,
              similarIntentScore,
              [],
              nluModel.type,
            );
            if (runConfiguredModule(versionSettings.messages.buttonMismatch, analysis.id, runtimeForStructuredResponse.variables)) {
              setAnalyses((current) => [...current, analysis]);
              setSelectedAnalysisId(analysis.id);
              advanceLoadedScript();
              return;
            }
            const mismatchMessage = getConfiguredTextMessage(versionSettings.messages.buttonMismatch);
            setMessages((current) => [
              ...current,
              makeMessage("bot", mismatchMessage || getStudioRuntimeMessage(runtimeLanguage, "목록에 있는 버튼 중 하나를 선택해주세요."), {
                quickReplies: options,
                sourceTalkNodeId: runtimeForStructuredResponse.waitingTalkNodeId,
              }),
            ]);
            advanceLoadedScript();
            return;
          }
        }
        const variables = { ...runtimeForStructuredResponse.variables };
        const extractedEntities: EntityMatchResult[] = [];
        const entityExtractionLogs: string[] = [];
        if (waitingNode?.kind === "talk") {
          const responseName = stripVariablePrefix(waitingNode.config.responseVariableName);
          if (responseName && waitingNode.config.responseType !== "extract-entity") {
            setVariableValue(variables, responseName, utterance, utterance);
          }
          if (waitingConfig?.responseType === "extract-entity" && waitingConfig.responseEntityExtractions.length === 0) {
            entityExtractionLogs.push("응답 내 개체 추출 설정은 켜져 있지만 추출할 개체 목록이 비어 있습니다.");
          }
          (waitingConfig?.responseEntityExtractions || []).forEach((item) => {
            const name = stripVariablePrefix(item.variableName);
            const entityMatch = matchEntityValue(versionDocument, item.entityId, item.entityName, utterance);
            const normalizedEntities = normalizeVersionEntities(versionDocument.entities);
            const configuredEntity = normalizedEntities.find(
              (entity) =>
                entity.id === item.entityId ||
                entity.name.replace(/^@/, "").trim() === item.entityName.replace(/^@/, "").trim(),
            );
            const entityCandidates =
              configuredEntity?.rows.flatMap((row) =>
                [row.value, ...row.details].map((candidate) => candidate.normalize("NFKC").trim()).filter(Boolean),
              ) ?? [];
            extractedEntities.push({
              name: item.entityName,
              variableName: formatVariableName(item.variableName) || `$${item.variableName}`,
              value: entityMatch?.value ?? "",
              targetValue: entityMatch?.target ?? "",
              matched: Boolean(name && entityMatch),
            });
            entityExtractionLogs.push(
              entityMatch
                ? `${item.entityName} → ${formatVariableName(item.variableName) || item.variableName}: '${entityMatch.value}' 추출, target '${entityMatch.target}'`
                : `${item.entityName} → ${formatVariableName(item.variableName) || item.variableName}: 추출 실패(entityId=${item.entityId || "-"}, rows=${configuredEntity?.rows.length ?? 0}, values=${
                    configuredEntity?.rows
                      .map((row) => `${row.value}[${row.details.join(", ")}]`)
                      .join(", ") || "-"
                  }, candidates=${entityCandidates.join(", ") || "-"}, utterance=${utterance})`,
            );
            if (name && entityMatch) {
              setVariableValue(variables, name, entityMatch.value, entityMatch.target);
            }
          });
        }
        const analysis = buildAnalysis(
          versionDocument,
          utterance,
          [],
          runtimeForStructuredResponse.dialog,
          scoreCutoff,
          similarIntentScore,
          extractedEntities,
          nluModel.type,
        );
        setAnalyses((current) => [...current, analysis]);
        setSelectedAnalysisId(analysis.id);
        const extractedCount = extractedEntities.filter((entity) => entity.matched).length;
        appendAnalysisLog(analysis.id, {
          cardType: "사용자 입력",
          cardName: waitingNode?.title ?? getStudioRuntimeMessage(uiLanguage, "Talk 응답"),
          description:
            extractedEntities.length > 0
              ? formatStudioRuntimeMessage(uiLanguage, "Talk 카드가 받은 사용자 입력을 변수에 반영했습니다. 개체 추출 {extracted}/{total}건.", { extracted: extractedCount, total: extractedEntities.length })
              : getStudioRuntimeMessage(uiLanguage, "Talk 카드가 받은 사용자 입력을 변수에 반영했습니다."),
        }, { ...variables, talkResponse: utterance, responseUtterance: utterance });
        if (entityExtractionLogs.length > 0) {
          appendAnalysisLog(analysis.id, {
            cardType: "개체 추출",
            cardName: waitingNode?.title ?? getStudioRuntimeMessage(uiLanguage, "Talk 응답"),
            description: entityExtractionLogs.join(" / "),
          }, variables);
        }
        if (waitingNode?.kind === "talk" && refreshFormACardMessage(waitingNode, variables, analysis.id)) {
          appendAnalysisLog(analysis.id, {
            cardType: "Form(A Card)",
            cardName: waitingNode.title,
            description: "MsgKey로 같은 Adaptive Card 메시지를 갱신하고 입력 대기 상태를 유지했습니다.",
          }, variables);
          setRuntime({
            ...runtimeForStructuredResponse,
            variables,
            waitingTalkNodeId: waitingNode.id,
          });
          advanceLoadedScript();
          return;
        }
        void continueRuntime({ ...runtimeForStructuredResponse, variables, waitingTalkNodeId: undefined }, analysis.id);
        advanceLoadedScript();
      });
      return;
    }

    if (runtimeForStructuredResponse?.waitingTalkNodeId && !runtimeForStructuredResponse.ended) {
      const waitingNode = getNodeById(runtimeForStructuredResponse.graph, runtimeForStructuredResponse.waitingTalkNodeId);
      const waitingConfig = waitingNode?.kind === "talk" ? getTalkConfigForChannel(waitingNode, "SM_CHAT") : null;
      if (
        waitingNode?.kind === "talk" &&
        waitingConfig?.responseType === "single-select" &&
        waitingConfig.messages.filter((item) => item.trim()).length > 0
      ) {
        const options = waitingConfig.messages.map((item) => item.trim()).filter(Boolean);
        const matched =
          versionSettings.conversationDefaults.buttonSelection.option === "contains"
            ? options.some((option) => utterance.includes(option))
            : options.some((option) => option === utterance);
        const enforceSingleSelectMatch =
          !runtimeForStructuredResponse.transitionLocked &&
          !runtimeForStructuredResponse.dialog.transitionLocked &&
          waitingConfig.intentTransitionLocked !== true;
        if (!matched && enforceSingleSelectMatch) {
          const analysis = buildAnalysis(
            versionDocument,
            utterance,
            [],
            runtimeForStructuredResponse.dialog,
            scoreCutoff,
            similarIntentScore,
            [],
            nluModel.type,
          );
          if (runConfiguredModule(versionSettings.messages.buttonMismatch, analysis.id, runtimeForStructuredResponse.variables)) {
            setAnalyses((current) => [...current, analysis]);
            setSelectedAnalysisId(analysis.id);
            return;
          }
          const mismatchMessage = getConfiguredTextMessage(versionSettings.messages.buttonMismatch);
          setMessages((current) => [
            ...current,
            makeMessage("bot", mismatchMessage || getStudioRuntimeMessage(runtimeLanguage, "목록에 있는 버튼 중 하나를 선택해주세요."), {
              quickReplies: options,
              sourceTalkNodeId: runtimeForStructuredResponse.waitingTalkNodeId,
            }),
          ]);
          return;
        }
      }
      const variables = { ...runtimeForStructuredResponse.variables };
      const extractedEntities: EntityMatchResult[] = [];
      const entityExtractionLogs: string[] = [];
      if (waitingNode?.kind === "talk") {
        const responseName = stripVariablePrefix(waitingNode.config.responseVariableName);
        if (responseName && waitingNode.config.responseType !== "extract-entity") {
          setVariableValue(variables, responseName, utterance, utterance);
        }
        if (waitingConfig?.responseType === "extract-entity" && waitingConfig.responseEntityExtractions.length === 0) {
          entityExtractionLogs.push("응답 내 개체 추출 설정은 켜져 있지만 추출할 개체 목록이 비어 있습니다.");
        }
        (waitingConfig?.responseEntityExtractions || []).forEach((item) => {
          const name = stripVariablePrefix(item.variableName);
          const entityMatch = matchEntityValue(versionDocument, item.entityId, item.entityName, utterance);
            const normalizedEntities = normalizeVersionEntities(versionDocument.entities);
            const configuredEntity = normalizedEntities.find(
              (entity) =>
                entity.id === item.entityId ||
                entity.name.replace(/^@/, "").trim() === item.entityName.replace(/^@/, "").trim(),
            );
            const entityCandidates =
              configuredEntity?.rows.flatMap((row) =>
                [row.value, ...row.details].map((candidate) => candidate.normalize("NFKC").trim()).filter(Boolean),
              ) ?? [];
            extractedEntities.push({
              name: item.entityName,
              variableName: formatVariableName(item.variableName) || `$${item.variableName}`,
            value: entityMatch?.value ?? "",
            targetValue: entityMatch?.target ?? "",
            matched: Boolean(name && entityMatch),
          });
          entityExtractionLogs.push(
            entityMatch
              ? `${item.entityName} → ${formatVariableName(item.variableName) || item.variableName}: '${entityMatch.value}' 추출, target '${entityMatch.target}'`
              : `${item.entityName} → ${formatVariableName(item.variableName) || item.variableName}: 추출 실패(entityId=${item.entityId || "-"}, rows=${configuredEntity?.rows.length ?? 0}, values=${
                  configuredEntity?.rows
                    .map((row) => `${row.value}[${row.details.join(", ")}]`)
                    .join(", ") || "-"
                }, candidates=${entityCandidates.join(", ") || "-"}, utterance=${utterance})`,
          );
          if (name && entityMatch) {
            setVariableValue(variables, name, entityMatch.value, entityMatch.target);
          }
        });
      }
      const analysis = buildAnalysis(
        versionDocument,
        utterance,
        [],
        runtimeForStructuredResponse.dialog,
        scoreCutoff,
        similarIntentScore,
        extractedEntities,
        nluModel.type,
      );
      setAnalyses((current) => [...current, analysis]);
      setSelectedAnalysisId(analysis.id);
      const extractedCount = extractedEntities.filter((entity) => entity.matched).length;
      appendAnalysisLog(analysis.id, {
        cardType: "사용자 입력",
        cardName: waitingNode?.title ?? getStudioRuntimeMessage(uiLanguage, "Talk 응답"),
        description:
          extractedEntities.length > 0
            ? formatStudioRuntimeMessage(uiLanguage, "Talk 카드가 받은 사용자 입력을 변수에 반영했습니다. 개체 추출 {extracted}/{total}건.", { extracted: extractedCount, total: extractedEntities.length })
            : getStudioRuntimeMessage(uiLanguage, "Talk 카드가 받은 사용자 입력을 변수에 반영했습니다."),
      }, { ...variables, talkResponse: utterance, responseUtterance: utterance });
      if (entityExtractionLogs.length > 0) {
        appendAnalysisLog(analysis.id, {
          cardType: "개체 추출",
          cardName: waitingNode?.title ?? getStudioRuntimeMessage(uiLanguage, "Talk 응답"),
          description: entityExtractionLogs.join(" / "),
        }, variables);
      }
      if (waitingNode?.kind === "talk" && refreshFormACardMessage(waitingNode, variables, analysis.id)) {
        appendAnalysisLog(analysis.id, {
          cardType: "Form(A Card)",
          cardName: waitingNode.title,
          description: "MsgKey로 같은 Adaptive Card 메시지를 갱신하고 입력 대기 상태를 유지했습니다.",
        }, variables);
        setRuntime({
          ...runtimeForStructuredResponse,
          variables,
          waitingTalkNodeId: waitingNode.id,
        });
        advanceLoadedScript();
        return;
      }
      void continueRuntime({ ...runtimeForStructuredResponse, variables, waitingTalkNodeId: undefined }, analysis.id);
      advanceLoadedScript();
      return;
    }

    void startIntentRuntime(utterance, initialVariables).finally(() => {
      advanceLoadedScript();
    });
  }

  function handleSubmit(event?: FormEvent) {
    event?.preventDefault();
    const utterance = inputValue.trim();
    submitUtterance(utterance);
  }

  function handleAdaptiveCardRenderError(message: SimulatorMessage, error: string) {
    writeSimulatorLog("error", "simulator.adaptive_card_render_failed", {
      messageId: message.id,
      sourceTalkNodeId: message.sourceTalkNodeId,
      schemaVersion: message.template?.schemaVersion,
      error,
    });
    if (sharedMode) return;
    const analysisId = selectedAnalysisId || analyses.at(-1)?.id;
    if (analysisId) {
      appendAnalysisLog(analysisId, {
        cardType: "Adaptive Card 오류",
        cardName: message.template?.title ?? "Form(A Card)",
        description: formatStudioRuntimeMessage(uiLanguage, "Adaptive Card 렌더링 실패: {error}", { error }),
      }, runtime?.variables ?? {});
    }
  }

  function advanceLoadedScript() {
    setLoadedScript((current) => {
      if (!current?.active) {
        return current;
      }
      const nextIndex = current.index + 1;
      const nextValue = current.sentences[nextIndex] ?? "";
      window.setTimeout(() => {
        setInputValue(nextValue);
        inputRef.current?.focus();
      }, 0);
      return {
        ...current,
        index: nextIndex,
        active: nextIndex < current.sentences.length,
      };
    });
  }

  function handleInputChange(value: string) {
    if (!sharedMode && loadedScript?.active && value !== (loadedScript.sentences[loadedScript.index] ?? "")) {
      const keepEditing = window.confirm(getStudioRuntimeMessage(uiLanguage, "불러온 대화 진행을 중단하시겠습니까?"));
      if (keepEditing) {
        setLoadedScript((current) => (current ? { ...current, active: false } : current));
      } else {
        return;
      }
    }
    setInputValue(value);
  }

  function handleRestart() {
    if (sharedMode) { void connectSharedRoom(false); return; }
    startConversation();
  }

  function handleDownloadConversation() {
    const payload = {
      version: 1,
      bot: bot?.name ?? "",
      savedAt: nowText(),
      messages: displayedMessages,
      analyses: sharedMode ? [] : analyses,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `simulator-conversation-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  async function handleUploadConversation(file: File) {
    if (sharedMode) return;
    try {
      const text = await file.text();
      const parsed = JSON.parse(text) as unknown;
      const sentences = parseLoadedConversation(parsed);
      if (sentences.length === 0) {
        setErrorMessage(getSimulatorLabel(uiLanguage, "불러올 사용자 문장이 없습니다."));
        return;
      }
      setLoadedScript({
        fileName: file.name,
        sentences,
        index: 0,
        active: true,
      });
      setInputValue(sentences[0] ?? "");
      setErrorMessage("");
      inputRef.current?.focus();
    } catch {
      setErrorMessage(getSimulatorLabel(uiLanguage, "JSON 대화 파일을 읽지 못했습니다."));
    } finally {
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }

  function renderAnalysisList(title: string, rows: SimulatorAnalysis["scoreIn"]) {
    return (
      <div className="simulator-analysis__block">
        <strong>{title}</strong>
        {rows.length > 0 ? (
          <div className="simulator-analysis__rows">
            {rows.map((row) => (
              <div key={`${title}-${row.intentName}`} className="simulator-analysis__row">
                <span>{row.intentName}</span>
                <b>{row.score}</b>
                <small>{row.features.join(", ") || "-"}</small>
              </div>
            ))}
          </div>
        ) : (
          <p>{getSimulatorLabel(uiLanguage, "대상 없음")}</p>
        )}
      </div>
    );
  }

  function getIntentClassificationReason(analysis: SimulatorAnalysis) {
    const decision = getAppliedIntentDecision(analysis);
    if (decision.kind !== "nlu") {
      return decision.reason;
    }

    const rows = [...analysis.scoreIn, ...analysis.scoreOut];
    const top = rows[0] ?? null;
    const second = rows[1] ?? null;

    if (analysis.nluType === "ml") {
      const trainingCount = nluModel.deepLearningLite.documents.length;
      const trainedIntentCount = nluModel.deepLearningLite.dialogCentroids.length;
      if (trainingCount === 0 || trainedIntentCount === 0) {
        return getStudioRuntimeMessage(uiLanguage, "학습 문장이 없어 의도 분류를 수행할 수 없습니다.");
      }
    }

    if (!top) {
      if (isSemanticNluType(analysis.nluType)) {
        return getStudioRuntimeMessage(uiLanguage, "Semantic 검색 후보가 없어 의도 분류를 수행할 수 없습니다.");
      }
      if (analysis.nluType === "llm") {
        return getStudioRuntimeMessage(uiLanguage, "LLM 의도분류 후보가 없습니다.");
      }
      return getStudioRuntimeMessage(uiLanguage, "입력 발화에서 분류에 사용할 토큰을 찾지 못했습니다.");
    }

    if (analysis.selectedIntentName === "의도 미분류") {
      if (top.score < analysis.cutoffScore) {
        return formatStudioRuntimeMessage(uiLanguage, "최고 점수 {top}점이 Cut-off {cutoff}점 미만입니다.", { top: top.score, cutoff: analysis.cutoffScore });
      }

      if (
        top &&
        second &&
        second.score >= analysis.cutoffScore &&
        second.score >= getSimilarIntentThreshold(top.score, analysis.similarIntentScore)
      ) {
        return formatStudioRuntimeMessage(uiLanguage, "상위 후보가 1순위 점수 대비 유사의도 기준 {score}% 안에 포함되어 의도를 확정하지 않았습니다.", { score: analysis.similarIntentScore });
      }

      return getStudioRuntimeMessage(uiLanguage, "분류 후보는 있으나 선택 가능한 의도 조건을 만족하지 못했습니다.");
    }

    return formatStudioRuntimeMessage(uiLanguage, "{intent} 의도가 Cut-off 기준을 통과했습니다.", { intent: analysis.selectedIntentName });
  }

  function getAppliedIntentDecision(analysis: SimulatorAnalysis) {
    const preNluLog = analysis.cardLogs.find((log) =>
      (PRE_NLU_DECISION_LOG_TYPES as readonly string[]).includes(log.cardType),
    );
    if (preNluLog) {
      return {
        kind: "pre-nlu" as const,
        method: preNluLog.cardType,
        target: preNluLog.cardName,
        reason: preNluLog.description,
      };
    }

    if (isSemanticNluType(analysis.nluType)) {
      return {
        kind: "nlu" as const,
        method: getStudioRuntimeMessage(uiLanguage, "시멘틱"),
        target: analysis.selectedIntentName,
        reason: getStudioRuntimeMessage(uiLanguage, "선처리 단계에서 매칭된 항목이 없어 Semantic 검색 후보를 기준으로 의도를 판정했습니다."),
      };
    }

    if (analysis.nluType === "llm") {
      return {
        kind: "nlu" as const,
        method: "LLM",
        target: analysis.selectedIntentName,
        reason: getStudioRuntimeMessage(uiLanguage, "선처리 단계에서 매칭된 항목이 없어 LLM 의도분류 결과를 기준으로 의도를 판정했습니다."),
      };
    }

    return {
      kind: "nlu" as const,
      method: "ML",
      target: analysis.selectedIntentName,
      reason: getStudioRuntimeMessage(uiLanguage, "선처리 단계에서 매칭된 항목이 없어 ML Score 계산 결과를 기준으로 의도를 판정했습니다."),
    };
  }

  function renderIntentDecisionSummary(analysis: SimulatorAnalysis) {
    const decision = getAppliedIntentDecision(analysis);
    return (
      <div className="simulator-analysis__block simulator-analysis__block--decision">
        <strong>{getSimulatorLabel(uiLanguage, "의도 인식 적용 단계")}</strong>
        <div className="simulator-analysis__manual-card">
          <strong>{decision.method}</strong>
          <div>
            <span>{getSimulatorLabel(uiLanguage, "적용 대상")}</span>
            <b>{decision.target}</b>
          </div>
          <small>{decision.reason}</small>
        </div>
        <p className="simulator-analysis__hint">{getSimulatorLabel(uiLanguage, "판정 순서: 제외/무시 → 스몰토크 → Exacting Matching → 룰 → ML/시멘틱/LLM. 앞 단계가 적용되면 이후 단계는 실행하지 않습니다.")}</p>
      </div>
    );
  }

  function formatScoreRate(score: number) {
    return `${score.toFixed(2)}%`;
  }

  function formatScoreSetting(value: number) {
    return value <= 1 ? value.toFixed(1) : (value / 100).toFixed(2);
  }

  function renderManualScoreRows(rows: SimulatorAnalysis["scoreIn"], emptyText: string) {
    if (rows.length === 0) {
      return <p>{emptyText}</p>;
    }

    return (
      <div className="simulator-analysis__score-table">
        {rows.map((row) => (
          <div key={`manual-score-${row.intentName}`} className="simulator-analysis__score-item">
            <span>{getSimulatorLabel(uiLanguage, "의도")}</span>
            <strong>{row.intentName}</strong>
            <span>{getSimulatorLabel(uiLanguage, "스코어/확률")}</span>
            <strong>{formatScoreRate(row.score)}</strong>
            <span>Features</span>
            <strong>{row.features.length > 0 ? row.features.join(", ") : "-"}</strong>
          </div>
        ))}
      </div>
    );
  }

  function renderNluValidationDiagnostics() {
    if (nluValidationDiagnostics.length === 0) {
      return (
        <div className="simulator-analysis__manual-section">
          <strong>{getSimulatorLabel(uiLanguage, "검증 문장 진단")}</strong>
          <p>{getSimulatorLabel(uiLanguage, "등록된 Validation 문장이 없습니다.")}</p>
        </div>
      );
    }

    const problemCount = nluValidationDiagnostics.filter((item) => item.status !== "정상").length;
    const diagnosisCounts = nluValidationDiagnostics.reduce<Record<NluValidationDiagnostic["diagnosisType"], number>>(
      (counts, item) => ({
        ...counts,
        [item.diagnosisType]: counts[item.diagnosisType] + 1,
      }),
      { "공통 Feature 충돌": 0, "학습문장 부족": 0, "Score 설정 후보": 0, 정상: 0 },
    );
    return (
      <div className="simulator-analysis__manual-section">
        <strong>{getSimulatorLabel(uiLanguage, "검증 문장 진단")}</strong>
        <p>
          {formatStudioRuntimeMessage(uiLanguage, "V 문장 {total}건 중 보완 후보 {problems}건", { total: nluValidationDiagnostics.length, problems: problemCount })}
        </p>
        <p>
          {formatStudioRuntimeMessage(uiLanguage, "공통 Feature 충돌 {conflicts}건 / 학습문장 부족 {shortages}건 / Score 설정 후보 {candidates}건", { conflicts: diagnosisCounts["공통 Feature 충돌"], shortages: diagnosisCounts["학습문장 부족"], candidates: diagnosisCounts["Score 설정 후보"] })}
        </p>
        <div className="simulator-analysis__diagnostic-actions">
          <button
            type="button"
            className="manual-main__learn-button"
            disabled={nluTrainingSupplementCandidates.length === 0 || nluSupplementSaving}
            onClick={handleApplyNluTrainingSupplements}
          >
            {nluSupplementSaving
              ? getStudioRuntimeMessage(uiLanguage, "보강 중")
              : formatStudioRuntimeMessage(uiLanguage, "학습문장 부족 후보 {count}건 T 추가", { count: Math.min(nluTrainingSupplementCandidates.length, 30) })}
          </button>
          {nluSupplementMessage ? <small>{getStudioRuntimeMessage(uiLanguage, nluSupplementMessage)}</small> : null}
        </div>
        <div className="simulator-analysis__diagnostic-list">
          {nluValidationDiagnostics.map((item) => (
            <div key={`${item.expectedIntentName}-${item.utterance}`} className="simulator-analysis__diagnostic-item">
              <span className={`simulator-analysis__diagnostic-status is-${item.status}`}>{getStudioRuntimeMessage(uiLanguage, item.status)}</span>
              <span className="simulator-analysis__diagnostic-type">{getStudioRuntimeMessage(uiLanguage, item.diagnosisType)}</span>
              <strong>{item.utterance}</strong>
              <small>
                {getStudioRuntimeMessage(uiLanguage, "기대")}: {item.expectedIntentName} / {getStudioRuntimeMessage(uiLanguage, "1순위")}: {item.topIntentName} {formatScoreRate(item.topScore)}
                {item.secondIntentName !== "-"
                  ? formatStudioRuntimeMessage(uiLanguage, " / 2순위: {intent} {score}", { intent: item.secondIntentName, score: formatScoreRate(item.secondScore) })
                  : ""}
              </small>
              <small>
                {getStudioRuntimeMessage(uiLanguage, "기대 의도 점수")}: {formatScoreRate(item.expectedScore)} / {getStudioRuntimeMessage(uiLanguage, "기대 의도 T 문장")}: {item.expectedTrainingCount}{getStudioRuntimeMessage(uiLanguage, "개")}
              </small>
              <small>Features: {item.features.join(", ") || "-"}</small>
              <small>{getStudioRuntimeMessage(uiLanguage, "기대 의도 Features")}: {item.expectedFeatures.join(", ") || "-"}</small>
              <small>{getStudioRuntimeMessage(uiLanguage, "공통 Features")}: {item.commonFeatures.join(", ") || "-"}</small>
              <small>{item.reason}</small>
              <small>{item.suggestion}</small>
            </div>
          ))}
        </div>
      </div>
    );
  }

  function renderIntentClassificationSummary(analysis: SimulatorAnalysis) {
    const scoreOutRows = analysis.scoreOut;
    const morphemeText = analysis.morphemes.map((item) => `${item.token}/${item.tag}`).join(" + ");
    const isSemanticAnalysis = isSemanticNluType(analysis.nluType);
    const evidenceTitle = getStudioRuntimeMessage(uiLanguage, isSemanticAnalysis ? "Semantic 검색 근거" : analysis.nluType === "llm" ? "LLM 분류 근거" : "형태소");
    const evidenceDescription = isSemanticAnalysis
      ? getStudioRuntimeMessage(uiLanguage, "학습된 의도 벡터 검색 결과를 Score 순서로 표시합니다.")
      : analysis.nluType === "llm"
        ? getStudioRuntimeMessage(uiLanguage, "LLM 의도분류 결과를 Score 순서로 표시합니다.")
        : morphemeText || "-";
    const evidenceFeatures = [...analysis.scoreIn, ...analysis.scoreOut]
      .slice(0, 3)
      .flatMap((row) => row.features)
      .filter(Boolean);
    return (
      <div className="simulator-analysis__block simulator-analysis__block--nlu">
        <div className="simulator-analysis__manual-section">
          <strong>{getSimulatorLabel(uiLanguage, "Score Setting")}</strong>
          <p>
            NLU Cut-off Score : {formatScoreSetting(analysis.cutoffScore)} / 유사의도 Score : {formatScoreSetting(analysis.similarIntentScore)}
          </p>
          <p className="simulator-analysis__reason">분류 결과 : {getIntentClassificationReason(analysis)}</p>
        </div>

        <div className="simulator-analysis__manual-section">
          <strong>{getSimulatorLabel(uiLanguage, "의도분류 Score-in")}</strong>
          {renderManualScoreRows(analysis.scoreIn, "Cut-off 기준을 통과한 의도가 없습니다.")}
        </div>

        <div className="simulator-analysis__manual-section">
          <strong>{getSimulatorLabel(uiLanguage, "의도분류 Score-out")}</strong>
          {renderManualScoreRows(scoreOutRows, "비교 가능한 Score-out 의도가 없습니다.")}
        </div>

        <div className="simulator-analysis__manual-section">
          <strong>{evidenceTitle}</strong>
          <p>{evidenceDescription}</p>
          <small>
            {analysis.nluType === "ml"
              ? formatStudioRuntimeMessage(uiLanguage, "분류 토큰: {tokens}", { tokens: tokenizeForDeepLearningLite(analysis.utterance).join(", ") || "-" })
              : formatStudioRuntimeMessage(uiLanguage, "근거: {features}", { features: evidenceFeatures.join(", ") || "-" })}
          </small>
        </div>

        <div className="simulator-analysis__manual-card">
          <strong>{getSimulatorLabel(uiLanguage, "대화시작 카드")}</strong>
          <div>
            <span>{getSimulatorLabel(uiLanguage, "연결된 의도")}</span>
            <b>{getStudioRuntimeMessage(uiLanguage, analysis.selectedIntentName)}</b>
          </div>
        </div>
      </div>
    );
  }

  function renderVariableTable(variables: Record<string, string>) {
    const entries = Object.entries(variables);
    if (entries.length === 0) {
      return <p>{getSimulatorLabel(uiLanguage, "변수값 없음")}</p>;
    }

    return (
      <div className="simulator-analysis__variables">
        {entries.map(([key, value]) => (
          <div key={key} className="simulator-analysis__variable">
            <span>${key}</span>
            <code>{value || getStudioRuntimeMessage(uiLanguage, "(빈 값)")}</code>
          </div>
        ))}
      </div>
    );
  }

  function renderRuntimeState() {
    const currentVariables = runtime?.variables ?? selectedAnalysis?.variableSnapshots.at(-1)?.variables ?? {};
    const runtimeStatus = runtime?.ended
      ? runtime.sessionEnded
        ? getStudioRuntimeMessage(uiLanguage, "세션 종료")
        : getStudioRuntimeMessage(uiLanguage, "대화 종료")
      : runtime
        ? getStudioRuntimeMessage(uiLanguage, "진행 중")
        : "-";
    return (
      <div className="simulator-analysis__block simulator-analysis__block--runtime">
        <strong>{getSimulatorLabel(uiLanguage, "현재 런타임 상태")}</strong>
        <div className="simulator-analysis__summary simulator-analysis__summary--compact">
          <span>{getSimulatorLabel(uiLanguage, "대화")}</span>
          <strong>{getStudioRuntimeMessage(uiLanguage, runtime?.dialog.name ?? selectedAnalysis?.selectedIntentName ?? "-")}</strong>
          <span>{getSimulatorLabel(uiLanguage, "다음 카드")}</span>
          <strong>{runtime?.nextNodeId || "-"}</strong>
          <span>{getSimulatorLabel(uiLanguage, "대기 카드")}</span>
          <strong>{runtime?.waitingTalkNodeId || "-"}</strong>
          <span>{getSimulatorLabel(uiLanguage, "종료 여부")}</span>
          <strong>{runtimeStatus}</strong>
        </div>
        {renderVariableTable(currentVariables)}
      </div>
    );
  }

  function isErrorAnalysisLog(log: SimulatorAnalysis["cardLogs"][number]) {
    return (
      log.cardType.includes("오류") ||
      /오류|실패|초과|중단|누락|없습니다|못했습니다/.test(log.description)
    );
  }

  function renderEntitySummary(analysis: SimulatorAnalysis) {
    return (
      <div className="simulator-analysis__block">
        <strong>{getSimulatorLabel(uiLanguage, "개체 인식")}</strong>
        {analysis.entities.length > 0 ? (
          <div className="simulator-analysis__rows">
            {analysis.entities.map((entity) => (
              <div
                key={`${entity.name}-${entity.variableName}`}
                className={`simulator-analysis__row${entity.matched ? "" : " simulator-analysis__row--muted"}`}
              >
                <span>{entity.name}</span>
                <b>{entity.matched ? entity.value : "-"}</b>
                <small>
                  {entity.variableName}
                  {entity.targetValue ? ` / target: ${entity.targetValue}` : ""}
                  {!entity.matched ? ` / ${getStudioRuntimeMessage(uiLanguage, "미인식")}` : ""}
                </small>
              </div>
            ))}
          </div>
        ) : (
          <p>{getSimulatorLabel(uiLanguage, "인식된 개체 없음")}</p>
        )}
      </div>
    );
  }

  function renderErrorSummary(analysis: SimulatorAnalysis) {
    const errors = analysis.cardLogs.filter(isErrorAnalysisLog);
    return (
      <div className="simulator-analysis__block">
        <strong>{getSimulatorLabel(uiLanguage, "오류 / 이상 상황")}</strong>
        {errors.length > 0 ? (
          <div className="simulator-analysis__rows">
            {errors.map((log, index) => (
              <div key={`${log.cardName}-${index}`} className="simulator-analysis__row simulator-analysis__row--error">
                <span>{getStudioRuntimeMessage(uiLanguage, log.cardType)}</span>
                <b>{getStudioRuntimeMessage(uiLanguage, log.cardName)}</b>
                <small>{localizeSimulatorAnalysisDescription(uiLanguage, log.description)}</small>
              </div>
            ))}
          </div>
        ) : (
          <p>{getSimulatorLabel(uiLanguage, "오류 없음")}</p>
        )}
      </div>
    );
  }

  function renderFlowSummary(analysis: SimulatorAnalysis) {
    const logs = analysis.cardLogs.filter((log) => !isErrorAnalysisLog(log));
    return (
      <div className="simulator-analysis__block">
        <strong>{getSimulatorLabel(uiLanguage, "시나리오 흐름")}</strong>
        {logs.length > 0 ? (
          <ol className="simulator-analysis__flow">
            {logs.map((log, index) => (
              <li key={`${log.cardName}-${index}`}>
                <span>{index + 1}</span>
                <div>
                  <b>
                    {getStudioRuntimeMessage(uiLanguage, log.cardType)} / {getStudioRuntimeMessage(uiLanguage, log.cardName)}
                  </b>
                  <small>{localizeSimulatorAnalysisDescription(uiLanguage, log.description)}</small>
                  {Object.keys(log.variables).length > 0 ? (
                    <details className="simulator-analysis__flow-variables">
                      <summary>{getSimulatorLabel(uiLanguage, "카드 실행 후 변수값")}</summary>
                      {renderVariableTable(log.variables)}
                    </details>
                  ) : null}
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <p>{getSimulatorLabel(uiLanguage, "표시할 카드 흐름 없음")}</p>
        )}
      </div>
    );
  }

  function renderFullTraceLogs() {
    const logs = analyses.flatMap((analysis, analysisIndex) =>
      analysis.cardLogs.map((log, logIndex) => ({
        ...log,
        utterance: analysis.utterance,
        order: `${analysisIndex + 1}.${logIndex + 1}`,
      })),
    );

    return (
      <div className="simulator-analysis__block">
        <strong>{getSimulatorLabel(uiLanguage, "전체 처리 로그")}</strong>
        {logs.length > 0 ? (
          <div className="simulator-analysis__rows">
            {logs.map((log) => (
              <div key={`${log.order}-${log.cardType}-${log.cardName}`} className="simulator-analysis__row simulator-analysis__row--trace">
                <span>
                  {log.order}. {getStudioRuntimeMessage(uiLanguage, log.cardType)}
                </span>
                <b>{getStudioRuntimeMessage(uiLanguage, log.cardName)}</b>
                <small>
                  [{localizeSimulatorAnalysisUtterance(uiLanguage, log.utterance)}] {localizeSimulatorAnalysisDescription(uiLanguage, log.description)}
                </small>
                <div>{renderVariableTable(log.variables)}</div>
              </div>
            ))}
          </div>
        ) : (
          <p>{getSimulatorLabel(uiLanguage, "처리 로그 없음")}</p>
        )}
      </div>
    );
  }

  function renderFullVariableSnapshots() {
    const snapshots = analyses.flatMap((analysis, analysisIndex) =>
      analysis.variableSnapshots.map((snapshot, snapshotIndex) => ({
        ...snapshot,
        utterance: analysis.utterance,
        order: `${analysisIndex + 1}.${snapshotIndex + 1}`,
      })),
    );

    return (
      <div className="simulator-analysis__block">
        <strong>{getSimulatorLabel(uiLanguage, "전체 변수 스냅샷")}</strong>
        {snapshots.length > 0 ? (
          <div className="simulator-analysis__snapshots">
            {snapshots.map((snapshot, index) => (
              <details key={`${snapshot.order}-${snapshot.label}`} open={index === snapshots.length - 1}>
                <summary>
                  {snapshot.order}. [{localizeSimulatorAnalysisUtterance(uiLanguage, snapshot.utterance)}] {localizeSimulatorAnalysisLabel(uiLanguage, snapshot.label)}
                </summary>
                {renderVariableTable(snapshot.variables)}
              </details>
            ))}
          </div>
        ) : (
          <p>{getSimulatorLabel(uiLanguage, "변수 스냅샷 없음")}</p>
        )}
      </div>
    );
  }

  const content = (
    <>
      {!embedded ? (
        <div className="studio-topbar studio-topbar--flat">
          <div>
            <p className="crumb">{copy.botTestBreadcrumb}</p>
            <h1>{copy.botTestTitle}</h1>
          </div>
        </div>
      ) : null}

      <div
        className={`simulator-workbench${embedded ? " simulator-workbench--popup" : ""}${
          !sharedMode ? (analysisVisible ? " simulator-workbench--debug-open" : "") : ""
        }`}
      >
        <div className="simulator-stage simulator-stage--flat simulator-stage--messenger">
          <div className="simulator-window simulator-window--messenger" style={sharedMode ? { minHeight: 0, maxHeight: embedded ? undefined : "calc(100dvh - 260px)" } : undefined}>
            <div className="simulator-window__header">
              <div className="simulator-title">
                <span className="simulator-avatar" />
                <span>
                  <strong>{bot?.name ?? getSimulatorLabel(uiLanguage, "봇")}</strong>
                  <small>{sharedMode ? sharedHistory?.activeVersion?.name || getSimulatorLabel(uiLanguage, "운영 버전 확인 중") : versionId} / Simulator</small>
                </span>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label={copy.closeSimulator}
                onClick={() =>
                  embedded
                    ? onClose?.()
                    : router.push(`/studio/bots/${botId}/versions/${versionId}/intents`)
                }
              >
                ×
              </button>
            </div>

            <div className="simulator-script-status">
              <label>{getSimulatorLabel(uiLanguage, "대화 방식")} <select aria-label={getSimulatorLabel(uiLanguage, "대화 방식")} value={sharedMode ? "shared" : "preview"} disabled={sharedSendingBot || sharedSendingGeneral || sharedConnecting} onChange={event => setSharedMode(event.target.value === "shared")}>
                <option value="preview">{getSimulatorLabel(uiLanguage, "1:1 미리보기")}</option><option value="shared">{getSimulatorLabel(uiLanguage, "공유방")}</option>
              </select></label>
            </div>
            {sharedMode ? <div className="simulator-alert">
              <p>{getSimulatorLabel(uiLanguage, "공유방은 운영 버전으로 대화합니다. 방 ID로 다른 참여자를 초대할 수 있습니다.")}</p>
              <label>{getSimulatorLabel(uiLanguage, "참여자 이름")} <input aria-label={getSimulatorLabel(uiLanguage, "참여자 이름")} value={sharedName} onChange={event => setSharedName(event.target.value)} maxLength={120} /></label>
              <label>{getSimulatorLabel(uiLanguage, "방 ID")} <input aria-label={getSimulatorLabel(uiLanguage, "공유방 ID")} value={sharedRoomInput} onChange={event => setSharedRoomInput(event.target.value)} /></label>
              <button type="button" disabled={sharedConnecting || sharedSendingBot || sharedSendingGeneral} onClick={() => void connectSharedRoom(false)}>{getSimulatorLabel(uiLanguage, "새 공유방")}</button>
              <button type="button" disabled={!sharedRoomInput.trim() || sharedConnecting || sharedSendingBot || sharedSendingGeneral} onClick={() => void connectSharedRoom(true)}>{getSimulatorLabel(uiLanguage, "참가자로 입장")}</button>
              {sharedMember ? <p>{getSimulatorLabel(uiLanguage, "현재 방:")} {sharedMember.roomId} / {sharedHistory?.turn.state === "processing" ? getSimulatorLabel(uiLanguage, "답변 처리 중") : sharedHistory?.turn.state === "waiting_input" ? sharedHistory.turn.ownerParticipantId === sharedMember.participantId ? getSimulatorLabel(uiLanguage, "내 후속 답변을 기다립니다.") : getSimulatorLabel(uiLanguage, "다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.") : sharedHistory ? getSimulatorLabel(uiLanguage, "봇에게 질문할 수 있습니다.") : getSimulatorLabel(uiLanguage, "방 상태 확인 중")}</p> : null}
              {sharedError ? <p role="alert">{sharedError}</p> : null}
              {sharedSendError ? <p role="alert">{sharedSendError}</p> : null}
            </div> : null}
            {errorMessage ? <div className="simulator-alert">{errorMessage}</div> : null}

            <div ref={scrollRef} className="simulator-window__body simulator-window__body--scroll">
              {loading ? (
                <div className="simulator-empty">{copy.loading}</div>
              ) : (
                <div className="chat-stack">
                  {displayedMessages.filter(isVisibleSimulatorMessage).map((message) => (
                    <div
                      key={message.id}
                      className={`chat-row chat-row--${message.sender === "user" ? "user" : "bot"}${message.sender === "system" ? " chat-row--system" : ""}`}
                    >
                      {message.sender === "bot" ? <span className="chat-message-avatar" /> : null}
                      <div className="chat-message">
                        {message.sender === "bot" ? (
                          <div className="chat-message__meta">
                            <strong>{message.participantName || bot?.name || getSimulatorLabel(uiLanguage, "봇")}</strong>
                          </div>
                        ) : null}
                        {sharedMode && message.sender === "user" ? <div className="chat-message__meta"><strong>{message.participantName}</strong></div> : null}
                        <div className="chat-message__line">
                          {message.sender === "user" ? (
                            <time dateTime={message.timestamp}>{formatMessageTime(message.timestamp, uiLanguage)}</time>
                          ) : null}
                          <div
                            className={`chat-bubble chat-bubble--${message.sender === "user" ? "user" : "bot"}${message.sender === "system" ? " chat-bubble--system" : ""}`}
                          >
                            {message.text ? <div>{message.text}</div> : null}
                            {message.html ? (
                              <iframe
                                className="simulator-html-preview"
                                sandbox=""
                                srcDoc={sanitizeRichFormHtml(message.html)}
                                title={getSimulatorLabel(uiLanguage, "HTML 메시지")}
                              />
                            ) : null}
                            {message.cardTitle ? (
                              <div className="simulator-card-preview">
                                {message.cardImageUrl ? (
                                  <img
                                    className="simulator-card-preview__image"
                                    src={message.cardImageUrl}
                                    alt={message.cardTitle}
                                  />
                                ) : null}
                                <strong>{message.cardTitle}</strong>
                                {message.cardDescription ? (
                                  <p className="simulator-card-preview__description">{message.cardDescription}</p>
                                ) : null}
                                {message.cardItems?.map((item) => item.href ? (
                                  <a key={`${message.id}-${item.label}`} href={item.href} target="_blank" rel="noreferrer">
                                    <b>{item.label}</b>
                                    <em>{item.value}</em>
                                  </a>
                                ) : (
                                  <span key={`${message.id}-${item.label}`}>
                                    <b>{item.label}</b>
                                    <em>{item.value}</em>
                                  </span>
                                ))}
                              </div>
                            ) : null}
                            {message.table ? (
                              <div className="simulator-table">
                                <table>
                                  <thead>
                                    <tr>
                                      {message.table.columns.map((column, index) => (
                                        <th key={`${message.id}-column-${index}`}>{column}</th>
                                      ))}
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {message.table.rows.length > 0 ? (
                                      message.table.rows.map((row, rowIndex) => (
                                        <tr
                                          key={`${message.id}-row-${rowIndex}`}
                                          className={message.table?.selectable && (!sharedMode || canUseMessageCard(message)) ? "is-selectable" : undefined}
                                          role={message.table?.selectable && (!sharedMode || canUseMessageCard(message)) ? "button" : undefined}
                                          tabIndex={message.table?.selectable && (!sharedMode || canUseMessageCard(message)) ? 0 : undefined}
                                          onClick={() => {
                                            if (message.table?.selectable && (!sharedMode || canUseMessageCard(message))) {
                                              submitUtterance(row.key, {}, message.sourceTalkNodeId);
                                            }
                                          }}
                                          onKeyDown={(event) => {
                                            if ((!sharedMode || canUseMessageCard(message)) && message.table?.selectable && (event.key === "Enter" || event.key === " ")) {
                                              event.preventDefault();
                                              submitUtterance(row.key, {}, message.sourceTalkNodeId);
                                            }
                                          }}
                                        >
                                          <td>{(row.firstCell ?? row.key) || "-"}</td>
                                          {message.table?.columns.slice(1).map((_, valueIndex) => (
                                            <td key={`${message.id}-row-${rowIndex}-value-${valueIndex}`}>
                                              {row.values[valueIndex] || "-"}
                                            </td>
                                          ))}
                                        </tr>
                                      ))
                                    ) : (
                                      <tr>
                                        <td colSpan={message.table.columns.length || 1}>{getSimulatorLabel(uiLanguage, "표시할 항목이 없습니다.")}</td>
                                      </tr>
                                    )}
                                  </tbody>
                                </table>
                              </div>
                            ) : null}
                            {message.template ? (
                              <div
                                className={`simulator-template-card${
                                  canUseMessageCard(message)
                                    ? " is-active"
                                    : ""
                                }`}
                              >
                                {(message.template.kind === "adaptive-card" && message.template.rawJson) ? (
                                  <AdaptiveCardRenderer
                                    includeSubmittedInputs={sharedMode}
                                    template={message.template}
                                    disabled={loading || (sharedMode && !canUseMessageCard(message))}
                                    onAction={(value) => submitTemplateAction(message, value)}
                                    onRenderError={(error) => handleAdaptiveCardRenderError(message, error)}
                                  />
                                ) : (message.template.kind === "rich-form" && message.template.rawJson) ? (
                                  <RichFormRenderer
                                    template={message.template}
                                    disabled={loading || (sharedMode && !canUseMessageCard(message))}
                                    onAction={(value) => submitTemplateAction(message, value)}
                                  />
                                ) : (
                                  <>
                                    <strong>{message.template.title}</strong>
                                    {message.template.lines.map((line, index) => (
                                      <span key={`${message.id}-template-line-${index}`}>{line}</span>
                                    ))}
                                    {message.template.actions.length > 0 ? (
                                      <div className="simulator-template-card__actions">
                                        {message.template.actions.map((action, index) => (
                                          <button
                                            key={`${message.id}-template-action-${index}`}
                                            type="button"
                                            disabled={
                                              !canUseMessageCard(message)
                                            }
                                            onClick={() => submitUtterance(action, {}, message.sourceTalkNodeId)}
                                          >
                                            {action}
                                          </button>
                                        ))}
                                      </div>
                                    ) : null}
                                  </>
                                )}
                              </div>
                            ) : null}
                            {message.dtmf ? (() => {
                              const value = dtmfValues[message.id] ?? "";
                              const disabled = !canUseMessageCard(message);
                              const appendDigit = (digit: string) => {
                                setDtmfValues((current) => {
                                  const currentValue = current[message.id] ?? "";
                                  if (currentValue.length >= message.dtmf!.maxLength) return current;
                                  return { ...current, [message.id]: `${currentValue}${digit}` };
                                });
                                setDtmfErrors((current) => ({ ...current, [message.id]: "" }));
                              };
                              const submitDtmf = () => {
                                if (value.length < message.dtmf!.minLength || value.length > message.dtmf!.maxLength) {
                                  setDtmfErrors((current) => ({ ...current, [message.id]: formatSimulatorText(SIMULATOR_DTMF_CATALOGS[uiLanguage].lengthError, { min: String(message.dtmf!.minLength), max: String(message.dtmf!.maxLength) }) }));
                                  return;
                                }
                                setDtmfValues((current) => ({ ...current, [message.id]: "" }));
                                setDtmfErrors((current) => ({ ...current, [message.id]: "" }));
                                submitUtterance(value, {}, message.sourceTalkNodeId);
                              };
                              return (
                                <div className="simulator-dtmf" aria-label={getSimulatorLabel(uiLanguage, "DTMF 입력")}>
                                  <small>{formatSimulatorText(SIMULATOR_DTMF_CATALOGS[uiLanguage].guide, { min: String(message.dtmf.minLength), max: String(message.dtmf.maxLength), end: message.dtmf.endCharacter, first: String(message.dtmf.firstInputTimeoutMs / 1000), overall: String(message.dtmf.overallInputTimeoutMs / 1000) })}</small>
                                  <output>{value || getStudioRuntimeMessage(uiLanguage, "입력 대기 중")}</output>
                                  <div className="simulator-dtmf__keypad">
                                    {["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"].map((digit) => (
                                      <button key={`${message.id}-${digit}`} type="button" disabled={disabled} onClick={() => appendDigit(digit)}>{digit}</button>
                                    ))}
                                    <button type="button" disabled={disabled} onClick={submitDtmf}>{message.dtmf.endCharacter}</button>
                                  </div>
                                  <div className="simulator-dtmf__actions">
                                    <button type="button" disabled={disabled || !value} onClick={() => {
                                      setDtmfValues((current) => ({ ...current, [message.id]: value.slice(0, -1) }));
                                      setDtmfErrors((current) => ({ ...current, [message.id]: "" }));
                                    }}>{getSimulatorLabel(uiLanguage, "지우기")}</button>
                                    <button type="button" disabled={disabled} onClick={submitDtmf}>{getSimulatorLabel(uiLanguage, "입력 완료")}</button>
                                  </div>
                                  {dtmfErrors[message.id] ? <p role="alert">{dtmfErrors[message.id]}</p> : null}
                                </div>
                              );
                            })() : null}
                            {message.quickReplies?.length ? (
                              <div className="simulator-quick-replies">
                                {message.quickReplies.map((reply) => (
                                  <button
                                    key={reply}
                                    type="button"
                                    disabled={
                                      !(
                                        (!sharedMode && pendingIntentSelection && !runtime && message.id === messages.at(-1)?.id) ||
                                        (canUseMessageCard(message))
                                      )
                                    }
                                    onClick={() => submitUtterance(reply, {}, message.sourceTalkNodeId)}
                                  >
                                    {reply}
                                  </button>
                                ))}
                              </div>
                            ) : null}
                          </div>
                          {message.sender !== "system" && message.sender !== "user" ? (
                            <time dateTime={message.timestamp}>{formatMessageTime(message.timestamp, uiLanguage)}</time>
                          ) : null}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {!sharedMode && floatingButtonsVisible && floatingButtons.length > 0 ? (
              <div className="simulator-floating-area">
                <div className="simulator-floating-list">
                  {floatingButtons.map((button) => (
                    <button key={button.key} type="button" onClick={() => submitUtterance(button.key)}>
                      {button.label}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            <div className="simulator-toolbar">
              <button
                type="button"
                className="tool-link"
                onClick={handleRestart}
                aria-label={getSimulatorLabel(uiLanguage, "대화 재시작")}
                title={getSimulatorLabel(uiLanguage, "대화 재시작")}
              >
                <SimulatorToolbarIcon name="restart" />
              </button>
              <span className="simulator-toolbar__spacer" />
              <button
                type="button"
                className="tool-link"
                onClick={() => setFloatingButtonsVisible(false)}
                aria-label={getSimulatorLabel(uiLanguage, "플로팅 버튼 닫기")}
                title={getSimulatorLabel(uiLanguage, "플로팅 버튼 닫기")}
              >
                <SimulatorToolbarIcon name="close-floating" />
              </button>
              <button
                type="button"
                className="tool-link"
                disabled={sharedMode}
                onClick={() => fileInputRef.current?.click()}
                aria-label={getSimulatorLabel(uiLanguage, "대화 불러오기")}
                title={getSimulatorLabel(uiLanguage, "대화 불러오기")}
              >
                <SimulatorToolbarIcon name="load" />
              </button>
              <button
                type="button"
                className="tool-link"
                onClick={handleDownloadConversation}
                aria-label={getSimulatorLabel(uiLanguage, "대화 저장하기")}
                title={getSimulatorLabel(uiLanguage, "대화 저장하기")}
              >
                <SimulatorToolbarIcon name="download" />
              </button>
              {!sharedMode && loadedScript ? (
                <button
                  type="button"
                  className="tool-link"
                  onClick={() => setScriptViewerOpen(true)}
                  aria-label={getSimulatorLabel(uiLanguage, "대화 전체보기")}
                  title={getSimulatorLabel(uiLanguage, "대화 전체보기")}
                >
                  <SimulatorToolbarIcon name="list" />
                </button>
              ) : null}
              <input
                ref={fileInputRef}
                type="file"
                accept=".json,application/json"
                hidden
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) {
                    void handleUploadConversation(file);
                  }
                }}
              />
            </div>

            <form className="simulator-composer simulator-composer--messenger" style={sharedMode ? { gridTemplateColumns: "minmax(0, 1fr) auto auto" } : undefined} onSubmit={handleSubmit}>
              {!sharedMode && loadedScript ? (
                <div className="simulator-script-status">
                  <span>{loadedScript.fileName}</span>
                  <b>
                    {Math.min(loadedScript.index + 1, loadedScript.sentences.length)} / {loadedScript.sentences.length}
                  </b>
                </div>
              ) : null}
              <textarea
                ref={inputRef}
                className="textarea-control textarea-control--flat"
                value={inputValue}
                placeholder={copy.inputPlaceholder}
                onChange={(event) => handleInputChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    handleSubmit();
                  }
                }}
              />
              {sharedMode ? <button type="button" disabled={!inputValue.trim() || !sharedCanChat} onClick={() => void sendSharedMessage(inputValue, false)}>{getSimulatorLabel(uiLanguage, "일반 대화")}</button> : null}
              <button type="submit" className="primary-action" disabled={!inputValue.trim() || loading || (sharedMode && !sharedCanAsk)}>
                {sharedMode ? getSimulatorLabel(uiLanguage, "봇에게 질문") : copy.send}
              </button>
            </form>

            {embedded && !sharedMode ? (
              <button
                type="button"
                className={`simulator-footer simulator-footer--button${analysisOpen ? " is-active" : ""}`}
                onClick={() => {
                  setSelectedAnalysisId(selectedAnalysis?.id ?? analyses.at(-1)?.id ?? null);
                  setAnalysisOpen((current) => !current);
                }}
              >
                {copy.analysisData}
              </button>
            ) : null}
          </div>
        </div>

        {!sharedMode && analysisVisible ? (
        <aside className="simulator-analysis" aria-label={copy.analysisData}>
          <div className="simulator-analysis__header">
            <strong>{copy.analysisData}</strong>
            <span>{copy.runtimeTab}</span>
            {embedded ? (
              <button
                type="button"
                className="simulator-analysis__close"
                onClick={() => setAnalysisOpen(false)}
                aria-label={copy.closeAnalysis}
              >
                ×
              </button>
            ) : null}
          </div>
          {selectedAnalysis ? (
            <>
              {renderRuntimeState()}
              <div className="simulator-analysis__summary">
                <span>{getSimulatorLabel(uiLanguage, "사용자 발화")}</span>
                <strong>{localizeSimulatorAnalysisUtterance(uiLanguage, selectedAnalysis.utterance)}</strong>
                <span>{getSimulatorLabel(uiLanguage, "선택 의도")}</span>
                <strong>{getStudioRuntimeMessage(uiLanguage, selectedAnalysis.selectedIntentName)}</strong>
              </div>
              {renderIntentDecisionSummary(selectedAnalysis)}
              {renderIntentClassificationSummary(selectedAnalysis)}
              {renderEntitySummary(selectedAnalysis)}
              {renderErrorSummary(selectedAnalysis)}
              {renderFlowSummary(selectedAnalysis)}
              <div className="simulator-analysis__block">
                <details className="simulator-analysis__details">
                  <summary>{getSimulatorLabel(uiLanguage, "상세 진단 데이터")}</summary>
                  {renderAnalysisList(getSimulatorLabel(uiLanguage, "의도분류 Score-in"), selectedAnalysis.scoreIn)}
                  {renderAnalysisList(getSimulatorLabel(uiLanguage, "의도분류 Score-out"), selectedAnalysis.scoreOut)}
                  <div className="simulator-analysis__block simulator-analysis__block--nested">
                    <strong>{getSimulatorLabel(uiLanguage, "변수 스냅샷")}</strong>
                    {selectedAnalysis.variableSnapshots.length > 0 ? (
                      <div className="simulator-analysis__snapshots">
                        {selectedAnalysis.variableSnapshots.map((snapshot, index) => (
                          <details key={`${snapshot.label}-${index}`}>
                            <summary>{localizeSimulatorAnalysisLabel(uiLanguage, snapshot.label)}</summary>
                            {renderVariableTable(snapshot.variables)}
                          </details>
                        ))}
                      </div>
                    ) : (
                      <p>{getSimulatorLabel(uiLanguage, "변수 스냅샷 없음")}</p>
                    )}
                  </div>
                </details>
              </div>
            </>
          ) : (
            <div className="simulator-empty">{copy.emptyAnalysis}</div>
          )}
        </aside>
        ) : null}
      </div>

      {scriptViewerOpen && loadedScript ? (
        <div className="entity-editor-backdrop" role="presentation">
          <div className="entity-editor-dialog simulator-script-dialog" role="dialog" aria-modal="true">
            <div className="entity-editor-dialog__header">
              <strong>{getSimulatorLabel(uiLanguage, "불러온 대화 전체보기")}</strong>
              <button type="button" className="entity-editor-dialog__close" onClick={() => setScriptViewerOpen(false)}>
                ×
              </button>
            </div>
            <div className="entity-editor-dialog__body">
              <ol className="simulator-script-dialog__list">
                {loadedScript.sentences.map((sentence, index) => (
                  <li key={`${sentence}-${index}`} className={index === loadedScript.index ? "is-active" : ""}>
                    {sentence}
                  </li>
                ))}
              </ol>
            </div>
            <div className="entity-editor-dialog__footer">
              <button type="button" className="primary-action" onClick={() => setScriptViewerOpen(false)}>{getSimulatorLabel(uiLanguage, "확인")}</button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );

  if (embedded) {
    return content;
  }

  return <section className="workspace-stack">{content}</section>;
}

type SimulatorFloatingLauncherProps = {
  startDialogId?: string;
  botIdOverride?: string;
  versionIdOverride?: string;
};

export function SimulatorFloatingLauncher({
  startDialogId = "",
  botIdOverride = "",
  versionIdOverride = "",
}: SimulatorFloatingLauncherProps) {
  const { language: uiLanguage } = useI18n();
  const copy = SIMULATOR_CATALOGS[uiLanguage];
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  return (
    <>
      <button
        type="button"
        className={`floating-simulator${open ? " is-open" : ""}`}
        aria-label={open ? copy.closeSimulator : copy.openSimulator}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="floating-simulator__face">
          <span />
          <span />
        </span>
      </button>

      {open ? (
        <div className="simulator-popup-backdrop" role="presentation">
          <div className="simulator-popup-shell" role="dialog" aria-modal="true" aria-label={copy.dialogLabel}>
            <SimulatorPage
              embedded
              startDialogId={startDialogId}
              botIdOverride={botIdOverride}
              versionIdOverride={versionIdOverride}
              onClose={() => setOpen(false)}
            />
          </div>
        </div>
      ) : null}
    </>
  );
}
