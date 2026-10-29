"use client";

import { getStudioRuntimeMessage } from "@/lib/i18n/studio-runtime-native";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { loadAuthSession } from "@/lib/auth";
import { resolveApiAssetPublicUrl } from "@/lib/api";
import { SimulatorPage } from "@/components/simulator-page";
import { useI18n } from "@/components/language-provider";
import { getStudioPageLabel, STUDIO_PAGE_CATALOGS } from "@/lib/i18n/studio-pages";
import { activateStudioBotVersion, fetchStudioHubCandidates, fetchStudioBotVersions, fetchStudioHub, trainStudioBotVersionNlu, type StudioHubCandidate, type StudioHub, updateStudioHubMembers } from "@/lib/studio-bots-api";

type Props = { hubId: string };
type DraftMember = { bot_id: string; display_name: string; use_as_small_talk: boolean };

const toDraftMembers = (hub: StudioHub): DraftMember[] => hub.members.map((member) => ({ bot_id: member.bot_id, display_name: member.display_name || member.name || "", use_as_small_talk: member.use_as_small_talk }));

export function BotHubComposition({ hubId }: Props) {
  const { language: uiLanguage } = useI18n();
  const copy = STUDIO_PAGE_CATALOGS[uiLanguage];
  const [hub, setHub] = useState<StudioHub | null>(null);
  const [bots, setBots] = useState<StudioHubCandidate[]>([]);
  const [members, setMembers] = useState<DraftMember[]>([]);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [training, setTraining] = useState(false);
  const [simulatorOpen, setSimulatorOpen] = useState(false);

  useEffect(() => {
    const session = loadAuthSession();
    if (!session) { setMessage(getStudioRuntimeMessage(uiLanguage, "로그인이 필요합니다.")); setLoading(false); return; }
    let disposed = false;
    Promise.all([fetchStudioHub(session.access_token, hubId), fetchStudioHubCandidates(session.access_token, hubId)])
      .then(([loadedHub, loadedBots]) => { if (!disposed) { setHub(loadedHub); setBots(loadedBots); setMembers(toDraftMembers(loadedHub)); } })
      .catch((error) => { if (!disposed) setMessage(error instanceof Error ? error.message : getStudioRuntimeMessage(uiLanguage, "봇 허브를 불러오지 못했습니다.")); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [hubId]);

  const candidates = useMemo(() => bots, [bots, hubId]);
  const botById = useMemo(() => new Map(candidates.map((bot) => [bot.id, bot])), [candidates]);

  function toggle(bot: StudioHubCandidate) {
    setMembers((current) => current.some((member) => member.bot_id === bot.id)
      ? current.filter((member) => member.bot_id !== bot.id)
      : [...current, { bot_id: bot.id, display_name: bot.name, use_as_small_talk: false }]);
  }
  function update(botId: string, patch: Partial<DraftMember>) { setMembers((current) => current.map((member) => member.bot_id === botId ? { ...member, ...patch } : member)); }
  function move(botId: string, offset: -1 | 1) {
    setMembers((current) => {
      const index = current.findIndex((member) => member.bot_id === botId); const nextIndex = index + offset;
      if (index < 0 || nextIndex < 0 || nextIndex >= current.length) return current;
      const next = [...current]; [next[index], next[nextIndex]] = [next[nextIndex], next[index]]; return next;
    });
  }
  async function save() {
    const session = loadAuthSession(); if (!session) { setMessage(getStudioRuntimeMessage(uiLanguage, "로그인이 필요합니다.")); return; }
    setSaving(true); setMessage("");
    try {
      const saved = await updateStudioHubMembers(session.access_token, hubId, { members: members.map((member) => ({ ...member, display_name: member.display_name.trim() || undefined, use_as_small_talk: hub?.call_method === "natural" && member.use_as_small_talk })) });
      setHub(saved); setMembers(toDraftMembers(saved)); setMessage(getStudioRuntimeMessage(uiLanguage, "봇 허브 구성을 저장했습니다."));
    } catch (error) { setMessage(error instanceof Error ? error.message : getStudioRuntimeMessage(uiLanguage, "봇 허브 구성 저장에 실패했습니다.")); }
    finally { setSaving(false); }
  }

  async function trainNaturalHub() {
    const session = loadAuthSession();
    if (!session || !hub) { setMessage(getStudioRuntimeMessage(uiLanguage, "로그인이 필요합니다.")); return; }
    if (hub.call_method !== "natural") { setMessage(getStudioRuntimeMessage(uiLanguage, "자연어 입력형 봇 허브에서만 학습할 수 있습니다.")); return; }
    const savedMemberIds = hub.members.map((member) => member.bot_id).join("|");
    const draftMemberIds = members.map((member) => member.bot_id).join("|");
    if (savedMemberIds !== draftMemberIds) { setMessage(getStudioRuntimeMessage(uiLanguage, "하위 봇 구성을 먼저 저장한 후 학습해주세요.")); return; }
    const readyMemberCount = hub.members.filter((member) => botById.get(member.bot_id)?.is_trained).length;
    if (readyMemberCount < 2) { setMessage(getStudioRuntimeMessage(uiLanguage, "자연어 입력형 봇 허브는 학습 완료된 운영 버전의 하위 봇이 2개 이상 필요합니다.")); return; }
    setTraining(true); setMessage("");
    try {
      const versions = await fetchStudioBotVersions(session.access_token, hubId);
      const version = versions.find((item) => item.is_active) || versions.at(-1);
      if (!version) throw new Error(getStudioRuntimeMessage(uiLanguage, "학습할 봇 허브 버전이 없습니다."));
      await trainStudioBotVersionNlu(session.access_token, hubId, version.id);
      await activateStudioBotVersion(session.access_token, hubId, version.id);
      const refreshed = await fetchStudioHub(session.access_token, hubId);
      setHub(refreshed); setMembers(toDraftMembers(refreshed));
      setMessage(getStudioRuntimeMessage(uiLanguage, "봇 허브 학습과 운영 버전 적용이 완료되었습니다."));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : getStudioRuntimeMessage(uiLanguage, "봇 허브 학습에 실패했습니다."));
    } finally {
      setTraining(false);
    }
  }

  if (loading) return <main className="page"><p className="bot-hub__notice">{getStudioPageLabel(copy,"봇 허브를 불러오는 중입니다...")}</p></main>;
  if (!hub) return <main className="page"><p className="bot-hub__notice bot-hub__notice--error">{message || getStudioRuntimeMessage(uiLanguage, "봇 허브를 찾을 수 없습니다.")}</p></main>;

  return <main className="page bot-hub">
    <header className="bot-hub__header">
      <div className="bot-hub__identity">
        {hub.profile_image_url ? <img className="bot-hub__profile-image" src={resolveApiAssetPublicUrl(hub.profile_image_url)} alt="" /> : <span className={`bot-hub__profile bot-hub__profile--${hub.profile_key}`} aria-hidden="true" />}
        <div>
          <Link href={`/studio/hubs/${hubId}`} className="bot-hub__back">{getStudioPageLabel(copy,"봇 허브 화면")}</Link>
          <h1>{getStudioPageLabel(copy,"봇 허브 구성")}</h1>
          <p>{hub.name}에 포함할 하위 봇을 선택하고 표시 순서를 관리합니다.</p>
        </div>
      </div>
      <div className="bot-hub__header-actions">
        <button type="button" className="bot-hub__primary" onClick={save} disabled={saving}>{saving ? getStudioRuntimeMessage(uiLanguage, "저장 중...") : getStudioRuntimeMessage(uiLanguage, "구성 저장")}</button>
      </div>
    </header>
    {message ? <p className={`bot-hub__notice${message === getStudioRuntimeMessage(uiLanguage, "봇 허브 구성을 저장했습니다.") || message === getStudioRuntimeMessage(uiLanguage, "봇 허브 학습과 운영 버전 적용이 완료되었습니다.") ? "" : " bot-hub__notice--error"}`}>{message}</p> : null}
    <section className="bot-hub__grid">
      <article className="bot-hub__panel"><div className="bot-hub__panel-title"><h2>{getStudioPageLabel(copy,"하위 봇 선택")}</h2><span>{getStudioRuntimeMessage(uiLanguage, "{count}개").replace("{count}", String(candidates.length))}</span></div><p className="bot-hub__help">{hub.call_method === "natural" ? getStudioRuntimeMessage(uiLanguage, "학습 완료된 운영 버전의 일반 봇을 2개 이상 선택해야 합니다.") : getStudioRuntimeMessage(uiLanguage, "일반 봇을 선택하면 허브의 호출 버튼으로 사용됩니다.")}</p><div className="bot-hub__candidate-list">{candidates.map((bot) => <label key={bot.id} className="bot-hub__candidate"><input type="checkbox" checked={members.some((member) => member.bot_id === bot.id)} onChange={() => toggle(bot)} /><span><strong>{bot.name}</strong><small>{bot.active_version_id ? (bot.is_trained ? getStudioRuntimeMessage(uiLanguage, "운영 버전 · 학습 완료") : getStudioRuntimeMessage(uiLanguage, "운영 버전 · 학습 필요")) : getStudioRuntimeMessage(uiLanguage, "운영 버전 없음")}</small></span></label>)}{candidates.length === 0 ? <p className="bot-hub__help">{getStudioPageLabel(copy,"추가할 일반 봇이 없습니다.")}</p> : null}</div></article>
      <article className="bot-hub__panel"><div className="bot-hub__panel-title"><h2>{getStudioPageLabel(copy,"허브 구성")}</h2><span>{getStudioRuntimeMessage(uiLanguage, "{count}개 선택").replace("{count}", String(members.length))}</span></div><p className="bot-hub__help">{hub.call_method === "natural" ? getStudioRuntimeMessage(uiLanguage, "표시명은 후보 안내에, 순서는 동점 후보 정렬에 사용됩니다.") : getStudioRuntimeMessage(uiLanguage, "표시 순서는 호출 버튼의 표시 순서입니다.")}</p><ol className="bot-hub__member-list">{members.map((member, index) => { const bot = botById.get(member.bot_id); return <li key={member.bot_id}><div className="bot-hub__member-main"><strong>{bot?.name || getStudioRuntimeMessage(uiLanguage, "삭제된 봇")}</strong><input aria-label={getStudioPageLabel(copy,"표시명")} value={member.display_name} onChange={(event) => update(member.bot_id, { display_name: event.target.value })} />{hub.call_method === "natural" ? <label className="bot-hub__check"><input type="checkbox" checked={member.use_as_small_talk} onChange={(event) => update(member.bot_id, { use_as_small_talk: event.target.checked })} />{getStudioPageLabel(copy, "봇 허브 스몰토크 사용")}</label> : null}</div><div className="bot-hub__member-actions"><button type="button" onClick={() => move(member.bot_id, -1)} disabled={index === 0}>↑</button><button type="button" onClick={() => move(member.bot_id, 1)} disabled={index === members.length - 1}>↓</button><button type="button" onClick={() => setMembers((current) => current.filter((item) => item.bot_id !== member.bot_id))}>×</button></div></li>; })}</ol>{members.length === 0 ? <p className="bot-hub__empty">{getStudioPageLabel(copy,"왼쪽 목록에서 하위 봇을 선택하세요.")}</p> : null}</article>
    </section>
  </main>;
}
