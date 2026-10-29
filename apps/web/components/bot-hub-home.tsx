"use client";

import { getStudioRuntimeMessage } from "@/lib/i18n/studio-runtime-native";

import Link from "next/link";
import { useEffect, useState } from "react";

import { SimulatorPage } from "@/components/simulator-page";
import { loadAuthSession } from "@/lib/auth";
import { resolveApiAssetPublicUrl } from "@/lib/api";
import { useI18n } from "@/components/language-provider";
import { getStudioPageLabel, STUDIO_PAGE_CATALOGS } from "@/lib/i18n/studio-pages";
import {
  activateStudioBotVersion,
  fetchStudioBots,
  fetchStudioHubCandidates,
  fetchStudioBotVersions,
  fetchStudioHub,
  trainStudioBotVersionNlu,
  type StudioBotApiItem,
  type StudioHubCandidate,
  type StudioHub,
} from "@/lib/studio-bots-api";

type Props = { hubId: string };

function HubProfile({ hub }: { hub: StudioHub }) {
  if (hub.profile_image_url) {
    return <img className="bot-hub__profile-image" src={resolveApiAssetPublicUrl(hub.profile_image_url)} alt="" />;
  }
  return <span className={`bot-hub__profile bot-hub__profile--${hub.profile_key}`} aria-hidden="true" />;
}

export function BotHubHome({ hubId }: Props) {
  const { language: uiLanguage } = useI18n();
  const copy = STUDIO_PAGE_CATALOGS[uiLanguage];
  const [hub, setHub] = useState<StudioHub | null>(null);
  const [botsById, setBotsById] = useState<Record<string, StudioBotApiItem>>({});
  const [candidatesById, setCandidatesById] = useState<Record<string, StudioHubCandidate>>({});
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [training, setTraining] = useState(false);
  const [simulatorOpen, setSimulatorOpen] = useState(false);

  useEffect(() => {
    const session = loadAuthSession();
    if (!session) {
      setMessage(getStudioRuntimeMessage(uiLanguage, "로그인이 필요합니다."));
      setLoading(false);
      return;
    }

    let disposed = false;
    fetchStudioHub(session.access_token, hubId)
      .then((loadedHub) => {
        if (disposed) return;
        setHub(loadedHub);

        void Promise.all([fetchStudioBots(session.access_token), fetchStudioHubCandidates(session.access_token, hubId)])
          .then(([bots, candidates]) => {
            if (disposed) return;
            setBotsById(Object.fromEntries(bots.map((bot) => [bot.id, bot])));
            setCandidatesById(Object.fromEntries(candidates.map((candidate) => [candidate.id, candidate])));
          })
          .catch(() => {
            // 구성 봇의 상세 표시는 보조 정보이며, 허브 화면 자체는 계속 표시한다.
          });
      })
      .catch((error) => {
        if (!disposed) setMessage(error instanceof Error ? error.message : getStudioRuntimeMessage(uiLanguage, "봇 허브를 불러오지 못했습니다."));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });

    return () => {
      disposed = true;
    };
  }, [hubId]);

  async function trainNaturalHub() {
    const session = loadAuthSession();
    if (!session || !hub) {
      setMessage(getStudioRuntimeMessage(uiLanguage, "로그인이 필요합니다."));
      return;
    }

    const readyMemberCount = hub.members.filter((member) => candidatesById[member.bot_id]?.is_trained).length;
    if (readyMemberCount < 2) {
      setMessage(getStudioRuntimeMessage(uiLanguage, "자연어 입력형 봇 허브는 학습 완료된 운영 버전의 하위 봇이 2개 이상 필요합니다."));
      return;
    }

    setTraining(true);
    setMessage("");
    try {
      const versions = await fetchStudioBotVersions(session.access_token, hubId);
      const version = versions.find((item) => item.is_active) || versions.at(-1);
      if (!version) throw new Error(getStudioRuntimeMessage(uiLanguage, "학습할 봇 허브 버전이 없습니다."));

      await trainStudioBotVersionNlu(session.access_token, hubId, version.id);
      await activateStudioBotVersion(session.access_token, hubId, version.id);
      setHub(await fetchStudioHub(session.access_token, hubId));
      setMessage(getStudioRuntimeMessage(uiLanguage, "봇 허브 학습과 운영 버전 적용이 완료되었습니다."));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : getStudioRuntimeMessage(uiLanguage, "봇 허브 학습에 실패했습니다."));
    } finally {
      setTraining(false);
    }
  }

  if (loading) {
    return <main className="page"><p className="bot-hub__notice">{getStudioPageLabel(copy,"봇 허브를 불러오는 중입니다...")}</p></main>;
  }
  if (!hub) {
    return <main className="page"><p className="bot-hub__notice bot-hub__notice--error">{message || getStudioRuntimeMessage(uiLanguage, "봇 허브를 찾을 수 없습니다.")}</p></main>;
  }

  return (
    <main className="page bot-hub">
      <header className="bot-hub__header">
        <div className="bot-hub__identity">
          <HubProfile hub={hub} />
          <div>
            <Link href="/studio/hubs" className="bot-hub__back">{getStudioPageLabel(copy,"봇 허브 목록")}</Link>
            <h1>{hub.name}</h1>
            <p>{hub.introduction || getStudioRuntimeMessage(uiLanguage, "{type} 봇 허브").replace("{type}", hub.call_method === "natural" ? getStudioRuntimeMessage(uiLanguage, "자연어 입력형") : getStudioRuntimeMessage(uiLanguage, "버튼 선택형"))}</p>
          </div>
        </div>
        <div className="bot-hub__header-actions">
          {hub.call_method === "natural" ? (
            <button type="button" className="bot-hub__secondary" onClick={trainNaturalHub} disabled={training}>
              {training ? getStudioPageLabel(copy, "학습 중...") : getStudioPageLabel(copy, "봇 허브 학습")}
            </button>
          ) : null}
          <Link href={`/studio/hubs/${hubId}/retraining`} className="bot-hub__secondary">{getStudioPageLabel(copy,"재학습")}</Link>
          <Link href={`/studio/hubs/${hubId}/settings`} className="bot-hub__secondary">{getStudioPageLabel(copy,"설정")}</Link>
          <Link href={`/studio/hubs/${hubId}/composition`} className="bot-hub__primary">{getStudioPageLabel(copy,"봇 허브 구성")}</Link>
        </div>
      </header>

      {message ? <p className={`bot-hub__notice${message === getStudioRuntimeMessage(uiLanguage, "봇 허브 학습과 운영 버전 적용이 완료되었습니다.") ? "" : " bot-hub__notice--error"}`}>{message}</p> : null}

      <section className="bot-hub__panel bot-hub__panel--members">
        <div className="bot-hub__panel-title">
          <h2>{getStudioPageLabel(copy, "봇 허브에 담긴 챗봇 정보 목록")}</h2>
          <span>{getStudioRuntimeMessage(uiLanguage, "{count}개").replace("{count}", String(hub.members.length))}</span>
        </div>
        {hub.members.length === 0 ? (
          <p className="bot-hub__empty">{getStudioPageLabel(copy,"담긴 봇이 없습니다. 봇 허브 구성을 클릭해 일반 봇을 추가하세요.")}</p>
        ) : (
          <table className="bot-hub__table">
            <thead>
              <tr>
                <th>{getStudioPageLabel(copy,"순서")}</th><th>{getStudioPageLabel(copy,"봇 이름")}</th><th>{getStudioPageLabel(copy,"표시명")}</th><th>{getStudioPageLabel(copy,"봇 설명")}</th><th>{getStudioPageLabel(copy,"운영버전")}</th><th>{getStudioPageLabel(copy,"학습상태")}</th><th>{getStudioPageLabel(copy,"학습 완료 시간")}</th><th>{getStudioPageLabel(copy, "봇 허브 스몰토크")}</th>
              </tr>
            </thead>
            <tbody>
              {hub.members.map((member, index) => {
                const bot = botsById[member.bot_id];
                const version = bot?.active_version;
                return (
                  <tr key={member.id}>
                    <td>{index + 1}</td>
                    <td>{member.name || bot?.name || getStudioRuntimeMessage(uiLanguage, "삭제된 봇")}</td>
                    <td>{member.display_name || member.name || bot?.name || "-"}</td>
                    <td>{bot?.description || "-"}</td>
                    <td>{version?.name || "-"}</td>
                    <td>{(version?.is_trained || candidatesById[member.bot_id]?.is_trained) ? getStudioRuntimeMessage(uiLanguage, "학습 완료") : member.has_operating_version ? getStudioRuntimeMessage(uiLanguage, "운영") : getStudioRuntimeMessage(uiLanguage, "운영 버전 없음")}</td>
                    <td>{version?.updated_at ? new Date(version.updated_at).toLocaleString("ko-KR") : "-"}</td>
                    <td>{member.use_as_small_talk ? getStudioRuntimeMessage(uiLanguage, "사용") : getStudioRuntimeMessage(uiLanguage, "미사용")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <button
        type="button"
        className="bot-hub__simulator-fab"
        disabled={!hub.active_version_id}
        title={hub.active_version_id ? getStudioRuntimeMessage(uiLanguage, "봇 허브 대화 시뮬레이터 열기") : getStudioRuntimeMessage(uiLanguage, "운영 버전을 지정한 후 사용할 수 있습니다.")}
        onClick={() => setSimulatorOpen(true)}
      >
        대화 시뮬레이터
      </button>

      {simulatorOpen && hub.active_version_id ? (
        <div className="simulator-popup-backdrop" role="presentation">
          <div className="simulator-popup-shell" role="dialog" aria-modal="true" aria-label={getStudioPageLabel(copy,"봇 허브 시뮬레이터")}>
            <SimulatorPage
              embedded
              botIdOverride={hubId}
              versionIdOverride={hub.active_version_id}
              onClose={() => setSimulatorOpen(false)}
            />
          </div>
        </div>
      ) : null}
    </main>
  );
}
