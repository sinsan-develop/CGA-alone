"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { ApiEditorDialog } from "@/components/api-store-list-page";
import { useI18n } from "@/components/language-provider";
import { DataGrid, type DataGridRow } from "@/components/data-grid";
import { SortHeaderLabel } from "@/components/sort-header-label";
import {
  buildApiExportPayload,
  createApiAssetId,
  createEmptyApiAsset,
  normalizeApiAssets,
  parseApiImportPayload,
  type VersionApiAsset,
} from "@/lib/api-assets";
import { hasApiWriteRole, loadAuthSession, type AuthSession } from "@/lib/auth";
import { fetchStudioGroupApis, updateStudioGroupApis } from "@/lib/studio-bots-api";
import { API_MANAGEMENT_CATALOGS, formatApiManagementText } from "@/lib/i18n/api-management";
import {
  LIST_PAGE_SIZE_OPTIONS,
  type ListPageSize,
  usePersistedPageSize,
} from "@/lib/use-persisted-page-size";

type SortKey = "category" | "name" | "baseUrl" | "methods" | "usage" | "updatedAt" | "updatedBy";

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

function apiIdentity(api: VersionApiAsset) {
  return api.apiKey || api.id;
}

function mergeApisByApiKey(currentApis: VersionApiAsset[], incomingApis: VersionApiAsset[]) {
  const byKey = new Map(currentApis.map((api) => [apiIdentity(api), api]));
  const nextApis = [...currentApis];

  incomingApis.forEach((api) => {
    const key = apiIdentity(api);
    const existing = byKey.get(key);
    if (existing) {
      const index = nextApis.findIndex((item) => apiIdentity(item) === key);
      nextApis[index] = {
        ...api,
        id: existing.id,
        apiKey: existing.apiKey || api.apiKey,
        updatedAt: new Date().toISOString(),
      };
      return;
    }

    nextApis.push({
      ...api,
      id: api.id || createApiAssetId(),
      apiKey: api.apiKey || createApiAssetId(),
      updatedAt: new Date().toISOString(),
    });
  });

  return nextApis;
}

export function GroupApiListPage() {
  const { language: uiLanguage } = useI18n();
  const copy = API_MANAGEMENT_CATALOGS[uiLanguage];
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [authSession, setAuthSession] = useState<AuthSession | null>(null);
  const [apis, setApis] = useState<VersionApiAsset[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [editingApi, setEditingApi] = useState<VersionApiAsset | null>(null);
  const [actionMenuOpen, setActionMenuOpen] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("updatedAt");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("desc");
  const [pageSize, setPageSize] = usePersistedPageSize<ListPageSize>(
    "aidot.page_size.group_api.list",
    10,
    LIST_PAGE_SIZE_OPTIONS,
  );
  const [page, setPage] = useState(1);

  useEffect(() => {
    const session = loadAuthSession();
    setAuthSession(session);
    if (!session) {
      setLoading(false);
      setErrorMessage(copy.loginRequired);
      return;
    }

    let ignore = false;
    setLoading(true);
    setErrorMessage("");

    fetchStudioGroupApis(session.access_token)
      .then((response) => {
        if (!ignore) {
          setApis(normalizeApiAssets(response.items));
        }
      })
      .catch((error) => {
        if (!ignore) {
          setErrorMessage(error instanceof Error ? error.message : copy.loadFailed);
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
  }, []);

  const filteredApis = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    const rows = keyword
      ? apis.filter((api) =>
          [api.name, api.description, api.baseUrl, api.apiKey].some((value) => value.toLowerCase().includes(keyword)),
        )
      : apis;

    return [...rows].sort((left, right) => {
      const leftValue = (() => {
        switch (sortKey) {
          case "category":
            return "API";
          case "name":
            return left.name;
          case "baseUrl":
            return left.baseUrl;
          case "methods":
            return left.methods.length;
          case "usage":
            return left.usageCount ?? 0;
          case "updatedBy":
            return left.updatedBy;
          case "updatedAt":
          default:
            return left.updatedAt;
        }
      })();
      const rightValue = (() => {
        switch (sortKey) {
          case "category":
            return "API";
          case "name":
            return right.name;
          case "baseUrl":
            return right.baseUrl;
          case "methods":
            return right.methods.length;
          case "usage":
            return right.usageCount ?? 0;
          case "updatedBy":
            return right.updatedBy;
          case "updatedAt":
          default:
            return right.updatedAt;
        }
      })();
      const compare =
        typeof leftValue === "number" && typeof rightValue === "number"
          ? leftValue - rightValue
          : String(leftValue).localeCompare(String(rightValue), "ko-KR");

      return sortDirection === "asc" ? compare : compare * -1;
    });
  }, [apis, query, sortDirection, sortKey]);

  const totalPages = Math.max(1, Math.ceil(filteredApis.length / pageSize));
  const pagedApis = useMemo(() => {
    const startIndex = (page - 1) * pageSize;
    return filteredApis.slice(startIndex, startIndex + pageSize);
  }, [filteredApis, page, pageSize]);

  useEffect(() => {
    setPage(1);
  }, [pageSize, query, sortDirection, sortKey]);

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  const allPagedSelected = pagedApis.length > 0 && pagedApis.every((api) => selectedIds.includes(apiIdentity(api)));
  const canWriteApi = hasApiWriteRole(authSession?.user.roles ?? []);

  const rows: DataGridRow[] = pagedApis.map((api) => {
    const rowId = apiIdentity(api);
    return {
      key: rowId,
      cells: [
        <input
          key="check"
          type="checkbox"
          aria-label={`${api.name} ${copy.selectItem}`}
          checked={selectedIds.includes(rowId)}
          onChange={() =>
            setSelectedIds((current) => (current.includes(rowId) ? current.filter((id) => id !== rowId) : [...current, rowId]))
          }
        />,
        "API",
        <Link
          key="api-name"
          className="api-store-page__name-link"
          href={`/studio/apis/${encodeURIComponent(rowId)}`}
        >
          {api.name || "-"}
        </Link>,
        api.baseUrl || "-",
        api.methods.length,
        api.usageCount ?? 0,
        formatUpdatedAt(api.updatedAt),
        api.updatedBy || "-",
      ],
    };
  });

  function toggleSort(nextKey: SortKey) {
    if (sortKey === nextKey) {
      setSortDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }

    setSortKey(nextKey);
    setSortDirection("asc");
  }

  async function saveApis(nextApis: VersionApiAsset[], successMessage: string) {
    if (!authSession) {
      setErrorMessage(copy.loginRequired);
      return;
    }
    setSaving(true);
    setMessage("");
    setErrorMessage("");
    try {
      const response = await updateStudioGroupApis(authSession.access_token, nextApis);
      setApis(normalizeApiAssets(response.items));
      setSelectedIds([]);
      setEditingApi(null);
      setMessage(successMessage);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : copy.saveFailed);
    } finally {
      setSaving(false);
    }
  }

  function openApiCreate() {
    if (!canWriteApi) {
      setErrorMessage(copy.createForbidden);
      return;
    }
    setEditingApi(createEmptyApiAsset());
  }

  function handleSaveApi(api: VersionApiAsset) {
    if (!canWriteApi) {
      setErrorMessage(copy.saveForbidden);
      return;
    }

    const now = new Date().toISOString();
    const preparedApi = {
      ...api,
      id: api.id || createApiAssetId(),
      type: "api" as const,
      category: "API",
      apiKey: api.apiKey || createApiAssetId(),
      updatedAt: now,
      updatedBy: authSession?.user.login_id ?? api.updatedBy,
    };
    const targetKey = apiIdentity(api);
    const existingIndex = apis.findIndex((item) => apiIdentity(item) === targetKey || item.id === api.id);
    const nextApis =
      existingIndex >= 0
        ? apis.map((item, index) => (index === existingIndex ? { ...preparedApi, apiKey: item.apiKey || preparedApi.apiKey } : item))
        : [...apis, preparedApi];

    void saveApis(nextApis, existingIndex >= 0 ? copy.updated : copy.created);
  }

  function handleDownloadApis(selectedOnly: boolean) {
    const selectedIdSet = new Set(selectedIds);
    const targetApis = selectedOnly ? apis.filter((api) => selectedIdSet.has(apiIdentity(api))) : filteredApis;

    if (targetApis.length === 0) {
      setErrorMessage(selectedOnly ? copy.downloadSelectionRequired : copy.downloadEmpty);
      return;
    }

    setErrorMessage("");
    downloadTextFile(
      selectedOnly ? "api-selected.json" : "api-all.json",
      JSON.stringify(buildApiExportPayload(targetApis), null, 2),
    );
    setMessage(selectedOnly ? formatApiManagementText(copy.downloadedSelected, { count: targetApis.length }) : copy.downloadedAll);
  }

  async function handleUploadFile(file: File) {
    if (!canWriteApi) {
      setErrorMessage(copy.uploadForbidden);
      return;
    }

    setSaving(true);
    setMessage("");
    setErrorMessage("");
    try {
      const importedApis = parseApiImportPayload(JSON.parse(await file.text()));
      if (importedApis.length === 0) {
        setErrorMessage(copy.uploadEmpty);
        return;
      }
      await saveApis(mergeApisByApiKey(apis, importedApis), formatApiManagementText(copy.uploaded, { count: importedApis.length }));
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : copy.uploadReadFailed);
    } finally {
      setSaving(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }

  if (editingApi) {
    const existing = apis.some((api) => apiIdentity(api) === apiIdentity(editingApi) || api.id === editingApi.id);
    return (
      <section className="studio-table-page api-store-page api-store-page--editor">
        <ApiEditorDialog
          initialApi={editingApi}
          apiKeyLocked={existing}
          saving={saving}
          onClose={() => setEditingApi(null)}
          onSave={handleSaveApi}
        />
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
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={copy.search}
          />
        </label>
        <button type="button" className="studio-table-page__filter" aria-label={copy.filter}>
          ▾
        </button>
        <div className="studio-table-page__search-actions">
          {canWriteApi ? (
            <button type="button" className="studio-table-page__primary" onClick={openApiCreate} disabled={saving}>
              + {copy.create}
            </button>
          ) : null}
          <button
            type="button"
            className="studio-table-page__ghost studio-table-page__more"
            onClick={() => setActionMenuOpen((current) => !current)}
            aria-label={copy.more}
          >
            ⋮
          </button>
          {actionMenuOpen ? (
            <div className="api-store-page__action-menu" role="menu">
              {canWriteApi ? (
                <button
                  type="button"
                  role="menuitem"
                  disabled={saving}
                  onClick={() => {
                    setActionMenuOpen(false);
                    fileInputRef.current?.click();
                  }}
                >
                  {copy.upload}
                </button>
              ) : null}
              <button
                type="button"
                role="menuitem"
                disabled={loading || filteredApis.length === 0}
                onClick={() => {
                  setActionMenuOpen(false);
                  handleDownloadApis(false);
                }}
              >
                {copy.downloadAll}
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
                {copy.downloadSelected}
              </button>
            </div>
          ) : null}
          <input
            ref={fileInputRef}
            type="file"
            accept="application/json,.json"
            className="api-store-page__file"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) {
                void handleUploadFile(file);
              }
            }}
          />
        </div>
      </div>

      <div className="studio-table-page__toolbar">
        <div className="studio-table-page__toolbar-left">
          <strong>{copy.total} {filteredApis.length}</strong>
          <label className="manual-main__mini-select manual-main__mini-select--select">
            <select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value) as ListPageSize)}>
              {LIST_PAGE_SIZE_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option} {copy.perPage}
                </option>
              ))}
            </select>
          </label>
          {canWriteApi ? (
            <button
              type="button"
              className="studio-table-page__ghost"
              disabled={selectedIds.length === 0 || saving}
              onClick={() => saveApis(apis.filter((api) => !selectedIds.includes(apiIdentity(api))), copy.deletedSelected)}
            >
              {copy.delete}
            </button>
          ) : null}
        </div>
      </div>

      <DataGrid
        variant="studio"
        columns={[
          <input
            key="check"
            type="checkbox"
            aria-label={copy.selectAll}
            checked={allPagedSelected}
            onChange={(event) =>
              setSelectedIds((current) => {
                const pageIds = pagedApis.map((api) => apiIdentity(api));
                if (event.target.checked) {
                  return Array.from(new Set([...current, ...pageIds]));
                }
                return current.filter((id) => !pageIds.includes(id));
              })
            }
          />,
          <button key="category" type="button" className="settings-sort-button" onClick={() => toggleSort("category")}>
            <SortHeaderLabel label={copy.category} direction={sortKey === "category" ? sortDirection : "none"} />
          </button>,
          <button key="api" type="button" className="settings-sort-button" onClick={() => toggleSort("name")}>
            <SortHeaderLabel label={copy.apiName} direction={sortKey === "name" ? sortDirection : "none"} />
          </button>,
          <button key="base" type="button" className="settings-sort-button" onClick={() => toggleSort("baseUrl")}>
            <SortHeaderLabel label={copy.baseUrl} direction={sortKey === "baseUrl" ? sortDirection : "none"} />
          </button>,
          <button key="methods" type="button" className="settings-sort-button" onClick={() => toggleSort("methods")}>
            <SortHeaderLabel label={copy.methodCount} direction={sortKey === "methods" ? sortDirection : "none"} />
          </button>,
          <button key="usage" type="button" className="settings-sort-button" onClick={() => toggleSort("usage")}>
            <SortHeaderLabel label={copy.usedIntents} direction={sortKey === "usage" ? sortDirection : "none"} />
          </button>,
          <button key="updated" type="button" className="settings-sort-button" onClick={() => toggleSort("updatedAt")}>
            <SortHeaderLabel label={copy.updatedAt} direction={sortKey === "updatedAt" ? sortDirection : "none"} />
          </button>,
          <button key="updatedBy" type="button" className="settings-sort-button" onClick={() => toggleSort("updatedBy")}>
            <SortHeaderLabel label={copy.updatedBy} direction={sortKey === "updatedBy" ? sortDirection : "none"} />
          </button>,
        ]}
        rows={rows}
        template="44px 120px 260px minmax(280px, 1fr) 110px 130px 160px 140px"
      />

      <div className="studio-table-page__pagination">
        <button type="button" disabled={page === 1} onClick={() => setPage(1)}>
          ◀
        </button>
        <button type="button" disabled={page === 1} onClick={() => setPage((current) => Math.max(1, current - 1))}>
          ‹
        </button>
        <button type="button" className="is-active">
          {page}
        </button>
        <button type="button" disabled={page === totalPages} onClick={() => setPage((current) => Math.min(totalPages, current + 1))}>
          ›
        </button>
        <button type="button" disabled={page === totalPages} onClick={() => setPage(totalPages)}>
          ▶
        </button>
      </div>

      {loading ? <p className="api-store-page__empty">{copy.loading}</p> : null}
      {!loading && authSession && rows.length === 0 ? <p className="api-store-page__empty">{copy.empty}</p> : null}
      {message ? <p className="form-message form-message--success">{message}</p> : null}
      {errorMessage ? <p className="form-message form-message--error">{errorMessage}</p> : null}
    </section>
  );
}
