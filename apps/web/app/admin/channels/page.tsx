"use client";

import Link from "next/link";
import { ChannelConnectionsPanel } from "@/components/channel-connections-panel";
import { CONNECTION_COPY } from "@/lib/i18n/channel-connections";
import { useEffect, useMemo, useRef, useState } from "react";

import { DataGrid, dataGridCellText, type DataGridRow, type DataGridSortState } from "@/components/data-grid";
import { useI18n } from "@/components/language-provider";
import {
  createChannel,
  deleteChannel,
  fetchChannels,
  testChannelConnection,
  updateChannel,
  type AdminChannelItem,
  type AdminChannelPayload,
} from "@/lib/admin-api";
import { loadAuthSession } from "@/lib/auth";
import { ADMIN_CHANNEL_CATALOGS } from "@/lib/i18n/admin-channels";
import { ADMIN_CHANNEL_DIALOG_CATALOGS, CHANNEL_DELETE_LABELS, CHANNEL_PAGE_MESSAGES, formatChannelDialogText, type AdminChannelDialogCatalog } from "@/lib/i18n/admin-channel-dialog";
import { SUPPORTED_LANGUAGES } from "@/lib/language";
import {
  LIST_PAGE_SIZE_OPTIONS,
  type ListPageSize,
  usePersistedPageSize,
} from "@/lib/use-persisted-page-size";

type ChannelStatus = AdminChannelItem["status"];

function authTypeGuide(authType: string, copy: AdminChannelDialogCatalog) {
  switch (authType) {
    case "token":
      return copy.tokenGuide;
    case "oauth":
      return copy.oauthGuide;
    case "basic":
      return copy.basicGuide;
    default:
      return copy.noneGuide;
  }
}

function isKakaoDraft(provider: string, rendererType: string) {
  return provider.trim().toLowerCase() === "kakao" || rendererType.trim().toLowerCase() === "kakao";
}

function formatDate(value: string, locale = "ko-KR") {
  return new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function channelDataText(channel: AdminChannelItem, key: string, fallback = "-") {
  const value = channel.data_json[key];
  return typeof value === "string" && value.trim() ? value : fallback;
}

function downloadChannels(items: AdminChannelItem[]) {
  const rows = [
    ["순서", "채널 아이디", "채널", "Provider", "렌더러 타입", "사용여부", "생성자", "최종수정자", "최종수정일시", "Endpoint URL", "인증방식", "인증정보JSON", "설명"],
    ...items.map((channel, index) => [
      String(index + 1),
      channel.code,
      channel.name,
      channelDataText(channel, "provider"),
      channelDataText(channel, "renderer_type"),
      channel.status_label,
      channel.creator_name,
      channel.updater_name,
      formatDate(channel.updated_at),
      channelDataText(channel, "endpoint_url", ""),
      channelDataText(channel, "auth_type", "none"),
      JSON.stringify(channel.data_json.auth_config ?? {}),
      channel.description ?? "",
    ]),
  ];
  const csv = rows.map((row) => row.map((cell) => `"${cell.replaceAll('"', '""')}"`).join(",")).join("\n");
  const blob = new Blob([`\ufeff${csv}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "채널 관리.csv";
  anchor.click();
  URL.revokeObjectURL(url);
}

function parseCsv(text: string) {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let inQuote = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const nextChar = text[index + 1];
    if (char === '"' && inQuote && nextChar === '"') {
      cell += '"';
      index += 1;
      continue;
    }
    if (char === '"') {
      inQuote = !inQuote;
      continue;
    }
    if (char === "," && !inQuote) {
      row.push(cell.trim());
      cell = "";
      continue;
    }
    if ((char === "\n" || char === "\r") && !inQuote) {
      if (char === "\r" && nextChar === "\n") {
        index += 1;
      }
      row.push(cell.trim());
      if (row.some(Boolean)) {
        rows.push(row);
      }
      row = [];
      cell = "";
      continue;
    }
    cell += char;
  }

  row.push(cell.trim());
  if (row.some(Boolean)) {
    rows.push(row);
  }
  return rows;
}

export default function AdminChannelsPage() {
  const { language: uiLanguage } = useI18n();
  const copy = ADMIN_CHANNEL_CATALOGS[uiLanguage];
  const dialogCopy = ADMIN_CHANNEL_DIALOG_CATALOGS[uiLanguage];
  const pageMessages = CHANNEL_PAGE_MESSAGES[uiLanguage];
  const locale = SUPPORTED_LANGUAGES.find((item) => item.code === uiLanguage)?.intlLocale ?? "ko-KR";
  const [token, setToken] = useState("");
  const [connectionChannel, setConnectionChannel] = useState<AdminChannelItem | null>(null);
  const [channels, setChannels] = useState<AdminChannelItem[]>([]);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [appliedStatus, setAppliedStatus] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<AdminChannelItem | null>(null);
  const [draft, setDraft] = useState({
    code: "",
    name: "",
    provider: "webchat",
    renderer_type: "webchat",
    endpoint_url: "",
    auth_type: "none",
    auth_config: "",
    description: "",
    status: "active" as ChannelStatus,
  });
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState("");
  const [noticeMessage, setNoticeMessage] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [pageSize, setPageSize] = usePersistedPageSize<ListPageSize>(
    "aidot.page_size.admin.channels",
    10,
    LIST_PAGE_SIZE_OPTIONS,
  );
  const [page, setPage] = useState(1);
  const [sortState, setSortState] = useState<DataGridSortState | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const session = loadAuthSession();
    if (!session) {
      setLoading(false);
      setErrorMessage(pageMessages.loginRequired);
      return;
    }
    setToken(session.access_token);
  }, []);

  useEffect(() => {
    if (!token) {
      return;
    }

    let ignore = false;
    setLoading(true);
    setErrorMessage("");
    fetchChannels(token, {
      query: appliedQuery || undefined,
      status: appliedStatus || undefined,
    })
      .then((response) => {
        if (!ignore) {
          setChannels(response.items);
        }
      })
      .catch((error) => {
        if (!ignore) {
          setErrorMessage(error instanceof Error ? error.message : pageMessages.loadFailed);
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
  }, [appliedQuery, appliedStatus, pageMessages.loadFailed, token]);

  function applySearch() {
    setAppliedQuery(query.trim());
    setAppliedStatus(status);
    setPage(1);
  }

  function resetSearch() {
    setQuery("");
    setStatus("");
    setAppliedQuery("");
    setAppliedStatus("");
    setPage(1);
  }

  function openCreate() {
    setEditing(null);
    setDraft({
      code: "",
      name: "",
      provider: "webchat",
      renderer_type: "webchat",
      endpoint_url: "",
      auth_type: "none",
      auth_config: "",
      description: "",
      status: "active",
    });
    setDialogOpen(true);
  }

  function openEdit(channel: AdminChannelItem) {
    setEditing(channel);
    setDraft({
      code: channel.code,
      name: channel.name,
      provider: String(channel.data_json.provider ?? "webchat"),
      renderer_type: String(channel.data_json.renderer_type ?? "webchat"),
      endpoint_url: String(channel.data_json.endpoint_url ?? ""),
      auth_type: String(channel.data_json.auth_type ?? "none"),
      auth_config: JSON.stringify(channel.data_json.auth_config ?? {}, null, 2),
      description: channel.description ?? "",
      status: channel.status,
    });
    setDialogOpen(true);
  }

  function closeDialog() {
    setDialogOpen(false);
    setEditing(null);
    setDraft({
      code: "",
      name: "",
      provider: "webchat",
      renderer_type: "webchat",
      endpoint_url: "",
      auth_type: "none",
      auth_config: "",
      description: "",
      status: "active",
    });
  }

  async function reload() {
    if (!token) {
      return;
    }
    const response = await fetchChannels(token, {
      query: appliedQuery || undefined,
      status: appliedStatus || undefined,
    });
    setChannels(response.items);
  }

  async function saveChannel() {
    if (!token || !draft.code.trim() || !draft.name.trim()) {
      return;
    }

    let authConfig: Record<string, unknown> = {};
    if (draft.auth_config.trim()) {
      try {
        const parsed = JSON.parse(draft.auth_config) as unknown;
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          authConfig = parsed as Record<string, unknown>;
        } else {
          setNoticeMessage(dialogCopy.jsonObject);
          return;
        }
      } catch {
        setNoticeMessage(dialogCopy.jsonInvalid);
        return;
      }
    }

    const payload: AdminChannelPayload = {
      code: draft.code.trim().toUpperCase(),
      name: draft.name.trim(),
      provider: draft.provider.trim() || "webchat",
      renderer_type: draft.renderer_type.trim() || "webchat",
      endpoint_url: draft.endpoint_url.trim() || null,
      auth_type: draft.auth_type.trim() || "none",
      auth_config: authConfig,
      description: draft.description.trim() || null,
      status: draft.status,
    };

    try {
      if (editing) {
        await updateChannel(token, editing.id, {
          name: payload.name,
          provider: payload.provider,
          renderer_type: payload.renderer_type,
          endpoint_url: payload.endpoint_url,
          auth_type: payload.auth_type,
          auth_config: payload.auth_config,
          description: payload.description,
          status: payload.status,
        });
        setNoticeMessage(dialogCopy.updated);
      } else {
        await createChannel(token, payload);
        setNoticeMessage(dialogCopy.created);
      }
      closeDialog();
      await reload();
    } catch (error) {
      setNoticeMessage(error instanceof Error ? error.message : dialogCopy.saveFailed);
    }
  }

  async function checkChannelConnection(channel: AdminChannelItem) {
    if (!token) {
      return;
    }
    try {
      const result = await testChannelConnection(token, channel.id);
      const suffix = result.issues.length > 0 ? ` (${result.issues.join(" / ")})` : "";
      setNoticeMessage(`${channel.name}: ${result.message}${suffix}`);
    } catch (error) {
      setNoticeMessage(error instanceof Error ? error.message : dialogCopy.connectionFailed);
    }
  }
  async function removeChannel() {
    if (!token || !editing) {
      return;
    }

    try {
      await deleteChannel(token, editing.id);
      setNoticeMessage(dialogCopy.deleted);
      closeDialog();
      await reload();
    } catch (error) {
      setNoticeMessage(error instanceof Error ? error.message : dialogCopy.deleteFailed);
    }
  }

  async function handleUpload(file: File | null) {
    if (!token || !file) {
      return;
    }

    try {
      const text = await file.text();
      const rows = parseCsv(text).filter((row) => row.length >= 3);
      const dataRows = rows[0]?.[1]?.includes("채널 아이디") ? rows.slice(1) : rows;
      let savedCount = 0;
      let skippedCount = 0;

      for (const row of dataRows) {
        const [
          ,
          code,
          name,
          provider,
          rendererType,
          statusLabel,
          ,
          ,
          ,
          endpointUrl = "",
          authType = "none",
          authConfig = "{}",
          description = "",
        ] = row;

        if (!(code ?? "").trim() || !(name ?? "").trim()) {
          skippedCount += 1;
          continue;
        }

        let parsedAuthConfig: Record<string, unknown> = {};
        try {
          const parsed = JSON.parse(authConfig || "{}");
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            parsedAuthConfig = parsed as Record<string, unknown>;
          }
        } catch {
          parsedAuthConfig = {};
        }

        const payload: AdminChannelPayload = {
          code: code.trim().toUpperCase(),
          name: name.trim(),
          provider: (provider || "webchat").trim(),
          renderer_type: (rendererType || "webchat").trim(),
          endpoint_url: endpointUrl.trim() || null,
          auth_type: (authType || "none").trim(),
          auth_config: parsedAuthConfig,
          description: description.trim() || null,
          status: statusLabel === "미사용" || statusLabel === "inactive" ? "inactive" : "active",
        };

        const existing = channels.find((item) => item.code === payload.code);
        if (existing) {
          await updateChannel(token, existing.id, {
            name: payload.name,
            provider: payload.provider,
            renderer_type: payload.renderer_type,
            endpoint_url: payload.endpoint_url,
            auth_type: payload.auth_type,
            auth_config: payload.auth_config,
            description: payload.description,
            status: payload.status,
          });
        } else {
          await createChannel(token, payload);
        }
        savedCount += 1;
      }

      setMenuOpen(false);
      setNoticeMessage(formatChannelDialogText(dialogCopy.uploadResult,{saved:savedCount,skipped:skippedCount}));
      await reload();
    } catch (error) {
      setNoticeMessage(error instanceof Error ? error.message : dialogCopy.uploadFailed);
    }
  }

  const rows: DataGridRow[] = channels.map((channel, index) => ({
    key: channel.id,
    cells: [
      String(index + 1),
      channel.code,
      <button key={channel.id} type="button" className="table-link" onClick={() => openEdit(channel)}>
        {channel.name}
      </button>,
      channelDataText(channel, "provider"),
      channelDataText(channel, "renderer_type"),
      channel.status_label,
      channel.creator_name,
      channel.updater_name,
      formatDate(channel.updated_at, locale),
      <div key={`manage-${channel.id}`} className="admin-page__table-actions">
        <button type="button" className="admin-page__ghost" onClick={() => checkChannelConnection(channel)}>
          {copy.connectionTest}
        </button>
        {channelDataText(channel, "provider") !== "simulator" ? <button type="button" className="admin-page__ghost" onClick={() => setConnectionChannel(channel)}>{CONNECTION_COPY[uiLanguage].title}</button> : null}
        <button type="button" className="admin-page__ghost" onClick={() => openEdit(channel)}>
          {copy.edit}
        </button>
      </div>,
    ],
  }));
  const sortedRows = useMemo(() => {
    if (!sortState) {
      return rows;
    }
    return [...rows].sort((left, right) => {
      const leftText = dataGridCellText(left.cells[sortState.columnIndex]);
      const rightText = dataGridCellText(right.cells[sortState.columnIndex]);
      const leftNumber = Number(leftText.replaceAll(",", ""));
      const rightNumber = Number(rightText.replaceAll(",", ""));
      const bothNumeric =
        leftText.trim() !== "" && rightText.trim() !== "" && Number.isFinite(leftNumber) && Number.isFinite(rightNumber);
      const result = bothNumeric
        ? leftNumber - rightNumber
        : leftText.localeCompare(rightText, locale, { numeric: true, sensitivity: "base" });
      return sortState.direction === "asc" ? result : -result;
    });
  }, [locale, rows, sortState]);
  const totalPages = Math.max(1, Math.ceil(sortedRows.length / pageSize));
  const pagedRows = sortedRows.slice((page - 1) * pageSize, page * pageSize);

  useEffect(() => {
    setPage(1);
  }, [pageSize, sortState]);

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  return (
    <section className="admin-page">
      <h2>{copy.title}</h2>

      <div className="admin-page__search-row admin-channels__search-row">
        <label className="admin-page__search">
          <span aria-hidden="true">⌕</span>
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={copy.searchPlaceholder}
          />
        </label>
        <select className="login-select" value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="">{copy.allStatus}</option>
          <option value="active">{copy.active}</option>
          <option value="inactive">{copy.inactive}</option>
        </select>
        <button type="button" className="admin-page__filter" onClick={resetSearch}>
          {copy.reset}
        </button>
        <div className="admin-page__search-actions">
          <button type="button" className="admin-page__primary" onClick={applySearch}>
            {copy.search}
          </button>
          <button type="button" className="admin-page__primary" onClick={openCreate}>
            + {copy.create}
          </button>
          <div className="admin-common-variables__more">
            <button
              type="button"
              className="admin-common-variables__more-button"
              onClick={() => setMenuOpen((current) => !current)}
              aria-label={copy.more}
            >
              <span className="admin-common-variables__more-dots" aria-hidden="true">
                <span />
                <span />
                <span />
              </span>
            </button>
            {menuOpen ? (
              <div className="admin-common-variables__menu">
                <button
                  type="button"
                  onClick={() => {
                    setMenuOpen(false);
                    fileInputRef.current?.click();
                  }}
                >
                  {copy.upload}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setMenuOpen(false);
                    downloadChannels(channels);
                  }}
                >
                  {copy.download}
                </button>
              </div>
            ) : null}
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv,text/csv"
              className="admin-common-variables__file"
              onChange={(event) => {
                void handleUpload(event.target.files?.[0] ?? null);
                event.currentTarget.value = "";
              }}
            />
          </div>
        </div>
      </div>

      <div className="admin-page__toolbar">
        <div className="admin-page__toolbar-left">
          <strong>{copy.total} {channels.length}</strong>
          <label className="manual-main__mini-select manual-main__mini-select--select">
            <select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value) as ListPageSize)}>
              {LIST_PAGE_SIZE_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option} {copy.perPage}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {noticeMessage ? <p className="admin-page__empty">{noticeMessage}</p> : null}
      {errorMessage ? (
        <div className="admin-state-box">
          <p>{errorMessage}</p>
          <Link href="/login" className="ghost-pill">
            {copy.login}
          </Link>
        </div>
      ) : null}

      {!errorMessage ? (
        <>
          <DataGrid
            variant="admin"
            template="70px 130px 150px 120px 130px 100px 130px 130px 170px 180px"
            columns={[copy.order, copy.channelId, copy.channel, "Provider", copy.renderer, copy.status, copy.creator, copy.updatedBy, copy.updatedAt, copy.manage]}
            rows={loading ? [] : pagedRows}
            sortState={sortState}
            onSort={setSortState}
          />

          {loading ? <p className="admin-page__empty">{copy.loading}</p> : null}
          {!loading && rows.length === 0 ? <p className="admin-page__empty">{copy.empty}</p> : null}

          <div className="admin-page__pagination">
            <button type="button" disabled={page === 1} onClick={() => setPage(1)}>
              «
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
              »
            </button>
          </div>
        </>
      ) : null}

      {connectionChannel && token ? <ChannelConnectionsPanel key={connectionChannel.id} token={token} channel={connectionChannel} onClose={() => setConnectionChannel(null)} /> : null}

      {dialogOpen ? (
        <div className="settings-dialog-backdrop">
          <div className="settings-dialog admin-variable-dialog">
            <div className="settings-dialog__header">
              <strong>{editing ? dialogCopy.editTitle : dialogCopy.createTitle}</strong>
              <button type="button" className="settings-dialog__close" onClick={closeDialog} aria-label={dialogCopy.close}>
                ×
              </button>
            </div>
            <div className="admin-variable-dialog__body">
              {isKakaoDraft(draft.provider, draft.renderer_type) ? (
                <div className="admin-form-guide admin-form-guide--wide">
                  <strong>{dialogCopy.kakaoGuideTitle}</strong>
                  <p>{dialogCopy.kakaoRecommended}</p>
                  <p>{dialogCopy.kakaoAuth}</p>
                  <p>{dialogCopy.kakaoUnsupported}</p>
                </div>
              ) : null}
              <label>
                <span>{dialogCopy.channelId}</span>
                <input
                  type="text"
                  value={draft.code}
                  disabled={Boolean(editing)}
                  onChange={(event) => setDraft((current) => ({ ...current, code: event.target.value }))}
                />
              </label>
              <label>
                <span>{dialogCopy.channelName}</span>
                <input
                  type="text"
                  value={draft.name}
                  onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))}
                />
              </label>
              <label>
                <span>Provider</span>
                <select
                  className="login-select"
                  value={draft.provider}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      provider: event.target.value,
                      renderer_type: event.target.value === "ms_teams" ? "adaptive_card" : event.target.value,
                    }))
                  }
                >
                  <option value="webchat">Webchat</option>
                  <option value="kakao">Kakao</option>
                  <option value="ms_teams">MS Teams</option>
                  <option value="slack">Slack</option>
                  <option value="discord">Discord</option>
                  <option value="telegram">Telegram</option>
                  <option value="google_chat">Google Chat</option>
                  <option value="line">LINE</option>
                  <option value="simulator">Simulator</option>
                </select>
              </label>
              <label>
                <span>{dialogCopy.renderer}</span>
                <input
                  type="text"
                  value={draft.renderer_type}
                  onChange={(event) => setDraft((current) => ({ ...current, renderer_type: event.target.value }))}
                />
              </label>
              <label>
                <span>Endpoint URL</span>
                <input
                  type="text"
                  value={draft.endpoint_url}
                  placeholder="https://..."
                  onChange={(event) => setDraft((current) => ({ ...current, endpoint_url: event.target.value }))}
                />
              </label>
              <label>
                <span className="admin-inline-label">
                  {dialogCopy.authType}
                  <span
                    className="admin-inline-help"
                    data-help={dialogCopy.authHelp}
                    tabIndex={0}
                    role="img"
                    aria-label={dialogCopy.authHelpLabel}
                  >
                    i
                  </span>
                </span>
                <select
                  className="login-select"
                  value={draft.auth_type}
                  onChange={(event) => setDraft((current) => ({ ...current, auth_type: event.target.value }))}
                >
                  <option value="none">{dialogCopy.none}</option>
                  <option value="token">Token</option>
                  <option value="oauth">OAuth</option>
                  <option value="basic">Basic</option>
                </select>
              </label>
              <p className="admin-form-guide__inline">{authTypeGuide(draft.auth_type, dialogCopy)}</p>
              <label>
                <span className="admin-inline-label">
                  {dialogCopy.authJson}
                  <span
                    className="admin-inline-help"
                    data-help={dialogCopy.authJsonHelp}
                    tabIndex={0}
                    role="img"
                    aria-label={dialogCopy.authJsonLabel}
                  >
                    i
                  </span>
                </span>
                <textarea
                  value={draft.auth_config}
                  placeholder={'{"token":"..."}'}
                  onChange={(event) => setDraft((current) => ({ ...current, auth_config: event.target.value }))}
                />
              </label>
              <label>
                <span>{dialogCopy.description}</span>
                <textarea
                  value={draft.description}
                  onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))}
                />
              </label>
              <label>
                <span>{dialogCopy.usage}</span>
                <select
                  className="login-select"
                  value={draft.status}
                  onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value as ChannelStatus }))}
                >
                  <option value="active">{copy.active}</option>
                  <option value="inactive">{copy.inactive}</option>
                </select>
              </label>
            </div>
            <div className="entity-editor-dialog__footer">
              {editing ? (
                <button type="button" className="secondary-action" onClick={removeChannel}>
                  {CHANNEL_DELETE_LABELS[uiLanguage]}
                </button>
              ) : null}
              <button type="button" className="secondary-action" onClick={closeDialog}>
                {dialogCopy.cancel}
              </button>
              <button type="button" className="primary-action" onClick={saveChannel}>
                {dialogCopy.confirm}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
