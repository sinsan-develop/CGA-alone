"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { apiRequest } from "@/lib/api";
import { fetchAdminLicense, type AdminLicenseStatusResponse } from "@/lib/admin-api";
import {
  clearAuthSession,
  loadAuthSession,
  loadLastBotScreen,
  refreshAuthSessionCookies,
  hasAdminOperationsRole,
  hasAdminRole,
  hasApiRole,
  type AuthSession,
} from "@/lib/auth";
import { prefetchStudioBots } from "@/lib/studio-bots-api";
import { useI18n } from "@/components/language-provider";
import { normalizeSupportedLanguage, SUPPORTED_LANGUAGES } from "@/lib/language";
import { SHELL_NAVIGATION } from "@/lib/i18n/shell-navigation";
import { ADMIN_NAVIGATION_CATALOGS, buildAdminNavigationGroups } from "@/lib/i18n/admin-navigation";
import { ACCOUNT_PAGE_CATALOGS, getAccountRoleLabel } from "@/lib/i18n/account-pages";
import { STUDIO_RAIL_CATALOGS, formatStudioRailText } from "@/lib/i18n/studio-rail";
import { GETTING_STARTED_CATALOGS, type GettingStartedCourseId } from "@/lib/i18n/getting-started";
import { buildStudioSelectionHref, resolveStudioWorkContext } from "@/components/studio-work-context";

type PrimaryPanelId = "operations" | "build" | "api" | "admin";

type GettingStartedPhase = "introduction" | "course";

type BuildNavigationItem = {
  label: string;
  href: string;
  active: boolean;
};

type BuildNavigationGroup = {
  code: string;
  title: string;
  href?: string;
  active?: boolean;
  items?: BuildNavigationItem[];
};

export function StudioRail() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const { language, setLanguage, t } = useI18n();
  const navigation = SHELL_NAVIGATION[language];
  const railCopy = STUDIO_RAIL_CATALOGS[language];
  const gettingStartedCopy = GETTING_STARTED_CATALOGS[language];
  const accountCatalog = ACCOUNT_PAGE_CATALOGS[language];
  const adminGroups = useMemo(
    () => buildAdminNavigationGroups(ADMIN_NAVIGATION_CATALOGS[language]),
    [language],
  );
  const [session, setSession] = useState<AuthSession | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [activePanel, setActivePanel] = useState<PrimaryPanelId | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [gettingStartedOpen, setGettingStartedOpen] = useState(false);
  const [gettingStartedSlide, setGettingStartedSlide] = useState(0);
  const [gettingStartedMode, setGettingStartedMode] = useState<GettingStartedCourseId>("explore");
  const [gettingStartedPhase, setGettingStartedPhase] = useState<GettingStartedPhase>("introduction");
  const [gettingStartedStep, setGettingStartedStep] = useState(0);
  const [hideGettingStarted, setHideGettingStarted] = useState(false);
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const [licenseStatus, setLicenseStatus] = useState<AdminLicenseStatusResponse | null>(null);
  const accountMenuRef = useRef<HTMLDivElement | null>(null);
  const helpMenuRef = useRef<HTMLDivElement | null>(null);

  const currentGettingStartedSlide = railCopy.gettingStartedSlides[gettingStartedSlide];
  const currentGettingStartedCourse = gettingStartedCopy.courses[gettingStartedMode];
  const currentGettingStartedStep = currentGettingStartedCourse.steps[gettingStartedStep];
  const manualHref = (manual: "getting-started" | "user-manual" | "nlu-guide") =>
    `/manuals/cga-${manual}-${language}.pdf`;

  useEffect(() => {
    const currentSession = loadAuthSession();
    setSession(currentSession);
    refreshAuthSessionCookies();
    if (currentSession) {
      prefetchStudioBots(currentSession.access_token);
      fetchAdminLicense(currentSession.access_token)
        .then(setLicenseStatus)
        .catch(() => setLicenseStatus(null));
    }
  }, []);

  useEffect(() => {
    if (!menuOpen) {
      return;
    }

    function handlePointerDown(event: MouseEvent) {
      if (!accountMenuRef.current?.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (!helpOpen) {
      return;
    }

    function handlePointerDown(event: MouseEvent) {
      if (!helpMenuRef.current?.contains(event.target as Node)) {
        setHelpOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
    };
  }, [helpOpen]);

  useEffect(() => {
    if (!gettingStartedOpen) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setGettingStartedOpen(false);
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [gettingStartedOpen]);

  useEffect(() => {
    setActivePanel(null);
  }, [pathname]);

  async function handleLogout() {
    if (!session || isLoggingOut) {
      return;
    }

    setIsLoggingOut(true);

    try {
      const lastBotScreen = loadLastBotScreen(session.user.login_id);
      await apiRequest<{ message: string }>(
        "/api/v1/auth/logout",
        {
          method: "POST",
          body: JSON.stringify({
            last_bot_screen: lastBotScreen,
          }),
        },
        session.access_token,
      );
    } catch {
      // 서버 요청이 실패해도 로컬 세션은 정리한다.
    } finally {
      clearAuthSession();
      setSession(null);
      setMenuOpen(false);
      setIsLoggingOut(false);
      router.push("/login");
      router.refresh();
    }
  }

  function openGettingStarted(slide = 0) {
    setHelpOpen(false);
    setGettingStartedSlide(slide);
    setGettingStartedMode("explore");
    setGettingStartedPhase("introduction");
    setGettingStartedStep(0);
    setHideGettingStarted(window.localStorage.getItem("cga.getting-started.hidden") === "true");
    setGettingStartedOpen(true);
  }

  function closeGettingStarted(savePreference = false) {
    if (savePreference) {
      window.localStorage.setItem("cga.getting-started.hidden", "true");
    }
    setGettingStartedOpen(false);
  }

  function startGettingStarted() {
    setGettingStartedStep(0);
    setGettingStartedPhase("course");
  }

  function finishGettingStarted() {
    closeGettingStarted(hideGettingStarted);
    router.push(gettingStartedMode === "create" ? "/studio/bots/new" : "/studio/bots");
  }

  function switchGettingStartedCourse() {
    setGettingStartedMode((current) => (current === "explore" ? "create" : "explore"));
    setGettingStartedStep(0);
  }

  const accountInitial = session?.user.name?.trim().charAt(0) || session?.user.login_id?.trim().charAt(0) || "A";
  const roles = session?.user.roles ?? [];
  const roleSummary = session ? roles.map((role) => getAccountRoleLabel(accountCatalog, role)).join(", ") : "";
  const canUseApi = hasApiRole(roles);
  const canUseAdmin = hasAdminOperationsRole(roles);
  const groupSummary = session?.user.group_name ?? "-";
  const organizationSummary = session?.user.organization_name ?? railCopy.defaultServer;
  const lastBotScreen = session ? loadLastBotScreen(session.user.login_id) : null;
  const currentContext = resolveStudioWorkContext({
    pathname,
    selectedBotId: searchParams.get("bot"),
    selectedVersionId: searchParams.get("version"),
    lastBotScreen,
  });
  const currentBotId = currentContext.botId;
  const currentVersionId = currentContext.versionId;
  const hasBotContext = Boolean(currentBotId);
  const resolvedVersionId = currentVersionId || "v1";
  const currentBotBasePath = hasBotContext ? `/studio/bots/${currentBotId}/versions/${resolvedVersionId}` : "";
  const botContextFallback = "/studio/bots";
  const botWorkspaceHref = hasBotContext ? `${currentBotBasePath}/intents` : botContextFallback;
  const isBuildBotSelectionPath = pathname === "/studio/workspace" && searchParams.get("mode") === "build";
  const botSelectionHref = hasBotContext
    ? buildStudioSelectionHref("/studio/workspace", "mode=build", currentBotId, currentVersionId)
    : "/studio/workspace?mode=build";
  const visibleAdminGroups = hasAdminRole(roles)
    ? adminGroups
    : adminGroups.filter((group) => group.key === "status");
  const primaryItems: Array<{ id: PrimaryPanelId; label: string; icon: string }> = [
    { id: "operations", label: t("nav.operations"), icon: "operations" },
    { id: "build", label: t("nav.build"), icon: "build" },
    ...(canUseApi ? [{ id: "api" as const, label: "API", icon: "api" }] : []),
    ...(canUseAdmin ? [{ id: "admin" as const, label: "Admin", icon: "admin" }] : []),
  ];
  const isApiPath = pathname.startsWith("/studio/apis") || pathname.includes("/apis");
  const isDbOperationsDashboardPath = pathname === "/admin/operations-dashboard";
  const isAdminPath = pathname.startsWith("/admin") && !isDbOperationsDashboardPath;
  const isOperationPath =
    isDbOperationsDashboardPath ||
    pathname === "/studio/bots" ||
    (pathname === "/studio/workspace" && !isBuildBotSelectionPath) ||
    pathname === "/studio/operations-dashboard" ||
    pathname.startsWith("/studio/hubs") ||
    pathname.includes("/retraining") ||
    pathname.includes("/analysis");
  const activePrimaryId: PrimaryPanelId = isAdminPath
    ? "admin"
    : isApiPath
      ? "api"
      : isOperationPath
        ? "operations"
        : "build";
  const operationLinks = [
    { code: "BM", label: navigation.botManagement, href: "/studio/bots" },
    { code: "BOT", label: navigation.botWorkspace, href: "/studio/workspace" },
    { code: "DB", label: navigation.dbDashboard, href: "/admin/operations-dashboard" },
    { code: "RT", label: navigation.retraining, href: hasBotContext ? `${currentBotBasePath}/retraining` : botContextFallback },
    { code: "AN", label: navigation.analysis, href: hasBotContext ? `${currentBotBasePath}/analysis` : botContextFallback },
  ];
  const botSettingsBasePath = hasBotContext ? `/studio/bots/${currentBotId}/settings` : "";
  const botSettingsVersionQuery = `?version=${resolvedVersionId}`;
  const buildNavigationGroups: BuildNavigationGroup[] = [
    {
      code: "01",
      title: navigation.botSelect,
      href: botSelectionHref,
      active: isBuildBotSelectionPath,
    },
    {
      code: "02",
      title: navigation.botCreate,
      href: "/studio/bots/new",
      active: pathname === "/studio/bots/new",
    },
    {
      code: "03",
      title: navigation.botSettings,
      items: [
        { label: navigation.aiModelSettings, path: "", active: pathname === botSettingsBasePath },
        { label: navigation.defaultsSettings, path: "/conversation-defaults", active: pathname.startsWith(`${botSettingsBasePath}/conversation-defaults`) },
        { label: navigation.messageSettings, path: "/messages", active: pathname.startsWith(`${botSettingsBasePath}/messages`) },
        { label: navigation.messengerFeatures, path: "/messenger", active: pathname.startsWith(`${botSettingsBasePath}/messenger`) },
        { label: navigation.blocklistSettings, path: "/blocklist", active: pathname.startsWith(`${botSettingsBasePath}/blocklist`) },
        { label: navigation.ruleSettings, path: "/rules", active: pathname.startsWith(`${botSettingsBasePath}/rules`) },
        { label: navigation.smalltalk, path: "/smalltalk", active: pathname.startsWith(`${botSettingsBasePath}/smalltalk`) },
        { label: navigation.botstation, path: "/botstation", active: pathname.startsWith(`${botSettingsBasePath}/botstation`) },
      ].map((item) => ({
        ...item,
        href: hasBotContext ? `${botSettingsBasePath}${item.path}${botSettingsVersionQuery}` : botContextFallback,
      })),
    },
    {
      code: "04",
      title: navigation.botConfigure,
      href: hasBotContext ? `${currentBotBasePath}/configure` : botContextFallback,
      active: pathname.includes("/configure"),
    },
    {
      code: "05",
      title: navigation.botBuild,
      items: [
        { label: navigation.intentManagement, href: botWorkspaceHref, active: pathname.includes("/intents") },
        { label: navigation.entityManagement, href: hasBotContext ? `${currentBotBasePath}/entities` : botContextFallback, active: pathname.includes("/entities") },
        { label: navigation.dictionaryManagement, href: hasBotContext ? `${currentBotBasePath}/dictionary` : botContextFallback, active: pathname.includes("/dictionary") },
      ],
    },
    {
      code: "06",
      title: navigation.botTest,
      href: hasBotContext ? `${currentBotBasePath}/simulator` : botContextFallback,
      active: pathname.includes("/simulator"),
    },
    {
      code: "07",
      title: navigation.botEvaluation,
      href: hasBotContext ? `${currentBotBasePath}/evaluation` : botContextFallback,
      active: pathname.includes("/evaluation"),
    },
  ];
  const panelTitle =
    activePanel === "operations" ? navigation.operationsPanel : activePanel === "build" ? navigation.buildPanel : navigation.systemPanel;
  const panelLinks = activePanel === "operations" ? operationLinks : [];
  const currentLicense = licenseStatus?.license ?? null;
  const licenseProduct = currentLicense?.product?.trim() || railCopy.noLicense;
  const licenseId = currentLicense?.license_id?.trim() || "-";
  const licenseExpiresAt = currentLicense?.expires_at?.trim() || "-";

  function handleLanguageChange(value: string) {
    setLanguage(normalizeSupportedLanguage(value));
  }

  return (
    <aside className="studio-rail">
      <nav className="studio-rail__menu" aria-label={railCopy.mainMenu}>
        {primaryItems.map((item) => {
          const isActive = activePrimaryId === item.id;

          return (
            <button
              type="button"
              key={item.id}
              className={`studio-rail__item${isActive ? " is-active" : ""}`}
              aria-label={item.label}
              title={item.label}
              aria-expanded={item.id === "api" ? undefined : activePanel === item.id}
              onClick={() => {
                if (item.id === "api") {
                  setActivePanel(null);
                  router.push("/studio/apis");
                  return;
                }

                setActivePanel((current) => (current === item.id ? null : item.id));
              }}
            >
              <span className={`studio-rail__glyph studio-rail__glyph--${item.icon}`} aria-hidden="true">
                <span className={`studio-rail__icon studio-rail__icon--${item.icon}`} />
              </span>
              <span className="studio-rail__label">{item.label}</span>
            </button>
          );
        })}
      </nav>

      {activePanel ? (
        <section
          className={`studio-rail__context-panel${
            activePanel === "admin"
              ? " studio-rail__context-panel--admin"
              : activePanel === "build"
                ? " studio-rail__context-panel--build"
                : ""
          }`}
          aria-label={`${panelTitle} ${railCopy.menu}`}
        >
          <header>
            <strong>{panelTitle}</strong>
            <button type="button" onClick={() => setActivePanel(null)} aria-label={railCopy.closeMenu}>×</button>
          </header>
          {activePanel === "build" ? (
            <div className="studio-rail__build-groups">
              {buildNavigationGroups.map((group) => {
                const groupActive = Boolean(group.active || group.items?.some((item) => item.active));

                return (
                  <section key={`${group.code}:${group.title}`} className={`studio-rail__build-group${groupActive ? " is-active" : ""}`}>
                    {group.href ? (
                      <Link
                        href={group.href}
                        className={`studio-rail__build-heading${group.active ? " is-active" : ""}`}
                        onClick={() => setActivePanel(null)}
                      >
                        <span>{group.code}</span>
                        <strong>{group.title}</strong>
                      </Link>
                    ) : (
                      <div className="studio-rail__build-heading">
                        <span>{group.code}</span>
                        <strong>{group.title}</strong>
                      </div>
                    )}
                    {group.items ? (
                      <nav aria-label={group.title}>
                        {group.items.map((item) => (
                          <Link
                            key={item.href}
                            href={item.href}
                            className={item.active ? "is-active" : ""}
                            onClick={() => setActivePanel(null)}
                          >
                            {item.label}
                          </Link>
                        ))}
                      </nav>
                    ) : null}
                  </section>
                );
              })}
            </div>
          ) : activePanel === "admin" ? (
            <div className="studio-rail__admin-groups">
              {visibleAdminGroups.map((group) => (
                <section key={group.title} className="studio-rail__admin-group">
                  <h3>{group.title}</h3>
                  <nav aria-label={group.title}>
                    {group.items.map((item) => (
                      <Link key={item.href} href={item.href} onClick={() => setActivePanel(null)}>
                        {item.label}
                      </Link>
                    ))}
                  </nav>
                </section>
              ))}
            </div>
          ) : (
            <nav>
              {panelLinks.map((item, index) => (
                <Link key={`${item.code}:${item.href}:${index}`} href={item.href} onClick={() => setActivePanel(null)}>
                  <span>{item.code}</span>
                  <strong>{item.label}</strong>
                </Link>
              ))}
            </nav>
          )}
        </section>
      ) : null}

      <div className="studio-rail__menu studio-rail__menu--bottom" ref={helpMenuRef}>
        <button
          type="button"
          className={`studio-rail__help-button${helpOpen ? " is-active" : ""}`}
          aria-label={t("nav.help")}
          aria-expanded={helpOpen}
          onClick={() => setHelpOpen((current) => !current)}
        >
          ?
        </button>
        {helpOpen ? (
          <div className="studio-rail__help-popover">
            <section className="studio-rail__help-section">
              <strong>About CGA Studio</strong>
              <dl className="studio-rail__help-meta">
                <div>
                  <dt>{t("help.license")}</dt>
                  <dd>
                    <b>{licenseProduct}</b>
                    <span>{licenseId} ({licenseExpiresAt} {t("help.expires")})</span>
                  </dd>
                </div>
                <div>
                  <dt>{t("help.version")}</dt>
                  <dd>v1.1</dd>
                </div>
              </dl>
            </section>
            <section className="studio-rail__help-section studio-rail__help-section--links">
              <strong>{t("nav.help")}</strong>
              <a href="/manuals/cga-alone-guide-ko.html" target="_blank" rel="noreferrer" lang="ko">
                {language === "ko" ? "CGA-alone 기능 안내" : "CGA-alone guide (한국어)"}
              </a>
              <a href="/manuals/channel-connections-ko.html" target="_blank" rel="noreferrer" lang="ko">
                {language === "ko" ? "채널 연결 관리" : "Channel connections (한국어)"}
              </a>
              <button
                type="button"
                className="studio-rail__help-title"
                aria-haspopup="dialog"
                onClick={() => openGettingStarted()}
              >
                {language === "ko" ? "시작하기 체험" : t("gettingStarted.title")}
              </button>
              <a href={manualHref("getting-started")} target="_blank" rel="noreferrer">
                {language === "ko" ? "시작하기" : t("gettingStarted.title")} PDF
              </a>
              <a href={manualHref("user-manual")} target="_blank" rel="noreferrer">
                {t("help.userManual")}
              </a>
              <a href={manualHref("nlu-guide")} target="_blank" rel="noreferrer">
                {t("help.nluGuide")}
              </a>
            </section>
          </div>
        ) : null}
      </div>

      {gettingStartedOpen ? (
        <div
          className="studio-getting-started__backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              closeGettingStarted(hideGettingStarted);
            }
          }}
        >
          <section
            className="studio-getting-started"
            role="dialog"
            aria-modal="true"
            aria-labelledby="cga-getting-started-title"
          >
            <header className="studio-getting-started__header">
              <strong id="cga-getting-started-title">{t("gettingStarted.title")}</strong>
              <button type="button" aria-label={`${t("gettingStarted.title")} ${t("common.close")}`} onClick={() => closeGettingStarted(hideGettingStarted)}>
                ×
              </button>
            </header>

            {gettingStartedPhase === "introduction" ? (
              <>
                <div className={`studio-getting-started__hero is-${currentGettingStartedSlide.variant}`}>
                  <div className="studio-getting-started__illustration" aria-hidden="true">
                    <div className="studio-getting-started__bot">
                      <span className="studio-getting-started__bot-eye" />
                      <span className="studio-getting-started__bot-eye" />
                    </div>
                    <div className="studio-getting-started__cards">
                      <span>Intent</span>
                      <span>Bot Station</span>
                      <span>Analytics</span>
                    </div>
                  </div>
                  <div className="studio-getting-started__copy">
                    <h2>{currentGettingStartedSlide.title}</h2>
                    <p>{currentGettingStartedSlide.description}</p>
                  </div>
                  <button
                    type="button"
                    className="studio-getting-started__next"
                    aria-label={railCopy.nextIntroduction}
                    onClick={() => setGettingStartedSlide((current) => (current + 1) % railCopy.gettingStartedSlides.length)}
                  >
                    ›
                  </button>
                  <div className="studio-getting-started__dots" aria-label={railCopy.introductionSelection}>
                    {railCopy.gettingStartedSlides.map((slide, index) => (
                      <button
                        key={slide.variant}
                        type="button"
                        className={index === gettingStartedSlide ? "is-active" : ""}
                        aria-label={formatStudioRailText(railCopy.introductionAt, { index: index + 1 })}
                        aria-current={index === gettingStartedSlide ? "step" : undefined}
                        onClick={() => setGettingStartedSlide(index)}
                      />
                    ))}
                  </div>
                </div>

                <div className="studio-getting-started__body">
                  <h3>{t("gettingStarted.choose")}</h3>
                  <div className="studio-getting-started__course-options" role="radiogroup" aria-label={railCopy.experienceMode}>
                    {(["explore", "create"] as const).map((mode) => (
                      <label key={mode} className={gettingStartedMode === mode ? "is-selected" : ""}>
                        <input type="radio" name="getting-started-mode" value={mode} checked={gettingStartedMode === mode} onChange={() => setGettingStartedMode(mode)} />
                        <strong>{mode === "explore" ? t("gettingStarted.explore") : t("gettingStarted.create")}</strong>
                        <span>{gettingStartedCopy.courses[mode].description}</span>
                      </label>
                    ))}
                  </div>
                  <div className="studio-getting-started__actions">
                    <button type="button" className="studio-getting-started__secondary" onClick={() => closeGettingStarted(hideGettingStarted)}>
                      {t("gettingStarted.freeStart")}
                    </button>
                    <button type="button" className="studio-getting-started__primary" onClick={startGettingStarted}>
                      {t("gettingStarted.start")}
                    </button>
                  </div>
                  <label className="studio-getting-started__remember">
                    <input type="checkbox" checked={hideGettingStarted} onChange={(event) => setHideGettingStarted(event.target.checked)} />
                    <span>{t("gettingStarted.hide")}</span>
                  </label>
                </div>
              </>
            ) : (
              <div className="studio-getting-started__course">
                <div className="studio-getting-started__course-copy">
                  <span>{`${t("gettingStarted.title")} · ${gettingStartedMode === "explore" ? t("gettingStarted.explore") : t("gettingStarted.create")} · ${currentGettingStartedStep.section}`}</span>
                  <h2>{currentGettingStartedStep.title}</h2>
                  <p>{currentGettingStartedStep.description}</p>
                </div>
                <ol className="studio-getting-started__progress" aria-label={gettingStartedCopy.progress.replace("{current}", String(gettingStartedStep + 1)).replace("{total}", String(currentGettingStartedCourse.steps.length))}>
                  {currentGettingStartedCourse.steps.map((step, index) => (
                    <li key={`${step.section}:${index}`} className={index === gettingStartedStep ? "is-active" : index < gettingStartedStep ? "is-passed" : ""}>
                      <button type="button" onClick={() => setGettingStartedStep(index)} aria-current={index === gettingStartedStep ? "step" : undefined}>
                        <b>{index + 1}</b>
                        <span>{step.section}</span>
                      </button>
                    </li>
                  ))}
                </ol>
                {gettingStartedStep === currentGettingStartedCourse.steps.length - 1 ? (
                  <p className="studio-getting-started__finished">{gettingStartedCopy.finished}</p>
                ) : null}
                <div className="studio-getting-started__course-actions">
                  <button type="button" className="studio-getting-started__secondary" disabled={gettingStartedStep === 0} onClick={() => setGettingStartedStep((current) => current - 1)}>
                    {gettingStartedCopy.previous}
                  </button>
                  <span>{gettingStartedCopy.progress.replace("{current}", String(gettingStartedStep + 1)).replace("{total}", String(currentGettingStartedCourse.steps.length))}</span>
                  {gettingStartedStep < currentGettingStartedCourse.steps.length - 1 ? (
                    <button type="button" className="studio-getting-started__primary" onClick={() => setGettingStartedStep((current) => current + 1)}>
                      {gettingStartedCopy.next}
                    </button>
                  ) : (
                    <button type="button" className="studio-getting-started__primary" onClick={finishGettingStarted}>
                      {gettingStartedCopy.finish}
                    </button>
                  )}
                </div>
                <button type="button" className="studio-getting-started__switch" onClick={switchGettingStartedCourse}>
                  {gettingStartedCopy.switchCourse}
                </button>
                <label className="studio-getting-started__remember">
                  <input type="checkbox" checked={hideGettingStarted} onChange={(event) => setHideGettingStarted(event.target.checked)} />
                  <span>{t("gettingStarted.hide")}</span>
                </label>
              </div>
            )}
          </section>
        </div>
      ) : null}

      <div className="studio-rail__account" ref={accountMenuRef}>
        <button
          type="button"
          className={`studio-rail__account-button${menuOpen ? " is-active" : ""}`}
          aria-label={t("nav.userMenu")}
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((current) => !current)}
        >
          <span>{accountInitial}</span>
        </button>

        {menuOpen ? (
          <div className="studio-rail__account-popover">
            <button
              type="button"
              className="studio-rail__account-close"
              onClick={() => setMenuOpen(false)}
              aria-label={railCopy.closeUserMenu}
            >
              ×
            </button>
            {session ? (
              <>
                <div className="studio-rail__account-header">
                  <div className="studio-rail__account-avatar">{accountInitial}</div>
                  <div className="studio-rail__account-meta">
                    <strong>
                      {session.user.name} / {session.user.login_id}
                    </strong>
                    <span>
                      {roleSummary || railCopy.noPermission} / {groupSummary} / {organizationSummary}
                    </span>
                  </div>
                </div>

                <div className="studio-rail__account-actions">
                  <Link
                    href="/me/profile"
                    className="studio-rail__account-link"
                    onClick={() => setMenuOpen(false)}
                  >
                    {navigation.editProfile}
                  </Link>

                  <select
                    className="studio-rail__language-select"
                    value={language}
                    onChange={(event) => handleLanguageChange(event.target.value)}
                    aria-label={navigation.userLanguage}
                  >
                    {SUPPORTED_LANGUAGES.map((option) => (
                      <option key={option.code} value={option.code}>{option.label}</option>
                    ))}
                  </select>

                  <button
                    type="button"
                    className="studio-rail__account-logout"
                    onClick={handleLogout}
                    disabled={isLoggingOut}
                  >
                    {isLoggingOut ? t("common.loggingOut") : t("common.logout")}
                  </button>
                </div>
              </>
            ) : (
              <Link href="/login" className="studio-rail__account-link" onClick={() => setMenuOpen(false)}>
                {t("common.login")}
              </Link>
            )}
          </div>
        ) : null}
      </div>
    </aside>
  );
}
