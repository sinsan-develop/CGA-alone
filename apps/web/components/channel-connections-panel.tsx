"use client";
import { useEffect, useState } from "react";
import { useI18n } from "@/components/language-provider";
import { CONNECTION_COPY } from "@/lib/i18n/channel-connections";
import { actOnChannelConnection, fetchChannelConnections, fetchConnectionBots, saveChannelConnection, type ChannelConnection, type ConnectionDraft, type ConnectionPayload } from "@/lib/channel-connections-api";

const credentialFields: Record<string, string[]> = {
  slack: ["bot_token", "app_token"], discord: ["bot_token"], telegram: ["bot_token"],
  ms_teams: ["client_id", "client_secret", "tenant_id"], google_chat: ["service_account_json", "project_id"],
  line: ["access_token", "channel_secret"], kakao: ["bot_token"], webchat: ["access_token"],
};
const emptyDraft: ConnectionDraft = {name: "", bot_id: "", installation_id: "", account_id: "", target_id: ""};

export function buildConnectionPayload(draft: ConnectionDraft, credentials: Record<string, string>): ConnectionPayload {
  const values = Object.fromEntries(Object.entries(credentials).filter(([, value]) => value.trim()));
  return {...draft, name: draft.name.trim(), credentials: Object.keys(values).length ? values : undefined};
}

export function connectionErrorMessage(error: unknown, copy: Record<string, string>): string {
  const code = error instanceof Error ? error.message.split(":", 1)[0] : "";
  const messages: Record<string, string> = {
    CONNECTION_KEY_UNAVAILABLE: copy.keyUnavailable,
    CONNECTION_CREDENTIALS_UNAVAILABLE: copy.keyUnavailable,
    DUPLICATE_CONNECTION: copy.duplicate,
    DISCONNECT_FIRST: copy.disconnectFirst,
    INVALID_CREDENTIALS: copy.invalidCredentials,
  };
  return Object.hasOwn(messages, code) ? messages[code] : copy.error;
}

export function ConnectionDetails({ item, bots, copy, language }: { item: ChannelConnection; bots: {id: string; name: string}[]; copy: Record<string, string>; language: string }) {
  const botName = bots.find(bot => bot.id === item.bot_id)?.name ?? item.bot_id;
  const checked = item.last_checked_at ? new Intl.DateTimeFormat(language, {dateStyle: "medium", timeStyle: "short"}).format(new Date(item.last_checked_at)) : copy.notChecked;
  return <p>{copy.bot}: {botName} · {copy.lastChecked}: {checked}</p>;
}

export function ChannelConnectionsPanel({ token, channel, onClose }: { token: string; channel: {id: string; name: string; data_json: Record<string, unknown>}; onClose: () => void }) {
  const {language} = useI18n(); const copy = CONNECTION_COPY[language];
  const [items, setItems] = useState<ChannelConnection[]>([]);
  const [bots, setBots] = useState<{id: string; name: string}[]>([]);
  const [draft, setDraft] = useState<ConnectionDraft>(emptyDraft);
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string | undefined>();
  const [formOpen, setFormOpen] = useState(false);
  const [busy, setBusy] = useState(false); const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState(""); const [error, setError] = useState("");
  const provider = String(channel.data_json.provider ?? "").replaceAll("-", "_");
  useEffect(() => {
    let active = true;
    setLoading(true);
    Promise.all([fetchChannelConnections(token, channel.id), fetchConnectionBots(token, channel.id)])
      .then(([connections, choices]) => { if (active) { setItems(connections.items); setBots(choices.items); } })
      .catch(() => { if (active) setError(copy.error); })
      .finally(() => { if (active) setLoading(false); });
    return () => {active = false;};
  }, [token, channel.id, copy.error]);

  function resetForm() {setFormOpen(false);setEditing(undefined);setDraft(emptyDraft);setCredentials({});}
  async function reload() {setItems((await fetchChannelConnections(token, channel.id)).items);}
  async function handleSave(event: React.FormEvent) {
    event.preventDefault(); if (busy) return;
    setBusy(true);setError("");setNotice("");
    try {
      await saveChannelConnection(token, channel.id, buildConnectionPayload(draft, credentials), editing);
      resetForm();setNotice(copy.saved);await reload();
    } catch (error) {setError(connectionErrorMessage(error, copy));} finally {setBusy(false);}
  }
  async function action(item: ChannelConnection, name: "check" | "disconnect" | "delete") {
    if (busy || (name === "delete" && !window.confirm(copy.confirmDelete))) return;
    setBusy(true);setError("");setNotice("");
    try {
      const result = await actOnChannelConnection(token, channel.id, item.id, name);
      setNotice(name === "check" ? (result.configuration_valid ? copy.configOk : copy.configBad) : name === "delete" ? copy.deleted : copy.disconnected);
      if (name === "delete" && editing === item.id) resetForm();
      await reload();
    } catch (error) {setError(connectionErrorMessage(error, copy));} finally {setBusy(false);}
  }
  function edit(item: ChannelConnection) {
    setDraft({name:item.name,bot_id:item.bot_id,installation_id:item.installation_id,account_id:item.account_id,target_id:item.target_id});
    setEditing(item.id);setCredentials({});setFormOpen(true);setNotice("");setError("");
  }
  return <section className="admin-page" aria-label={copy.title} aria-busy={busy || loading}>
    <div className="admin-page__table-actions"><h2>{channel.name} · {copy.title}</h2><button type="button" className="admin-page__ghost" disabled={busy} onClick={onClose}>{copy.close}</button></div>
    <p>{copy.pending}</p>
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    <button type="button" className="admin-page__ghost" disabled={busy || loading} onClick={() => {resetForm();setFormOpen(true);}}>{copy.add}</button>
    {!loading && !items.length ? <p>{copy.empty}</p> : null}
    <ul>{items.map(item => <li key={item.id}>
      <strong>{item.name}</strong> · {item.installation_id} / {item.account_id} / {item.target_id}
      <ConnectionDetails item={item} bots={bots} copy={copy} language={language} />
      <p>{copy[item.observed_state === "error" ? "stateError" : item.observed_state]} · {copy.credentials}: {item.credentials_registered ? copy.registered : copy.missing}</p>
      {item.last_error_code ? <p>{item.last_error_code}</p> : null}
      <div className="admin-page__table-actions">
        <button type="button" className="admin-page__ghost" disabled={busy} onClick={() => action(item,"check")}>{copy.check}</button>
        <button type="button" className="admin-page__ghost" disabled title={copy.pending}>{copy.connect}</button>
        <button type="button" className="admin-page__ghost" disabled={busy || item.desired_state === "disconnected" && item.observed_state === "disconnected"} onClick={() => action(item,"disconnect")}>{copy.disconnect}</button>
        <button type="button" className="admin-page__ghost" disabled={busy || item.desired_state !== "disconnected" || !["disconnected","error"].includes(item.observed_state)} onClick={() => edit(item)}>{copy.edit}</button>
        <button type="button" className="admin-page__ghost" disabled={busy || item.desired_state !== "disconnected" || !["disconnected","error"].includes(item.observed_state)} onClick={() => action(item,"delete")}>{copy.remove}</button>
      </div>
    </li>)}</ul>
    {formOpen ? <form className="settings-dialog__body" onSubmit={handleSave}>
      <label>{copy.name}<input required maxLength={120} value={draft.name} disabled={busy} onChange={e => setDraft({...draft,name:e.target.value})}/></label>
      <label>{copy.bot}<select required value={draft.bot_id} disabled={busy} onChange={e => setDraft({...draft,bot_id:e.target.value})}><option value="">{copy.selectBot}</option>{bots.map(bot => <option key={bot.id} value={bot.id}>{bot.name}</option>)}</select></label>
      {([['installation_id','installation'],['account_id','account'],['target_id','target']] as const).map(([field,label]) => <label key={field}>{copy[label]}<input required maxLength={200} value={draft[field]} disabled={busy} onChange={e => setDraft({...draft,[field]:e.target.value})}/></label>)}
      <fieldset disabled={busy}><legend>{copy.credentials}</legend><p>{copy.preserve}</p>
        {(credentialFields[provider] ?? []).map(field => <label key={field}>{field}<input type="password" autoComplete="new-password" maxLength={24000} value={credentials[field] ?? ""} onChange={e => setCredentials({...credentials,[field]:e.target.value})}/></label>)}
      </fieldset>
      <div className="admin-page__table-actions"><button type="button" disabled={busy} onClick={resetForm}>{copy.cancel}</button><button type="submit" disabled={busy}>{copy.save}</button></div>
    </form> : null}
  </section>;
}
