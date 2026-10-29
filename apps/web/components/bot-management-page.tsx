"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { CSSProperties, useEffect, useMemo, useRef, useState } from "react";

import {
  downloadOperationJson,
  filterOperationBots,
  formatOperationDate,
  operationBotGroupName,
  operationBotLocale,
  operationVersionName,
  readOperationAiDetails,
  readOperationEvaluation,
  readOperationNluEngine,
  rememberOperationVersion,
  resolveInitialOperationBotId,
  safeOperationFilePart,
} from "@/components/bot-operation-shared";
import { BotManagementMetadata } from "@/components/bot-management-metadata";
import { useI18n } from "@/components/language-provider";
import { buildStudioSelectionHref, resolveStudioVersionId } from "@/components/studio-work-context";
import { loadAuthSession, type AuthSession } from "@/lib/auth";
import {
  BOT_MANAGEMENT_CATALOGS,
  botManagementStatusLabel,
  formatBotManagementText,
} from "@/lib/i18n/bot-management";
import {
  activateStudioBotVersion,
  copyStudioBotVersion,
  createStudioBotVersion,
  deactivateStudioBotVersion,
  deleteStudioBotVersion,
  exportStudioBotAidotPackage,
  exportStudioBotVersionPackage,
  fetchStudioBots,
  fetchStudioBotVersions,
  importStudioBotAidotPackage,
  isStudioAidotBotPackage,
  updateStudioBotVersion,
  type StudioBotApiItem,
  type StudioBotVersionApiItem,
} from "@/lib/studio-bots-api";
import { normalizeVersionDocument, type VersionDocument } from "@/lib/version-document";

export function BotManagementPage() {
  const router = useRouter();
  const { language } = useI18n();
  const copy = BOT_MANAGEMENT_CATALOGS[language];
  const uploadRef = useRef<HTMLInputElement>(null);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [bots, setBots] = useState<StudioBotApiItem[]>([]);
  const [hasOwnedBot, setHasOwnedBot] = useState(false);
  const [selectedBotId, setSelectedBotId] = useState("");
  const [versions, setVersions] = useState<StudioBotVersionApiItem[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const selectedBot = useMemo(
    () => bots.find((bot) => bot.id === selectedBotId) || null,
    [bots, selectedBotId],
  );
  const selectedVersion = useMemo(
    () => versions.find((version) => version.id === selectedVersionId) || versions.find((version) => version.is_active) || versions[0] || null,
    [selectedVersionId, versions],
  );
  const activeVersion = useMemo(() => versions.find((version) => version.is_active) || null, [versions]);

  useEffect(() => {
    if (!session || !selectedBot || !selectedVersion) return;
    rememberOperationVersion(session, selectedBot, selectedVersion);
    const nextHref = buildStudioSelectionHref(
      "/studio/bots",
      window.location.search,
      selectedBot.id,
      operationVersionName(selectedVersion),
    );
    if (`${window.location.pathname}${window.location.search}` !== nextHref) {
      router.replace(nextHref, { scroll: false });
    }
  }, [router, selectedBot, selectedVersion, session]);

  useEffect(() => {
    const currentSession = loadAuthSession();
    if (!currentSession) {
      router.replace("/login");
      return;
    }
    setSession(currentSession);
    const requestedBotId = new URLSearchParams(window.location.search).get("bot") || "";
    fetchStudioBots(currentSession.access_token, true)
      .then((items) => {
        setHasOwnedBot(items.length > 0);
        const operationBots = filterOperationBots(currentSession, items);
        setBots(operationBots);
        setSelectedBotId(resolveInitialOperationBotId(currentSession, operationBots, requestedBotId));
      })
      .catch((error) => setMessage(error instanceof Error ? error.message : copy.loadBotsError))
      .finally(() => setLoading(false));
  }, [router]);

  useEffect(() => {
    if (!session || !selectedBotId) {
      setVersions([]);
      return;
    }
    setBusy(true);
    fetchStudioBotVersions(session.access_token, selectedBotId, true)
      .then((items) => {
        const sorted = [...items].sort((left, right) => (right.updated_at || "").localeCompare(left.updated_at || ""));
        const requestedVersion = new URLSearchParams(window.location.search).get("version") || "";
        setVersions(sorted);
        setSelectedVersionId((current) => current && sorted.some((item) => item.id === current)
          ? current
          : resolveStudioVersionId(sorted, requestedVersion));
      })
      .catch((error) => setMessage(error instanceof Error ? error.message : copy.loadVersionsError))
      .finally(() => setBusy(false));
  }, [selectedBotId, session]);

  function chooseBot(bot: StudioBotApiItem) {
    setSelectedBotId(bot.id);
    setSelectedVersionId("");
    router.replace(buildStudioSelectionHref("/studio/bots", window.location.search, bot.id), { scroll: false });
  }

  function chooseVersion(version: StudioBotVersionApiItem) {
    setSelectedVersionId(version.id);
    if (session && selectedBot) rememberOperationVersion(session, selectedBot, version);
  }

  async function refreshVersions(preferredId = "") {
    if (!session || !selectedBot) return;
    const items = await fetchStudioBotVersions(session.access_token, selectedBot.id, true);
    const sorted = [...items].sort((left, right) => (right.updated_at || "").localeCompare(left.updated_at || ""));
    setVersions(sorted);
    setSelectedVersionId(preferredId || (sorted.find((item) => item.is_active) || sorted[0])?.id || "");
  }

  async function runVersionAction(action: "add" | "copy" | "delete" | "activate") {
    if (!session || !selectedBot || (action !== "add" && !selectedVersion)) return;
    if (action === "delete" && !window.confirm(formatBotManagementText(copy.deleteConfirm, { name: selectedVersion?.name || "" }))) return;
    setBusy(true);
    setMessage("");
    try {
      if (action === "add") {
        const nextNo = Math.max(0, ...versions.map((item) => item.version_no)) + 1;
        const created = await createStudioBotVersion(session.access_token, selectedBot.id, {
          name: `v${nextNo}`,
          description: "",
          comment: copy.emptyVersionComment,
        });
        await refreshVersions(created.id);
        setMessage(formatBotManagementText(copy.versionAdded, { name: created.name }));
      } else if (action === "copy" && selectedVersion) {
        const copied = await copyStudioBotVersion(session.access_token, selectedBot.id, selectedVersion.id);
        await refreshVersions(copied.id);
        setMessage(formatBotManagementText(copy.versionCopied, { name: selectedVersion.name }));
      } else if (action === "delete" && selectedVersion) {
        await deleteStudioBotVersion(session.access_token, selectedBot.id, selectedVersion.id);
        await refreshVersions();
        setMessage(formatBotManagementText(copy.versionDeleted, { name: selectedVersion.name }));
      } else if (action === "activate" && selectedVersion) {
        const changed = selectedVersion.is_active
          ? await deactivateStudioBotVersion(session.access_token, selectedBot.id, selectedVersion.id)
          : await activateStudioBotVersion(session.access_token, selectedBot.id, selectedVersion.id);
        await refreshVersions(changed.id);
        setMessage(selectedVersion.is_active ? copy.activeVersionUnset : copy.activeVersionSet);
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : copy.versionActionError);
    } finally {
      setBusy(false);
    }
  }

  async function handleDownload(aidot: boolean) {
    if (!session || !selectedBot || !selectedVersion) return;
    setBusy(true);
    setMessage("");
    try {
      const data = aidot
        ? await exportStudioBotAidotPackage(session.access_token, selectedBot.id, selectedVersion.id)
        : await exportStudioBotVersionPackage(session.access_token, selectedBot.id, selectedVersion.id);
      const prefix = aidot ? "Bot" : safeOperationFilePart(selectedBot.name);
      downloadOperationJson(
        `${prefix}_${safeOperationFilePart(selectedBot.name)}_${safeOperationFilePart(selectedVersion.name)}.json`,
        data,
      );
      setMessage(aidot ? copy.aidotDownloaded : copy.versionDownloaded);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : copy.downloadError);
    } finally {
      setBusy(false);
    }
  }

  async function handleUpload(file: File | null) {
    if (!file || !session || !selectedBot || !selectedVersion) return;
    setBusy(true);
    setMessage("");
    try {
      const parsed = JSON.parse(await file.text()) as unknown;
      if (isStudioAidotBotPackage(parsed)) {
        const result = await importStudioBotAidotPackage(session.access_token, selectedBot.id, selectedVersion.id, parsed);
        await refreshVersions(result.version.id);
        setMessage(formatBotManagementText(copy.aidotUploaded, { name: file.name }));
      } else {
        const source = parsed && typeof parsed === "object" && !Array.isArray(parsed) && "version_json" in parsed
          ? (parsed as { version_json?: Partial<VersionDocument> }).version_json
          : parsed;
        if (!source || typeof source !== "object" || Array.isArray(source)) {
          throw new Error(copy.invalidVersionFile);
        }
        const changed = await updateStudioBotVersion(session.access_token, selectedBot.id, selectedVersion.id, {
          version_json: normalizeVersionDocument(source as Partial<VersionDocument>),
        });
        await refreshVersions(changed.id);
        setMessage(formatBotManagementText(copy.versionUploaded, { name: file.name }));
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : copy.uploadError);
    } finally {
      if (uploadRef.current) uploadRef.current.value = "";
      setBusy(false);
    }
  }

  const counts = selectedVersion?.asset_counts;
  const aiDetails = selectedBot && selectedVersion
    ? readOperationAiDetails(selectedBot, selectedVersion)
    : { nluType: "-", nluModel: "-", answerMode: "-", llmModel: "-" };
  const columns = {
    "--operation-cols": ".55fr .65fr .75fr 1.35fr .45fr .45fr .45fr .45fr .55fr .95fr .85fr 1fr",
  } as CSSProperties;

  return (
    <section className="cga-operation-page">
      <header className="cga-operation-header">
        <div className="cga-operation-heading">
          <span className="cga-operation-code">BM</span>
          <div><strong>{copy.title}</strong><span>{copy.description}</span></div>
        </div>
        {!loading && !hasOwnedBot ? <Link href="/studio/bots/new" className="cga-operation-primary-action">{copy.createBot}</Link> : null}
      </header>

      <div className="cga-operation-summary cga-operation-summary--management">
        <article><strong>{copy.group}</strong><span>{operationBotGroupName(session, selectedBot)}</span></article>
        <article><strong>{copy.botCount}</strong><span>{bots.length}{copy.countUnit}</span></article>
        <article><strong>{copy.selectedBot}</strong><span>{selectedBot?.name || "-"}</span></article>
        <article><strong>{copy.versionItems}</strong><span>{versions.length}{copy.countUnit}</span></article>
        <article><strong>{copy.workVersion}</strong><span>{selectedVersion ? operationVersionName(selectedVersion) : "-"}</span></article>
        <article><strong>{copy.activeVersion}</strong><span>{activeVersion ? operationVersionName(activeVersion) : "-"}</span></article>
      </div>

      <div className="cga-bot-management-grid">
        <article className="cga-operation-panel cga-operation-panel--bot-list">
          <header><div><strong>{copy.botList}</strong><span>{copy.botListDescription}</span></div></header>
          <div className="cga-bot-card-list">
            {bots.map((bot) => (
              <button key={bot.id} type="button" className={`cga-bot-select-card${bot.id === selectedBotId ? " is-selected" : ""}`} onClick={() => chooseBot(bot)}>
                <strong>{bot.name}</strong>
                <BotManagementMetadata
                  bot={bot}
                  statusLabel={botManagementStatusLabel(bot.status, copy)}
                  localeLabel={copy.locale}
                  locale={operationBotLocale(bot)}
                />
              </button>
            ))}
            {!loading && bots.length === 0 ? <div className="cga-operation-empty">{copy.noBots}</div> : null}
          </div>
        </article>

        <article className="cga-operation-panel cga-operation-panel--versions">
          <header>
            <div><strong>{copy.assetsAndVersions}</strong><span>{copy.assetsAndVersionsDescription}</span></div>
            <div className="cga-operation-inline-actions">
              <button type="button" onClick={() => runVersionAction("add")} disabled={busy || !selectedBot}>{copy.addVersion}</button>
              <button type="button" onClick={() => runVersionAction("copy")} disabled={busy || !selectedVersion}>{copy.copy}</button>
              <button type="button" onClick={() => runVersionAction("delete")} disabled={busy || !selectedVersion || selectedVersion.is_active}>{copy.delete}</button>
              <button type="button" onClick={() => uploadRef.current?.click()} disabled={busy || !selectedVersion}>{copy.uploadVersion}</button>
              <button type="button" onClick={() => handleDownload(false)} disabled={busy || !selectedVersion}>{copy.downloadVersion}</button>
              <button type="button" onClick={() => runVersionAction("activate")} disabled={busy || !selectedVersion}>{copy.toggleActive}</button>
            </div>
          </header>
          <div className="cga-operation-table cga-operation-table--versions" style={columns}>
            <div className="cga-operation-row cga-operation-row--head">
              <span>{copy.select}</span><span>{copy.version}</span><span>{copy.status}</span><span>{copy.lastTrainingEngine}</span><span>{copy.intents}</span><span>{copy.entities}</span><span>{copy.dictionary}</span><span>{copy.api}</span><span>{copy.evaluation}</span><span>{copy.modifiedAt}</span><span>{copy.modifiedBy}</span><span>{copy.note}</span>
            </div>
            {versions.map((version) => (
              <button key={version.id} type="button" className={`cga-operation-row cga-operation-row--button${version.id === selectedVersion?.id ? " is-selected" : ""}`} onClick={() => chooseVersion(version)}>
                <span><input type="radio" readOnly checked={version.id === selectedVersion?.id} aria-label={`${version.name} ${copy.select}`} /></span>
                <strong>{operationVersionName(version)}</strong>
                <span>{botManagementStatusLabel(version.status, copy)}</span>
                <span>{selectedBot ? readOperationNluEngine(selectedBot, version) : "-"}</span>
                <span>{version.asset_counts?.intents ?? 0}</span>
                <span>{version.asset_counts?.entities ?? 0}</span>
                <span>{version.asset_counts?.dictionary ?? 0}</span>
                <span>{version.asset_counts?.apis ?? 0}</span>
                <span>{readOperationEvaluation(version)}</span>
                <span>{formatOperationDate(version.updated_at)}</span>
                <span>{version.updated_by_login_id || "-"}</span>
                <span>{version.comment || (version.is_active ? copy.activeVersionNote : version.id === selectedVersion?.id ? copy.workVersionNote : "-")}</span>
              </button>
            ))}
            {!busy && versions.length === 0 ? <div className="cga-operation-empty">{copy.noVersionHistory}</div> : null}
          </div>
        </article>

        <article className="cga-operation-panel cga-operation-panel--bot-detail">
          <header><div><strong>{copy.detailTitle}</strong><span>{copy.detailDescription}</span></div></header>
          <dl className="cga-operation-definition">
            <div><dt>{copy.botId}</dt><dd>{selectedBot?.id || "-"}</dd></div>
            <div><dt>{copy.botName}</dt><dd>{selectedBot?.name || "-"}</dd></div>
            <div><dt>{copy.workVersion}</dt><dd>{selectedVersion ? operationVersionName(selectedVersion) : "-"}</dd></div>
            <div><dt>{copy.activeVersion}</dt><dd>{activeVersion ? operationVersionName(activeVersion) : "-"}</dd></div>
            <div><dt>{copy.status}</dt><dd>{selectedBot ? botManagementStatusLabel(selectedBot.status, copy) : "-"}</dd></div>
            <div><dt>{copy.language}</dt><dd>{operationBotLocale(selectedBot)}</dd></div>
            <div><dt>{copy.nluType}</dt><dd>{aiDetails.nluType}</dd></div>
            <div><dt>{copy.nluModel}</dt><dd>{aiDetails.nluModel}</dd></div>
            <div><dt>{copy.answerMode}</dt><dd>{aiDetails.answerMode}</dd></div>
            <div><dt>LLM</dt><dd>{aiDetails.llmModel}</dd></div>
            <div><dt>{copy.versionList}</dt><dd>{versions.length}{copy.countUnit}</dd></div>
            <div><dt>{copy.updated}</dt><dd>{formatOperationDate(selectedBot?.updated_at)}</dd></div>
          </dl>
          <div className="cga-operation-action-stack">
            <button type="button" onClick={() => handleDownload(true)} disabled={busy || !selectedVersion}><strong>{copy.aidotPackageDownload}</strong><span>{copy.currentWorkVersionOne}</span></button>
            <button type="button" onClick={() => uploadRef.current?.click()} disabled={busy || !selectedVersion}><strong>{copy.aidotPackageUpload}</strong><span>{copy.applyToCurrentWorkVersion}</span></button>
          </div>
          <div className={`cga-operation-note${message ? " is-active" : ""}`}>
            {message || (loading || busy
              ? copy.loading
              : formatBotManagementText(copy.assetSummary, {
                intents: counts?.intents ?? 0,
                entities: counts?.entities ?? 0,
                dictionary: counts?.dictionary ?? 0,
                apis: counts?.apis ?? 0,
              }))}
          </div>
          <input ref={uploadRef} type="file" accept="application/json,.json" hidden onChange={(event) => handleUpload(event.target.files?.[0] || null)} />
        </article>
      </div>
    </section>
  );
}
