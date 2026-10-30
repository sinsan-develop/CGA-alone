"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { DataGrid, dataGridCellText, type DataGridRow, type DataGridSortState } from "@/components/data-grid";
import { useI18n } from "@/components/language-provider";
import {
  createTemplate,
  deleteTemplate,
  fetchChannels,
  fetchTemplates,
  updateTemplate,
  type AdminChannelItem,
  type AdminTemplateItem,
  type AdminTemplatePayload,
} from "@/lib/admin-api";
import { loadAuthSession } from "@/lib/auth";
import { ADMIN_TEMPLATE_CATALOGS, formatAdminTemplateText } from "@/lib/i18n/admin-templates";
import { ADMIN_TEMPLATE_DEFAULT_CATALOGS, type AdminTemplateDefaultCatalog } from "@/lib/i18n/admin-template-defaults";
import { SUPPORTED_LANGUAGES } from "@/lib/language";
import {
  LIST_PAGE_SIZE_OPTIONS,
  type ListPageSize,
  usePersistedPageSize,
} from "@/lib/use-persisted-page-size";

type TemplateStatus = AdminTemplateItem["status"];

const RENDERER_OPTIONS = [
  { value: "text", label: "text" },
  { value: "html", label: "Html" },
  { value: "card", label: "Card" },
  { value: "table", label: "Table" },
  { value: "button", label: "Button" },
  { value: "link-button", label: "Link Button" },
  { value: "form", label: "Form(Rich)" },
  { value: "carousel", label: "Carousel" },
  { value: "dtmf", label: "DTMF" },
  { value: "form-a-card", label: "Form(A Card)" },
  { value: "simple-text", label: "Kakao Simple Text" },
  { value: "quick-reply", label: "Kakao Quick Replies" },
  { value: "basic-card", label: "Kakao Basic Card" },
  { value: "list-card", label: "Kakao List Card" },
  { value: "carousel", label: "Kakao Carousel" },
];

function isKakaoTemplateChannel(value: string) {
  return value.trim().toUpperCase() === "KAKAO";
}

const TELEGRAM_RENDERER_TYPES = ["text", "button", "card", "table", "link-button", "carousel"];
const DISCORD_RENDERER_TYPES = ["text", "button", "card", "table", "link-button", "carousel"];
const SLACK_RENDERER_TYPES = ["text", "button", "card", "table", "link-button", "carousel"];

const TEMPLATE_RENDERER_DEFAULTS: Record<string, { itemTypes: string }> = {
  text: { itemTypes: "text" }, html: { itemTypes: "html" }, card: { itemTypes: "title, imageUrl, description" },
  table: { itemTypes: "table" }, button: { itemTypes: "button" }, "link-button": { itemTypes: "label, url" },
  form: { itemTypes: "formMessage" }, carousel: { itemTypes: "carousel" }, dtmf: { itemTypes: "shortText, stepper, stepper" },
  "form-a-card": { itemTypes: "adaptiveCard" }, "simple-text": { itemTypes: "text" },
  "quick-reply": { itemTypes: "text, quickReplies" }, "basic-card": { itemTypes: "title, description, imageUrl, buttons" },
  "list-card": { itemTypes: "header, items" },
};

function normalizeTemplateDraftByChannel(
  draft: {
    channel_code: string;
    name: string;
    renderer_type: string;
    item_types: string;
    description: string;
    status: TemplateStatus;
  },
  rendererDefaults: AdminTemplateDefaultCatalog,
) {
  const allowedRendererTypes = isKakaoTemplateChannel(draft.channel_code)
    ? ["simple-text", "quick-reply", "basic-card", "list-card", "carousel"]
    : draft.channel_code.trim().toUpperCase() === "SLACK"
      ? SLACK_RENDERER_TYPES
      : draft.channel_code.trim().toUpperCase() === "DISCORD"
        ? DISCORD_RENDERER_TYPES
        : draft.channel_code.trim().toUpperCase() === "TELEGRAM"
          ? TELEGRAM_RENDERER_TYPES
      : RENDERER_OPTIONS.map((option) => option.value).filter(
        (value) => !["simple-text", "quick-reply", "basic-card", "list-card"].includes(value),
      );
  const normalizedRendererType = allowedRendererTypes.includes(draft.renderer_type)
    ? draft.renderer_type
    : allowedRendererTypes[0] ?? "text";
  const defaults = TEMPLATE_RENDERER_DEFAULTS[normalizedRendererType];
  return {
    ...draft,
    renderer_type: normalizedRendererType,
    item_types: defaults?.itemTypes ?? draft.item_types,
    description: draft.description.trim() ? draft.description : rendererDefaults[normalizedRendererType as keyof AdminTemplateDefaultCatalog] ?? draft.description,
  };
}

function formatDate(value: string,locale="ko-KR") {
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

function downloadTemplates(items: AdminTemplateItem[]) {
  const rows = [
    ["채널", "템플릿 이름", "렌더러 타입", "아이템 타입", "사용여부", "최종수정자", "최종수정일시", "설명"],
    ...items.map((item) => [
      item.channel_code,
      item.name,
      item.renderer_type,
      item.item_types,
      item.status_label,
      item.updater_name,
      formatDate(item.updated_at),
      item.description ?? "",
    ]),
  ];
  const csv = rows.map((row) => row.map((cell) => `"${cell.replaceAll('"', '""')}"`).join(",")).join("\n");
  const blob = new Blob([`\ufeff${csv}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "템플릿 목록.csv";
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

export default function AdminTemplatesPage() {
  const {language:uiLanguage}=useI18n();
  const copy=ADMIN_TEMPLATE_CATALOGS[uiLanguage];
  const rendererDefaults=ADMIN_TEMPLATE_DEFAULT_CATALOGS[uiLanguage];
  const locale=SUPPORTED_LANGUAGES.find((item)=>item.code===uiLanguage)?.intlLocale??"ko-KR";
  const [token, setToken] = useState("");
  const [templates, setTemplates] = useState<AdminTemplateItem[]>([]);
  const [activeChannels, setActiveChannels] = useState<AdminChannelItem[]>([]);
  const [query, setQuery] = useState("");
  const [channelCode, setChannelCode] = useState("");
  const [status, setStatus] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [appliedChannelCode, setAppliedChannelCode] = useState("");
  const [appliedStatus, setAppliedStatus] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<AdminTemplateItem | null>(null);
  const [draft, setDraft] = useState({
    channel_code: "",
    name: "",
    renderer_type: "text",
    item_types: "text",
    description: "",
    status: "active" as TemplateStatus,
  });
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState("");
  const [noticeMessage, setNoticeMessage] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [pageSize, setPageSize] = usePersistedPageSize<ListPageSize>(
    "aidot.page_size.admin.templates",
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
      setErrorMessage(copy.loginRequired);
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
    fetchTemplates(token, {
      query: appliedQuery || undefined,
      channelCode: appliedChannelCode || undefined,
      status: appliedStatus || undefined,
    })
      .then((response) => {
        if (!ignore) {
          setTemplates(response.items);
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
  }, [appliedChannelCode, appliedQuery, appliedStatus, copy.loadFailed, token]);

  useEffect(() => {
    if (!token) {
      return;
    }

    let ignore = false;
    fetchChannels(token, { status: "active" })
      .then((response) => {
        if (ignore) {
          return;
        }
        const channels = response.items.filter((channel) => channel.status === "active");
        setActiveChannels(channels);
        setDraft((current) => {
          if (current.channel_code || !channels[0]) {
            return current;
          }
          return normalizeTemplateDraftByChannel({ ...current, channel_code: channels[0].code }, rendererDefaults);
        });
      })
      .catch((error) => {
        if (!ignore) {
          setNoticeMessage(error instanceof Error ? error.message : copy.channelsFailed);
        }
      });

    return () => {
      ignore = true;
    };
  }, [copy.channelsFailed, rendererDefaults, token]);

  async function reload() {
    if (!token) {
      return;
    }
    const response = await fetchTemplates(token, {
      query: appliedQuery || undefined,
      channelCode: appliedChannelCode || undefined,
      status: appliedStatus || undefined,
    });
    setTemplates(response.items);
  }

  function applySearch() {
    setAppliedQuery(query.trim());
    setAppliedChannelCode(channelCode.trim().toUpperCase());
    setAppliedStatus(status);
    setPage(1);
  }

  function resetSearch() {
    setQuery("");
    setChannelCode("");
    setStatus("");
    setAppliedQuery("");
    setAppliedChannelCode("");
    setAppliedStatus("");
    setPage(1);
  }

  function openCreate() {
    setEditing(null);
    setDraft(normalizeTemplateDraftByChannel({
      channel_code: activeChannels[0]?.code ?? "",
      name: "",
      renderer_type: "text",
      item_types: "text",
      description: "",
      status: "active",
    }, rendererDefaults));
    setDialogOpen(true);
  }

  function openEdit(template: AdminTemplateItem) {
    setEditing(template);
    setDraft({
      channel_code: template.channel_code,
      name: template.name,
      renderer_type: template.renderer_type,
      item_types: template.item_types,
      description: template.description ?? "",
      status: template.status,
    });
    setDialogOpen(true);
  }

  function closeDialog() {
    setDialogOpen(false);
    setEditing(null);
  }

  async function saveTemplate() {
    if (!token || !draft.channel_code.trim() || !draft.name.trim() || !draft.renderer_type.trim()) {
      return;
    }

    const payload: AdminTemplatePayload = {
      channel_code: draft.channel_code.trim().toUpperCase(),
      name: draft.name.trim(),
      renderer_type: draft.renderer_type.trim(),
      item_types: draft.item_types.trim(),
      description: draft.description.trim() || null,
      status: draft.status,
    };

    try {
      if (editing) {
        await updateTemplate(token, editing.id, {
          name: payload.name,
          renderer_type: payload.renderer_type,
          item_types: payload.item_types,
          description: payload.description,
          status: payload.status,
        });
        setNoticeMessage(copy.updated);
      } else {
        await createTemplate(token, payload);
        setNoticeMessage(copy.created);
      }
      closeDialog();
      await reload();
    } catch (error) {
      setNoticeMessage(error instanceof Error ? error.message : copy.saveFailed);
    }
  }

  async function removeTemplate() {
    if (!token || !editing) {
      return;
    }

    try {
      await deleteTemplate(token, editing.id);
      setNoticeMessage(copy.deleted);
      closeDialog();
      await reload();
    } catch (error) {
      setNoticeMessage(error instanceof Error ? error.message : copy.deleteFailed);
    }
  }

  async function handleUpload(file: File | null) {
    if (!token || !file) {
      return;
    }

    try {
      const text = await file.text();
      const rows = parseCsv(text).filter((row) => row.length >= 4);
      const hasHeader = rows[0]?.[0]?.includes("채널") ?? false;
      const dataRows = hasHeader ? rows.slice(1) : rows;
      let savedCount = 0;
      let skippedCount = 0;
      const failedRows: string[] = [];

      for (const [rowIndex, row] of dataRows.entries()) {
        const [channel, name, rendererType, itemTypes, statusLabel, , , description] = row;
        if (!(channel ?? "").trim() || !(name ?? "").trim() || !(rendererType ?? "").trim()) {
          skippedCount += 1;
          continue;
        }

        const payload: AdminTemplatePayload = {
          channel_code: channel.trim().toUpperCase(),
          name: name.trim(),
          renderer_type: rendererType.trim(),
          item_types: (itemTypes || "").trim(),
          description: description?.trim() || null,
          status: statusLabel === "미사용" || statusLabel === "inactive" ? "inactive" : "active",
        };

        try {
          const existing = templates.find(
            (item) => item.channel_code === payload.channel_code && item.name === payload.name,
          );
          if (existing) {
            await updateTemplate(token, existing.id, {
              name: payload.name,
              renderer_type: payload.renderer_type,
              item_types: payload.item_types,
              description: payload.description,
              status: payload.status,
            });
          } else {
            await createTemplate(token, payload);
          }
          savedCount += 1;
        } catch (error) {
          const csvRowNumber = rowIndex + (hasHeader ? 2 : 1);
          const reason = error instanceof Error ? error.message : copy.unknownError;
          failedRows.push(`${csvRowNumber} ${payload.channel_code}/${payload.name}: ${reason}`);
        }
      }

      setMenuOpen(false);
      await reload();
      const summary =
        formatAdminTemplateText(copy.uploadSummary, {
          saved: savedCount,
          skipped: skippedCount,
          failed: failedRows.length,
        }) + (failedRows[0] ? ` (${failedRows[0]})` : "");
      setNoticeMessage(summary);
    } catch (error) {
      setNoticeMessage(error instanceof Error ? error.message : copy.uploadFailed);
    }
  }

  const rows: DataGridRow[] = templates.map((template, index) => ({
    key: template.id,
    cells: [
      String(index + 1),
      template.channel_name || template.channel_code,
      <button
        key={template.id}
        type="button"
        className="table-link admin-template-list__name-link"
        onClick={() => openEdit(template)}
      >
        {template.name}
      </button>,
      template.item_count.toString(),
      template.item_types,
      template.renderer_type,
      template.status_label,
      template.updater_name,
      formatDate(template.updated_at,locale),
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
  const rendererOptions = useMemo(() => {
    if (isKakaoTemplateChannel(draft.channel_code)) {
      return RENDERER_OPTIONS.filter((option) =>
        ["simple-text", "quick-reply", "basic-card", "list-card", "carousel"].includes(option.value),
      );
    }
    if (draft.channel_code.trim().toUpperCase() === "TELEGRAM") {
      return RENDERER_OPTIONS.filter((option) => TELEGRAM_RENDERER_TYPES.includes(option.value) && !option.label.startsWith("Kakao"));
    }
    if (draft.channel_code.trim().toUpperCase() === "DISCORD") {
      return RENDERER_OPTIONS.filter((option) => DISCORD_RENDERER_TYPES.includes(option.value) && !option.label.startsWith("Kakao"));
    }
    if (draft.channel_code.trim().toUpperCase() === "SLACK") {
      return RENDERER_OPTIONS.filter((option) => SLACK_RENDERER_TYPES.includes(option.value) && !option.label.startsWith("Kakao"));
    }
    return RENDERER_OPTIONS.filter(
      (option) => !["simple-text", "quick-reply", "basic-card", "list-card"].includes(option.value),
    );
  }, [draft.channel_code]);
  const channelOptions = useMemo(() => {
    const seen = new Map<string, string>();
    templates.forEach((template) => {
      const code = template.channel_code.trim();
      if (code) {
        seen.set(code, template.channel_name?.trim() || code);
      }
    });
    return [...seen.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((left, right) => left.label.localeCompare(right.label, "ko-KR"));
  }, [templates]);
  const totalPages = Math.max(1, Math.ceil(sortedRows.length / pageSize));
  const pagedRows = sortedRows.slice((page - 1) * pageSize, page * pageSize);

  useEffect(() => {
    setDraft((current) => {
      const normalized = normalizeTemplateDraftByChannel(current, rendererDefaults);
      if (
        normalized.renderer_type === current.renderer_type &&
        normalized.item_types === current.item_types &&
        normalized.description === current.description
      ) {
        return current;
      }
      return normalized;
    });
  }, [draft.channel_code, rendererDefaults]);

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

      <div className="admin-page__search-row admin-template-list__search-row">
        <label className="admin-page__search">
          <span aria-hidden="true">⌕</span>
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={copy.searchPlaceholder}
          />
        </label>
        <select
          className="admin-template-list__channel-input login-select"
          value={channelCode}
          onChange={(event) => setChannelCode(event.target.value)}
        >
          <option value="">{copy.allChannels}</option>
          {channelOptions.map((channel) => (
            <option key={channel.value} value={channel.value}>
              {channel.label}
            </option>
          ))}
        </select>
        <select className="login-select" value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="">{copy.allStatus}</option><option value="active">{copy.active}</option><option value="inactive">{copy.inactive}</option>
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
              ⋮
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
                    downloadTemplates(templates);
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
          <strong>{copy.total} {templates.length}</strong>
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
              className="admin-template-list__grid"
              template="80px 150px 220px 120px 1fr 160px 120px 160px 180px"
            columns={[copy.order,copy.channel,copy.name,copy.itemCount,copy.itemType,copy.renderer,copy.status,copy.updatedBy,copy.updatedAt]}
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

      {dialogOpen ? (
        <div className="settings-dialog-backdrop">
          <div className="settings-dialog admin-variable-dialog">
            <div className="settings-dialog__header">
              <strong>{editing ? copy.editTitle : copy.create}</strong>
              <button type="button" className="settings-dialog__close" onClick={closeDialog} aria-label={copy.close}>
                ×
              </button>
            </div>
            <div className="admin-variable-dialog__body">
              <label>
                <span>{copy.channel}</span>
                {editing ? (
                  <input type="text" value={draft.channel_code} disabled />
                ) : (
                  <select
                    className="login-select"
                    value={draft.channel_code}
                    onChange={(event) =>
                      setDraft((current) =>
                        normalizeTemplateDraftByChannel({ ...current, channel_code: event.target.value }, rendererDefaults),
                      )
                    }
                  >
                    {activeChannels.length === 0 ? <option value="">{copy.noChannel}</option> : null}
                    {activeChannels.map((channel) => (
                      <option key={channel.id} value={channel.code}>
                        {channel.name} ({channel.code})
                      </option>
                    ))}
                  </select>
                )}
              </label>
              {isKakaoTemplateChannel(draft.channel_code) ? (
                <div className="admin-form-guide admin-form-guide--wide">
                  <strong>{copy.kakaoGuideTitle}</strong><p>{copy.kakaoGuideTypes}</p><p>{copy.kakaoGuideScope}</p>
                </div>
              ) : null}
              <label>
                <span>{copy.name}</span>
                <input
                  type="text"
                  value={draft.name}
                  onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))}
                />
              </label>
              <label>
                <span>{copy.renderer}</span>
                <select
                  className="login-select"
                  value={draft.renderer_type}
                  onChange={(event) =>
                    setDraft((current) => {
                      const nextRendererType = event.target.value;
                      const defaults = TEMPLATE_RENDERER_DEFAULTS[nextRendererType];
                      return {
                        ...current,
                        renderer_type: nextRendererType,
                        item_types: defaults?.itemTypes ?? current.item_types,
                        description: current.description.trim()
                          ? current.description
                          : rendererDefaults[nextRendererType as keyof AdminTemplateDefaultCatalog] ?? current.description,
                      };
                    })
                  }
                >
                  {rendererOptions.map((option) => (
                    <option key={`${draft.channel_code}:${option.value}:${option.label}`} value={option.value}>
                      {option.value} ({option.label})
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>{copy.itemType}</span>
                <input
                  type="text"
                  value={draft.item_types}
                  onChange={(event) => setDraft((current) => ({ ...current, item_types: event.target.value }))}
                />
              </label>
              <label>
                <span>{copy.description}</span>
                <textarea
                  value={draft.description}
                  onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))}
                />
              </label>
              <label>
                <span>{copy.status}</span>
                <select
                  className="login-select"
                  value={draft.status}
                  onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value as TemplateStatus }))}
                >
                  <option value="active">{copy.active}</option><option value="inactive">{copy.inactive}</option>
                </select>
              </label>
            </div>
            <div className="entity-editor-dialog__footer">
              {editing ? (
                <button type="button" className="secondary-action" onClick={removeTemplate}>
                  {copy.delete}
                </button>
              ) : null}
              <button type="button" className="secondary-action" onClick={closeDialog}>
                {copy.cancel}
              </button>
              <button type="button" className="primary-action" onClick={saveTemplate}>
                {copy.confirm}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
