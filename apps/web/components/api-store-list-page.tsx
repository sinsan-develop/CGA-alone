"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { DataGrid, type DataGridRow } from "@/components/data-grid";
import { useI18n } from "@/components/language-provider";
import { SortHeaderLabel } from "@/components/sort-header-label";
import { useStudioWorkspace } from "@/components/studio-workspace-provider";
import {
  buildApiExportPayload,
  createApiAssetId,
  createEmptyApiAsset,
  createEmptyApiMethod,
  createEmptyApiParameter,
  normalizeApiAssets,
  parseApiImportPayload,
  withUpdatedApiAssets,
  type VersionApiAsset,
  type VersionApiDataType,
  type VersionApiOutputParameter,
} from "@/lib/api-assets";
import { applyUpdatedVersionToBot } from "@/lib/dialog-assets";
import { hasApiWriteRole, loadAuthSession, saveLastBotScreen, type AuthSession } from "@/lib/auth";
import { API_EDITOR_CATALOGS } from "@/lib/i18n/api-editor";
import { API_MANAGEMENT_CATALOGS, formatApiManagementText } from "@/lib/i18n/api-management";
import {
  fetchStudioBotVersionApis,
  type StudioBotApiItem,
  type StudioBotVersionApiItem,
  updateStudioBotVersionApis,
} from "@/lib/studio-bots-api";
import { normalizeVersionDocument } from "@/lib/version-document";

function formatUpdatedAt(value: string) {
  return value ? value.replace("T", " ").slice(0, 16) : "-";
}

function downloadTextFile(fileName: string, text: string, type = "application/json;charset=utf-8") {
  const blob = new Blob([`\uFEFF${text}`], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function getSampleOutputType(value: unknown): VersionApiDataType {
  if (Array.isArray(value)) {
    return "array";
  }
  if (value && typeof value === "object") {
    return "object";
  }
  if (typeof value === "number") {
    return "number";
  }
  if (typeof value === "boolean") {
    return "boolean";
  }
  return "string";
}

function buildOutputParametersFromSample(value: unknown, name = "root", path = "root"): VersionApiOutputParameter[] {
  const rows: VersionApiOutputParameter[] =
    name === "root"
      ? []
      : [
          {
            id: createApiAssetId(),
            name,
            path,
            dataType: getSampleOutputType(value),
            description: "",
          },
        ];

  if (Array.isArray(value)) {
    const firstItem = value[0];
    if (firstItem && typeof firstItem === "object") {
      return rows.concat(buildOutputParametersFromSample(firstItem, `${name}[]`, `${path}[]`));
    }
    return rows;
  }

  if (value && typeof value === "object") {
    Object.entries(value as Record<string, unknown>).forEach(([childName, childValue]) => {
      rows.push(...buildOutputParametersFromSample(childValue, childName, `${path}.${childName}`));
    });
  }

  return rows;
}

function getOutputParameterDepth(path: string) {
  const normalizedPath = path.replace(/^root\.?/, "").replace(/\[\]/g, "");
  const segmentCount = normalizedPath.split(".").filter(Boolean).length;
  return segmentCount;
}

function hasInvalidJson(value: string) {
  if (!value.trim()) {
    return false;
  }

  try {
    JSON.parse(value);
    return false;
  } catch {
    return true;
  }
}

export function ApiEditorDialog({
  initialApi,
  apiKeyLocked = false,
  saving,
  onClose,
  onSave,
}: {
  initialApi: VersionApiAsset;
  apiKeyLocked?: boolean;
  saving: boolean;
  onClose: () => void;
  onSave: (api: VersionApiAsset) => void;
}) {
  const { language: uiLanguage } = useI18n();
  const copy = API_EDITOR_CATALOGS[uiLanguage];
  const common = API_MANAGEMENT_CATALOGS[uiLanguage];
  const [draft, setDraft] = useState(initialApi);
  const [sampleInput, setSampleInput] = useState<{ methodIndex: number; value: string; error: string } | null>(null);
  const [textEditMethods, setTextEditMethods] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(initialApi.methods.map((method) => [method.id, Boolean(method.outputSample.trim())])),
  );
  const hasInvalidOutputText = draft.methods.some(
    (method) => (textEditMethods[method.id] ?? Boolean(method.outputSample.trim())) && hasInvalidJson(method.outputSample),
  );

  function updateMethod(index: number, updater: (method: VersionApiAsset["methods"][number]) => VersionApiAsset["methods"][number]) {
    setDraft((current) => ({
      ...current,
      methods: current.methods.map((method, methodIndex) => (methodIndex === index ? updater(method) : method)),
    }));
  }

  function addParameter(methodIndex: number) {
    updateMethod(methodIndex, (method) => ({
      ...method,
      parameters: [...method.parameters, createEmptyApiParameter("query")],
    }));
  }

  function removeParameter(methodIndex: number, parameterId: string) {
    updateMethod(methodIndex, (method) => ({
      ...method,
      parameters: method.parameters.filter((parameter) => parameter.id !== parameterId),
    }));
  }

  function applyOutputSample() {
    if (!sampleInput) {
      return;
    }

    try {
      const parsed = JSON.parse(sampleInput.value);
      updateMethod(sampleInput.methodIndex, (method) => ({
        ...method,
        outputParameters: buildOutputParametersFromSample(parsed),
        outputSample: "",
      }));
      const methodId = draft.methods[sampleInput.methodIndex]?.id;
      if (methodId) {
        setTextEditMethods((current) => ({ ...current, [methodId]: false }));
      }
      setSampleInput(null);
    } catch {
      setSampleInput((current) =>
        current ? { ...current, error: copy.invalidJson } : current,
      );
    }
  }

  return (
    <section className="api-store-dialog" role="region" aria-label={copy.region}>
      <header className="api-store-dialog__header">
        <div>
          <strong>{copy.intro}</strong>
        </div>
        <button type="button" className="api-store-dialog__close" onClick={onClose} aria-label={copy.close}>
          ×
        </button>
      </header>
      <div className="api-store-dialog__body">
          <section className="api-store-dialog__api-base">
            <div className="api-store-dialog__subhead">
              <strong>{copy.apiBasic}</strong>
            </div>
            <div className="api-store-dialog__api-grid">
              <label className="api-store-dialog__field">
                <span>{common.apiName} <em>*</em></span>
                <div className="api-store-dialog__count-field">
                  <input
                    className="api-store-dialog__input"
                    value={draft.name}
                    maxLength={30}
                    onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))}
                    placeholder={copy.apiNamePlaceholder}
                  />
                  <small>{draft.name.length}/30</small>
                </div>
              </label>
              <label className="api-store-dialog__field">
                <span>API Key <em>*</em></span>
                <div className="api-store-dialog__count-field">
                  <input
                    className="api-store-dialog__input"
                    value={draft.apiKey}
                    maxLength={40}
                    disabled={apiKeyLocked}
                    onChange={(event) => setDraft((current) => ({ ...current, apiKey: event.target.value }))}
                    placeholder="c436730e-b006-4d38-94fd-997da2a77d83"
                  />
                  <small>{draft.apiKey.length}/40</small>
                </div>
              </label>
              <label className="api-store-dialog__field">
                <span>{common.description}</span>
                <div className="api-store-dialog__count-field">
                  <input
                    className="api-store-dialog__input"
                    value={draft.description}
                    maxLength={50}
                    onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))}
                    placeholder={copy.descriptionPlaceholder}
                  />
                  <small>{draft.description.length}/50</small>
                </div>
              </label>
              <label className="api-store-dialog__field">
                <span>{common.baseUrl} <em>*</em></span>
                <div className="api-store-dialog__count-field">
                  <input
                    className="api-store-dialog__input"
                    value={draft.baseUrl}
                    maxLength={500}
                    onChange={(event) => setDraft((current) => ({ ...current, baseUrl: event.target.value }))}
                    placeholder={copy.urlPlaceholder}
                  />
                  <small>{draft.baseUrl.length}/500</small>
                </div>
              </label>
            </div>
          </section>

          {draft.methods.map((method, index) => (
            <section key={method.id} className="api-store-dialog__method-panel">
              <div className="api-store-dialog__method-top">
                <strong>{common.methods}</strong>
                <div className="api-store-dialog__method-actions">
                  <button
                    type="button"
                    className={`api-store-dialog__method-toggle${method.httpMethod === "GET" ? " is-active" : ""}`}
                    onClick={() => updateMethod(index, (item) => ({ ...item, httpMethod: "GET" }))}
                  >
                    GET
                  </button>
                  <button
                    type="button"
                    className={`api-store-dialog__method-toggle${method.httpMethod === "POST" ? " is-active" : ""}`}
                    onClick={() => updateMethod(index, (item) => ({ ...item, httpMethod: "POST" }))}
                  >
                    POST
                  </button>
                </div>
                <button type="button" className="api-store-dialog__collapse" aria-label={copy.collapse}>
                  ^
                </button>
              </div>

              <div className="api-store-dialog__subhead">
                <strong>{common.basicInfo}</strong>
              </div>

              <div className="api-store-dialog__basic-grid">
                <label className="api-store-dialog__field api-store-dialog__field--method-url">
                  <span>{common.methodUrl} <em>*</em></span>
                  <div className="api-store-dialog__count-field">
                    <input
                      className="api-store-dialog__input"
                      value={method.methodUrl}
                      maxLength={50}
                      onChange={(event) => updateMethod(index, (item) => ({ ...item, methodUrl: event.target.value }))}
                      placeholder={common.methodUrl}
                    />
                    <small>{method.methodUrl.length}/50</small>
                  </div>
                </label>
                <label className="api-store-dialog__field api-store-dialog__field--description">
                  <span>{common.description}</span>
                  <div className="api-store-dialog__count-field">
                    <input
                      className="api-store-dialog__input"
                      value={method.description}
                      maxLength={500}
                      onChange={(event) => updateMethod(index, (item) => ({ ...item, description: event.target.value }))}
                      placeholder={common.methodDescription}
                    />
                    <small>{method.description.length}/500</small>
                  </div>
                </label>

                <div className="api-store-dialog__type-row">
                  <span>{common.type}</span>
                  <label className="api-store-dialog__check">
                    <input
                      type="checkbox"
                      checked={method.loggingEnabled}
                      onChange={(event) => updateMethod(index, (item) => ({ ...item, loggingEnabled: event.target.checked }))}
                    />
                    <span>{common.logging}</span>
                  </label>
                  <label className="api-store-dialog__check">
                    <input
                      type="checkbox"
                      checked={method.proxyEnabled}
                      onChange={(event) => updateMethod(index, (item) => ({ ...item, proxyEnabled: event.target.checked }))}
                    />
                    <span>{common.proxy}</span>
                  </label>
                </div>
                <div className="api-store-dialog__radio-group api-store-dialog__radio-group--transfer">
                  <span>{common.transfer}</span>
                  <label>
                    <input type="radio" checked readOnly />
                    {common.sync}
                  </label>
                  <label className="is-disabled">
                    <input type="radio" disabled />
                    {common.async}
                  </label>
                </div>
              </div>

              <div className="api-store-dialog__method-body">
                <section className="api-store-dialog__parameters">
                  <div className="api-store-dialog__table-toolbar">
                    <strong>{common.parameters}</strong>
                    <button type="button" onClick={() => addParameter(index)}>
                      + {copy.addParameter}
                    </button>
                  </div>
                  <div className="api-store-dialog__param-table">
                    <div className="api-store-dialog__param-head">
                      <span>Name</span>
                      <span>{common.parameterDescription}</span>
                      <span>{common.parameterType}</span>
                      <span>{copy.dataType}</span>
                      <span>{copy.defaultValue}</span>
                      <span>{copy.required}</span>
                      <span>{copy.visible}</span>
                      <span />
                    </div>
                    {method.parameters.length > 0 ? (
                      method.parameters.map((parameter) => (
                        <div key={parameter.id} className="api-store-dialog__param-row">
                          <input
                            value={parameter.name}
                            maxLength={30}
                            onChange={(event) =>
                              updateMethod(index, (item) => ({
                                ...item,
                                parameters: item.parameters.map((entry) =>
                                  entry.id === parameter.id ? { ...entry, name: event.target.value } : entry,
                                ),
                              }))
                            }
                            placeholder={copy.max30}
                          />
                          <input
                            value={parameter.description}
                            maxLength={30}
                            onChange={(event) =>
                              updateMethod(index, (item) => ({
                                ...item,
                                parameters: item.parameters.map((entry) =>
                                  entry.id === parameter.id ? { ...entry, description: event.target.value } : entry,
                                ),
                              }))
                            }
                            placeholder={copy.max30}
                          />
                          <select
                            value={parameter.location}
                            onChange={(event) =>
                              updateMethod(index, (item) => ({
                                ...item,
                                parameters: item.parameters.map((entry) =>
                                  entry.id === parameter.id
                                    ? {
                                        ...entry,
                                        location: event.target.value as typeof parameter.location,
                                      }
                                    : entry,
                                ),
                              }))
                            }
                          >
                            <option value="query">query</option>
                            <option value="path">path</option>
                            <option value="header">header</option>
                            <option value="body">body</option>
                          </select>
                          <select
                            value={parameter.dataType}
                            onChange={(event) =>
                              updateMethod(index, (item) => ({
                                ...item,
                                parameters: item.parameters.map((entry) =>
                                  entry.id === parameter.id
                                    ? {
                                        ...entry,
                                        dataType: event.target.value as typeof parameter.dataType,
                                      }
                                    : entry,
                                ),
                              }))
                            }
                          >
                            <option value="string">string</option>
                            <option value="number">number</option>
                            <option value="boolean">boolean</option>
                            <option value="object">object</option>
                            <option value="array">array</option>
                          </select>
                          <input
                            value={parameter.defaultValue}
                            onChange={(event) =>
                              updateMethod(index, (item) => ({
                                ...item,
                                parameters: item.parameters.map((entry) =>
                                  entry.id === parameter.id ? { ...entry, defaultValue: event.target.value } : entry,
                                ),
                              }))
                            }
                            placeholder="default"
                          />
                          <input
                            type="checkbox"
                            checked={parameter.required}
                            onChange={(event) =>
                              updateMethod(index, (item) => ({
                                ...item,
                                parameters: item.parameters.map((entry) =>
                                  entry.id === parameter.id ? { ...entry, required: event.target.checked } : entry,
                                ),
                              }))
                            }
                          />
                          <input
                            type="checkbox"
                            checked={parameter.visible}
                            onChange={(event) =>
                              updateMethod(index, (item) => ({
                                ...item,
                                parameters: item.parameters.map((entry) =>
                                  entry.id === parameter.id ? { ...entry, visible: event.target.checked } : entry,
                                ),
                              }))
                            }
                          />
                          <button
                            type="button"
                            className="api-store-dialog__param-icon-button"
                            onClick={() => removeParameter(index, parameter.id)}
                            aria-label={copy.removeParameter}
                          >
                            <span className="api-store-dialog__trash-icon" aria-hidden="true" />
                          </button>
                        </div>
                      ))
                    ) : (
                      <div className="api-store-dialog__param-empty">{copy.parameterEmpty}</div>
                    )}
                  </div>
                </section>

                <section className="api-store-dialog__output">
                  <div className="api-store-dialog__output-head">
                    <strong>{copy.responseSettings}</strong>
                    <div>
                      <label>
                        <input
                          type="checkbox"
                          checked={textEditMethods[method.id] ?? Boolean(method.outputSample.trim())}
                          onChange={(event) => setTextEditMethods((current) => ({ ...current, [method.id]: event.target.checked }))}
                        />
                        {copy.textEdit}
                      </label>
                      <button
                        type="button"
                        onClick={() =>
                          setSampleInput({
                            methodIndex: index,
                            value: "{\n  \n}",
                            error: "",
                          })
                        }
                      >
                        {copy.sampleInput}
                      </button>
                    </div>
                  </div>
                  {textEditMethods[method.id] ?? Boolean(method.outputSample.trim()) ? (
                    <div className={`api-store-dialog__json-wrap${hasInvalidJson(method.outputSample) ? " is-invalid" : ""}`}>
                      <span aria-hidden="true">!</span>
                      <textarea
                        className="api-store-dialog__json"
                        value={method.outputSample}
                        onChange={(event) => updateMethod(index, (item) => ({ ...item, outputSample: event.target.value }))}
                        placeholder="{ }"
                      />
                    </div>
                  ) : (
                    <div className="api-store-dialog__output-table">
                      <div className="api-store-dialog__output-row api-store-dialog__output-row--head">
                        <span>Name</span>
                        <span>Data type</span>
                      </div>
                      {method.outputParameters.length > 0 ? (
                        <>
                          {method.outputParameters.some((parameter) => parameter.path === "root") ? null : (
                            <div className="api-store-dialog__output-row">
                              <span>root</span>
                              <span>object</span>
                            </div>
                          )}
                          {method.outputParameters.map((parameter) => (
                            <div key={parameter.id} className="api-store-dialog__output-row">
                              <span style={{ paddingLeft: `${6 + getOutputParameterDepth(parameter.path) * 14}px` }}>
                                {parameter.name || parameter.path || "-"}
                              </span>
                              <span>{parameter.dataType}</span>
                            </div>
                          ))}
                        </>
                      ) : (
                        <div className="api-store-dialog__output-empty" />
                      )}
                    </div>
                  )}
                </section>
              </div>

              <div className="api-store-dialog__method-footer">
                <button
                  type="button"
                  disabled={draft.methods.length <= 1}
                  onClick={() =>
                    setDraft((current) => ({
                      ...current,
                      methods: current.methods.filter((item) => item.id !== method.id),
                    }))
                  }
                >
                  {copy.removeMethod}
                </button>
              </div>
            </section>
          ))}

        <button
          type="button"
          className="api-store-dialog__add-method"
          onClick={() => setDraft((current) => ({ ...current, methods: [...current.methods, createEmptyApiMethod()] }))}
        >
          + {copy.addMethod}
        </button>
      </div>
      <footer className="api-store-dialog__footer">
        <button type="button" className="api-store-dialog__button" onClick={onClose}>
          {copy.cancel}
        </button>
        <button
          type="button"
          className="api-store-dialog__button api-store-dialog__button--primary"
          disabled={
            saving ||
            !draft.name.trim() ||
            !draft.apiKey.trim() ||
            !draft.baseUrl.trim() ||
            hasInvalidOutputText ||
            draft.methods.some((method) => !method.name.trim() || !method.methodUrl.trim())
          }
          onClick={() =>
            onSave({
              ...draft,
              apiKey: draft.apiKey.trim(),
              name: draft.name.trim(),
              baseUrl: draft.baseUrl.trim(),
              methods: draft.methods.map((method) => {
                const isTextEdit = textEditMethods[method.id] ?? Boolean(method.outputSample.trim());
                return {
                  ...method,
                  outputParameters:
                    isTextEdit && method.outputSample.trim()
                      ? buildOutputParametersFromSample(JSON.parse(method.outputSample))
                      : method.outputParameters,
                  outputSample: "",
                };
              }),
              updatedAt: new Date().toISOString(),
              updatedBy: loadAuthSession()?.user.login_id ?? "",
            })
          }
        >
          {copy.save}
        </button>
      </footer>
      {sampleInput ? (
        <div className="api-store-dialog__sample-backdrop" role="presentation">
          <section className="api-store-dialog__sample-dialog" role="dialog" aria-modal="true" aria-label={copy.sampleTitle}>
            <header>
              <strong>{copy.sampleTitle}</strong>
              <button type="button" onClick={() => setSampleInput(null)} aria-label={copy.close}>
                ×
              </button>
            </header>
            <textarea
              value={sampleInput.value}
              onChange={(event) => setSampleInput((current) => (current ? { ...current, value: event.target.value, error: "" } : current))}
              placeholder={copy.samplePlaceholder}
            />
            {sampleInput.error ? <p>{sampleInput.error}</p> : null}
            <footer>
              <button type="button" onClick={() => setSampleInput(null)}>
                {copy.cancel}
              </button>
              <button type="button" className="api-store-dialog__sample-primary" onClick={applyOutputSample}>
                {copy.confirm}
              </button>
            </footer>
          </section>
        </div>
      ) : null}
    </section>
  );
}

export function ApiStoreListPage() {
  const workspace = useStudioWorkspace();
  const { language: uiLanguage } = useI18n();
  const common = API_MANAGEMENT_CATALOGS[uiLanguage];
  const params = useParams<{ botId: string; versionId: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const sectionFetchKeyRef = useRef("");
  const botId = workspace.bot.id || decodeURIComponent(params.botId);
  const versionId = decodeURIComponent(params.versionId);
  const [authSession, setAuthSession] = useState<AuthSession | null>(workspace.session);
  const [bot, setBot] = useState<StudioBotApiItem | null>(workspace.bot);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [editingApi, setEditingApi] = useState<VersionApiAsset | null>(null);
  const [actionMenuOpen, setActionMenuOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    setAuthSession(workspace.session);
    setBot(workspace.bot);
    setLoading(false);
    setErrorMessage("");
    saveLastBotScreen(`/studio/bots/${botId}/versions/${versionId}/intents`);
  }, [botId, versionId, workspace.bot, workspace.session]);

  useEffect(() => {
    const version = bot?.active_version;
    if (!authSession || !bot || !version) {
      return;
    }

    const fetchKey = `${bot.id}:${version.id}`;
    if (sectionFetchKeyRef.current === fetchKey) {
      return;
    }
    sectionFetchKeyRef.current = fetchKey;

    let ignore = false;
    setLoading(true);
    fetchStudioBotVersionApis(authSession.access_token, bot.id, version.id)
      .then((response) => {
        if (ignore) {
          return;
        }
        const nextDocument = withUpdatedApiAssets(normalizeVersionDocument(version.version_json), normalizeApiAssets(response.items));
        const updatedVersion: StudioBotVersionApiItem = {
          ...response.version,
          version_json: nextDocument,
          asset_counts: response.asset_counts,
          scenario_validation: response.scenario_validation,
        };
        setBot((current) => (current ? applyUpdatedVersionToBot(current, updatedVersion) : current));
      })
      .catch((error) => {
        if (!ignore) {
          setErrorMessage(error instanceof Error ? error.message : common.loadFailed);
        }
      })
      .finally(() => {
        if (!ignore) {
          setLoading(false);
        }
      });

    return () => {
      ignore = true;
    };
  }, [authSession, bot, bot?.active_version]);

  const versionDocument = useMemo(() => normalizeVersionDocument(bot?.active_version?.version_json), [bot?.active_version?.version_json]);
  const apis = useMemo(() => normalizeApiAssets(versionDocument.apis), [versionDocument.apis]);
  const filteredApis = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) {
      return apis;
    }
    return apis.filter((api) => `${api.name} ${api.baseUrl} ${api.apiKey}`.toLowerCase().includes(keyword));
  }, [apis, query]);

  const allFilteredSelected = filteredApis.length > 0 && filteredApis.every((api) => selectedIds.includes(api.id));
  const canWriteApi = hasApiWriteRole(authSession?.user.roles ?? []);

  useEffect(() => {
    if (searchParams.get("create") !== "1" || editingApi) {
      return;
    }
    if (!canWriteApi) {
      router.replace(`/studio/bots/${botId}/versions/${versionId}/apis`, { scroll: false });
      setErrorMessage(common.createForbidden);
      return;
    }

    setEditingApi(createEmptyApiAsset());
    router.replace(`/studio/bots/${botId}/versions/${versionId}/apis`, { scroll: false });
  }, [botId, canWriteApi, editingApi, router, searchParams, versionId]);

  async function persistApis(nextApis: VersionApiAsset[], successMessage: string) {
    if (!authSession || !bot?.active_version) {
      router.replace("/login");
      return;
    }
    if (!canWriteApi) {
      setErrorMessage(common.editForbidden);
      return;
    }

    setSaving(true);
    setMessage("");
    setErrorMessage("");
    try {
      const response = await updateStudioBotVersionApis(authSession.access_token, bot.id, bot.active_version.id, nextApis);
      const nextDocument = withUpdatedApiAssets(versionDocument, normalizeApiAssets(response.items));
      const updatedVersion: StudioBotVersionApiItem = {
        ...response.version,
        version_json: nextDocument,
        asset_counts: response.asset_counts,
        scenario_validation: response.scenario_validation,
      };
      setBot((current) => (current ? applyUpdatedVersionToBot(current, updatedVersion) : current));
      setSelectedIds([]);
      setEditingApi(null);
      setMessage(successMessage);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : common.saveFailed);
    } finally {
      setSaving(false);
    }
  }

  function handleDownloadApis(selectedOnly: boolean) {
    const selectedIdSet = new Set(selectedIds);
    const targetApis = selectedOnly ? apis.filter((api) => selectedIdSet.has(api.id)) : apis;
    if (targetApis.length === 0) {
      setErrorMessage(selectedOnly ? common.downloadSelectionRequired : common.downloadEmpty);
      return;
    }
    downloadTextFile(
      selectedOnly ? "api-selected.json" : "api-all.json",
      JSON.stringify(buildApiExportPayload(targetApis), null, 2),
    );
    setMessage(selectedOnly ? formatApiManagementText(common.downloadedSelected, { count: targetApis.length }) : common.downloadedAll);
  }

  async function handleUploadFile(file: File) {
    try {
      const importedApis = parseApiImportPayload(JSON.parse(await file.text()));
      if (importedApis.length === 0) {
        setErrorMessage(common.uploadEmpty);
        return;
      }
      const byKey = new Map(apis.map((api) => [api.apiKey, api]));
      const nextApis = [...apis];
      importedApis.forEach((api) => {
        const existing = byKey.get(api.apiKey);
        if (existing) {
          const index = nextApis.findIndex((item) => item.id === existing.id);
          nextApis[index] = { ...api, id: existing.id, updatedAt: new Date().toISOString() };
        } else {
          nextApis.push({ ...api, id: createApiAssetId(), updatedAt: new Date().toISOString() });
        }
      });
      await persistApis(nextApis, formatApiManagementText(common.uploaded, { count: importedApis.length }));
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : common.uploadReadFailed);
    } finally {
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }

  function handleSaveApi(api: VersionApiAsset) {
    const existingIndex = apis.findIndex((item) => item.id === api.id);
    const nextApis =
      existingIndex >= 0
        ? apis.map((item) => (item.id === api.id ? api : item))
        : [...apis, api];
    void persistApis(nextApis, existingIndex >= 0 ? common.updated : common.created);
  }

  const rows: DataGridRow[] = filteredApis.map((api) => ({
    key: api.id,
    cells: [
      <input
        key="check"
        type="checkbox"
        aria-label={`${api.name} ${common.selectItem}`}
        checked={selectedIds.includes(api.id)}
        onChange={() =>
          setSelectedIds((current) => (current.includes(api.id) ? current.filter((id) => id !== api.id) : [...current, api.id]))
        }
      />,
      "API",
      <Link key="link" href={`/studio/bots/${botId}/versions/${versionId}/apis/${api.id}`}>
        {api.name}
      </Link>,
      api.baseUrl,
      String(api.methods.length),
      "0",
      formatUpdatedAt(api.updatedAt),
      api.updatedBy || "-",
    ],
  }));

  if (editingApi) {
    return (
      <section className="studio-table-page api-store-page api-store-page--editor">
        <ApiEditorDialog initialApi={editingApi} saving={saving} onClose={() => setEditingApi(null)} onSave={handleSaveApi} />
      </section>
    );
  }

  return (
    <section className="studio-table-page api-store-page">
      <header className="studio-table-page__title-row">
        <h1>API</h1>
      </header>

      <div className="studio-table-page__search-row">
        <label className="studio-table-page__search">
          <span aria-hidden="true">⌕</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={common.search} />
        </label>
        <button type="button" className="studio-table-page__filter" aria-label={common.filter}>
          ▾
        </button>
        <div className="studio-table-page__search-actions">
          {canWriteApi ? (
            <button type="button" className="studio-table-page__primary" onClick={() => setEditingApi(createEmptyApiAsset())}>
              + {common.create}
            </button>
          ) : null}
          <button
            type="button"
            className="studio-table-page__ghost studio-table-page__more"
            onClick={() => setActionMenuOpen((current) => !current)}
            aria-label={common.more}
          >
            ⋮
          </button>
          {actionMenuOpen ? (
            <div className="api-store-page__action-menu" role="menu">
              {canWriteApi ? (
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setActionMenuOpen(false);
                    fileInputRef.current?.click();
                  }}
                >
                  {common.upload}
                </button>
              ) : null}
              <button
                type="button"
                role="menuitem"
                disabled={loading || apis.length === 0}
                onClick={() => {
                  setActionMenuOpen(false);
                  handleDownloadApis(false);
                }}
              >
                {common.downloadAll}
              </button>
              <button
                type="button"
                role="menuitem"
                disabled={selectedIds.length === 0}
                onClick={() => {
                  setActionMenuOpen(false);
                  handleDownloadApis(true);
                }}
              >
                {common.downloadSelected}
              </button>
            </div>
          ) : null}
        </div>
      </div>

      <div className="studio-table-page__toolbar">
        <div className="studio-table-page__toolbar-left">
          <strong>{common.total} {filteredApis.length}</strong>
          <button type="button" className="studio-table-page__page-size">
            10 {common.perPage}
          </button>
          {selectedIds.length > 0 ? <span className="studio-table-page__selection">{selectedIds.length} {common.selectItem}</span> : null}
        </div>
        {canWriteApi ? (
          <div className="studio-table-page__toolbar-right">
            <button
              type="button"
              className="studio-table-page__ghost"
              disabled={selectedIds.length === 0 || saving}
              onClick={() => persistApis(apis.filter((api) => !selectedIds.includes(api.id)), common.deletedSelected)}
            >
              {common.delete}
            </button>
          </div>
        ) : null}
      </div>

      {message ? <p className="manual-main__status manual-main__status--success">{message}</p> : null}
      {errorMessage ? <p className="manual-main__status manual-main__status--error">{errorMessage}</p> : null}

      <div className="studio-table-page__grid-scroll">
        <DataGrid
          variant="studio"
          template="44px 120px 220px 1fr 120px 140px 180px 160px"
          columns={[
            <input
              key="check"
              type="checkbox"
              aria-label={common.selectAll}
              checked={allFilteredSelected}
              onChange={(event) => setSelectedIds(event.target.checked ? filteredApis.map((api) => api.id) : [])}
            />,
            common.category,
            <SortHeaderLabel key="api" label={common.apiName} />,
            common.baseUrl,
            common.methodCount,
            common.usedIntents,
            <SortHeaderLabel key="updated" label={common.updatedAt} />,
            common.updatedBy,
          ]}
          rows={rows}
        />
      </div>

      <div className="studio-table-page__pagination">
        <button type="button">◀</button>
        <button type="button" className="is-active">
          1
        </button>
        <button type="button">▶</button>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept=".json,application/json,text/plain"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) {
            void handleUploadFile(file);
          }
        }}
      />

    </section>
  );
}
