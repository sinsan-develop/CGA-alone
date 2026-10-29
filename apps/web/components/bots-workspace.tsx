"use client";

import { getStudioRuntimeMessage } from "@/lib/i18n/studio-runtime-native";

import Link from "next/link";
import { ReactNode, useEffect, useMemo, useState } from "react";

import { loadAuthSession, type AuthSession } from "@/lib/auth";
import { fetchStudioBots, getCachedStudioBots, type StudioBotApiItem } from "@/lib/studio-bots-api";
import { useI18n } from "@/components/language-provider";
import { BOT_CREATE_CATALOGS } from "@/lib/i18n/bot-create";
import { BOT_WORKSPACE_CATALOGS } from "@/lib/i18n/bot-workspace";
import { getStudioPageLabel, STUDIO_PAGE_CATALOGS } from "@/lib/i18n/studio-pages";

type BotsWorkspaceProps = {
  activeTab?: "bot" | "hub";
  overlay?: ReactNode;
};

type WorkspaceItem = {
  id: string;
  name: string;
  type: "텍스트형" | "보이스형" | "봇 허브";
  updatedAt: string;
  updatedBy: string;
  groupName: string;
  status: string;
  trained: boolean;
  href: string;
};

function mapBotType(item: StudioBotApiItem): WorkspaceItem["type"] {
  if (item.data_json?.bot_kind === "hub") {
    return "봇 허브";
  }

  if (item.data_json?.bot_mode === "voice") {
    return "보이스형";
  }

  return "텍스트형";
}

function mapWorkspaceItem(item: StudioBotApiItem): WorkspaceItem {
  const type = mapBotType(item);
  const versionRoute =
    item.active_version?.name ??
    (item.version_count > 0 ? "v1" : null);
  const updatedBy = item.active_version?.updated_by_login_id || item.group_name || "-";
  const trained =
    item.active_version?.is_trained === true ||
    item.active_version?.nlu_training?.status === "success" ||
    typeof item.active_version?.nlu_training?.trained_at === "string";

  return {
    id: item.id,
    name: item.name,
    type,
    updatedAt: item.updated_at.replace("T", " ").slice(0, 16),
    updatedBy,
    groupName: item.group_name || "-",
    status: item.status,
    trained,
    href:
      type === "봇 허브"
        ? `/studio/hubs/${item.id}`
        : versionRoute
          ? `/studio/bots/${item.id}/versions/${versionRoute}/intents`
          : `/studio/bots/${item.id}`,
  };
}

export function BotsWorkspace({ activeTab = "bot", overlay }: BotsWorkspaceProps) {
  const { language: uiLanguage } = useI18n();
  const workspaceCopy = BOT_WORKSPACE_CATALOGS[uiLanguage];
  const copy = STUDIO_PAGE_CATALOGS[uiLanguage];
  const [session, setSession] = useState<AuthSession | null>(null);
  const [token, setToken] = useState("");
  const [items, setItems] = useState<WorkspaceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    const session = loadAuthSession();
    if (!session) {
      setLoading(false);
      setErrorMessage(getStudioRuntimeMessage(uiLanguage, "로그인이 필요합니다."));
      return;
    }

    setSession(session);
    setToken(session.access_token);
    const cachedBots = getCachedStudioBots(session.access_token);
    if (cachedBots) {
      setItems(cachedBots.map(mapWorkspaceItem));
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!token) {
      return;
    }

    let ignore = false;
    setLoading(!getCachedStudioBots(token));
    setErrorMessage("");

    fetchStudioBots(token)
      .then((response) => {
        if (ignore) {
          return;
        }
        setItems(response.map(mapWorkspaceItem));
      })
      .catch((error) => {
        if (!ignore) {
          setErrorMessage(error instanceof Error ? error.message : workspaceCopy.loadBotsError);
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
  }, [token]);

  const favoriteBotIds = session?.user.favorite_bot_ids ?? [];
  const botItems = useMemo(() => {
    const filtered = items.filter((item) => (activeTab === "bot" ? item.type !== "봇 허브" : item.type === "봇 허브"));

    return [...filtered].sort((left, right) => {
      const leftFavoriteIndex = favoriteBotIds.indexOf(left.id);
      const rightFavoriteIndex = favoriteBotIds.indexOf(right.id);
      const leftFavorite = leftFavoriteIndex >= 0;
      const rightFavorite = rightFavoriteIndex >= 0;

      if (leftFavorite || rightFavorite) {
        if (leftFavorite && rightFavorite) {
          return leftFavoriteIndex - rightFavoriteIndex;
        }
        return leftFavorite ? -1 : 1;
      }

      return left.name.localeCompare(right.name, "ko");
    });
  }, [activeTab, favoriteBotIds, items]);
  const activeItemId = botItems[0]?.id;
  const botCount = items.filter((item) => item.type !== "봇 허브").length;
  const hubCount = items.filter((item) => item.type === "봇 허브").length;

  return (
    <section className="bots-workspace">
      {!loading && !errorMessage && items.length === 0 ? (
        <Link href="/studio/bots/new" className="bots-workspace__top-create">
          + {BOT_CREATE_CATALOGS[uiLanguage].createBot}
        </Link>
      ) : null}

      <div className="bots-workspace__tabs">
        <Link href="/studio/bots" className={activeTab === "bot" ? "is-active" : ""}>
          {workspaceCopy.bot}({botCount})
        </Link>
        <Link href="/studio/hubs" className={activeTab === "hub" ? "is-active" : ""}>
          {getStudioPageLabel(copy, "봇 허브")}({hubCount})
        </Link>
      </div>

      <div className="bots-workspace__body bots-workspace__body--list">
        <aside className="bots-workspace__list">
          <div className="bots-workspace__items">
            {botItems.map((item) => (
              <Link
                key={item.id}
                href={item.href}
                className={`bots-workspace__item${item.id === activeItemId ? " is-active" : ""}`}
              >
                <span className="bots-workspace__avatar" aria-hidden="true">
                  <span />
                  <span />
                </span>
                <div className="bots-workspace__summary">
                  <div className="bots-workspace__title-row">
                    <span className={`bots-workspace__state bots-workspace__state--${item.trained ? "trained" : "draft"}`} aria-hidden="true">
                      {item.trained ? "✓" : "−"}
                    </span>
                    <strong>{item.name}</strong>
                  </div>
                  <p>
                    {item.updatedAt} <span>{item.updatedBy}</span>
                  </p>
                </div>
                <div className="bots-workspace__meta">
                  <small>{getStudioPageLabel(copy, item.type)}</small>
                  <small>{item.status === "active" ? getStudioPageLabel(copy, "사용") : item.status}</small>
                </div>
              </Link>
            ))}
          </div>

          {loading ? <p className="bots-workspace__notice">{getStudioPageLabel(copy, "봇 목록을 불러오는 중입니다...")}</p> : null}
          {errorMessage ? <p className="bots-workspace__notice bots-workspace__notice--error">{errorMessage}</p> : null}
        </aside>

        {items.length === 0 && !loading && !errorMessage ? (
          <div className="bots-workspace__empty">
            <div className="bots-workspace__empty-icon">
              <span className="bots-workspace__empty-face">
                <span />
                <span />
              </span>
            </div>
            <p>
              {activeTab === "bot"
                ? getStudioPageLabel(copy, "아직 생성된 봇이 없네요.")
                : getStudioPageLabel(copy, "아직 생성된 봇 허브가 없네요.")}
              <br />
              {uiLanguage === "ko"
                ? getStudioRuntimeMessage(uiLanguage, "새로운 {type}을 만들고 메인 화면에서 이어서 작업하세요.").replace("{type}", activeTab === "bot" ? getStudioRuntimeMessage(uiLanguage, "봇") : getStudioRuntimeMessage(uiLanguage, "봇 허브"))
                : `Create a new ${activeTab === "bot" ? "bot" : "Bot Hub"} and continue from its main screen.`}
            </p>
            <Link href="/studio/bots/new" className="bots-workspace__create-button">
              + {BOT_CREATE_CATALOGS[uiLanguage].createBot}
            </Link>
          </div>
        ) : null}
      </div>

      {overlay ? (
        <>
          <div className="bots-workspace__scrim" />
          <div className="bots-workspace__overlay">{overlay}</div>
        </>
      ) : null}
    </section>
  );
}
