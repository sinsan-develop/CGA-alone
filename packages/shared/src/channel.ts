export type AidotChannelType = "webchat" | "kakao" | "ms-teams" | "slack" | "discord" | "telegram" | "google-chat";

export type AidotInitialChannelMessage = {
  type?: string;
  messageType?: string;
  text: string;
  options?: string[];
  payload?: AidotChannelStoredMessage["payload"];
};

export type AidotChannelBot = {
  id: string;
  name: string;
  groupId?: string;
  groupName: string | null;
  activeVersionId: string;
  activeVersionName: string;
  activeVersionNo: number;
  activatedAt?: string | null;
  initialMessages?: AidotInitialChannelMessage[];
};

export type AidotChannelRoom = {
  id: string;
  clientRoomId?: string;
  channelType: AidotChannelType;
  status?: string;
  bot: AidotChannelBot;
  createdAt: string;
  updatedAt: string;
};

export type AidotChannelStoredMessage = {
  id: string;
  participantId: string;
  participantKind: "bot" | "user" | "system";
  participantName: string;
  messageType?: string;
  text: string;
  payload?: { options?: string[]; columns?: string[]; rows?: Record<string, unknown>[]; keyColumn?: string; adaptiveCard?: unknown; richForm?: unknown; rawJson?: string; sourceTalkNodeId?: string };
  createdAt: string;
};

export type AidotChannelConnectResponse = {
  channelType: AidotChannelType;
  connected: boolean;
  clientId: string;
  bots: AidotChannelBot[];
};

export type AidotChannelRoomResponse = {
  room: AidotChannelRoom;
  initialMessages: AidotInitialChannelMessage[];
  messages?: AidotChannelStoredMessage[];
};

export type AidotChannelRoomListResponse = {
  rooms: AidotChannelRoom[];
};

export type AidotChannelRoomDetailResponse = {
  room: AidotChannelRoom;
  messages: AidotChannelStoredMessage[];
};

export type AidotChannelMessageResponse = {
  botMessage: AidotChannelStoredMessage | null;
  botMessages?: AidotChannelStoredMessage[];
  intent: { id: string | null; name: string | null; score: number };
  runtime?: {
    dialogEnded: boolean;
    sessionEnded: boolean;
    completionReason?: string | null;
    waitingNodeId?: string | null;
  };
};

export type AidotApiResponse<T> = {
  data: T;
  meta?: Record<string, unknown>;
};

export type AidotApiError = Error & {
  status?: number;
};

const WEBCHAT_API_KEY = process.env.NEXT_PUBLIC_AIDOT_WEBCHAT_API_KEY || process.env.AIDOT_WEBCHAT_API_KEY || "";

export const DEFAULT_AIDOT_CHANNEL_SERVER_URL = "http://localhost:8320";

export function normalizeAidotServerUrl(value?: string | null) {
  const trimmed = (value || "").trim();
  if (!trimmed) return DEFAULT_AIDOT_CHANNEL_SERVER_URL;
  try {
    const url = new URL(trimmed);
    return url.protocol === "http:" || url.protocol === "https:" ? url.origin : DEFAULT_AIDOT_CHANNEL_SERVER_URL;
  } catch {
    return DEFAULT_AIDOT_CHANNEL_SERVER_URL;
  }
}

export async function callAidotChannelApi<T>(serverUrl: string, path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (WEBCHAT_API_KEY) {
    headers.set("X-Aidot-Webchat-Key", WEBCHAT_API_KEY);
  }
  const response = await fetch(`${normalizeAidotServerUrl(serverUrl)}/api/v1${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });
  const payload = (await response.json().catch(() => null)) as AidotApiResponse<T> | { detail?: string } | null;
  if (!response.ok) {
    const message = payload && "detail" in payload && payload.detail ? payload.detail : "Aidot backend 호출에 실패했습니다.";
    const error = new Error(message) as AidotApiError;
    error.status = response.status;
    throw error;
  }
  if (!payload || !("data" in payload)) {
    throw new Error("Aidot backend 응답 형식이 올바르지 않습니다.");
  }
  return payload.data;
}



export type AidotRoomTurnState = {
  state: "idle" | "processing" | "waiting_input";
  ownerParticipantId: string | null;
  canAskBot: boolean;
};

export type AidotSharedRoomIdentity = {
  roomId: string;
  participantId: string;
  conversationToken: string;
};

export type AidotRoomClientState = {
  runtime?: AidotChannelMessageResponse["runtime"];
  activeVersion?: { id: string; name: string; versionNo: number };
};
export type AidotSharedRoomMembership = AidotSharedRoomIdentity & AidotRoomClientState & { turn: AidotRoomTurnState };
export type AidotSharedRoomHistoryResponse = AidotRoomClientState & {
  roomId: string;
  messages: AidotChannelStoredMessage[];
  turn: AidotRoomTurnState;
};
export type AidotSharedRoomMessageResponse = Omit<AidotChannelMessageResponse, "intent"> & AidotRoomClientState & {
  roomId: string;
  userMessage?: AidotChannelStoredMessage;
  intent?: AidotChannelMessageResponse["intent"];
  turn: AidotRoomTurnState;
};

// 화면의 입력 허용일 뿐이며 실제 소유권·차례는 서버가 다시 검증한다.
export function canAskAidotRoomBot(turn: AidotRoomTurnState | null | undefined, participantId: string, connected: boolean, sendingBot: boolean) {
  if (!connected || sendingBot || !participantId || turn?.canAskBot !== true) return false;
  if (turn.state === "idle") return turn.ownerParticipantId === null;
  return turn.state === "waiting_input" && turn.ownerParticipantId === participantId;
}

export function canRespondAidotRoomCard(runtime: AidotChannelMessageResponse["runtime"], sourceTalkNodeId: string | undefined, canAsk: boolean, messageId?: string, messages?: AidotChannelStoredMessage[]) {
  if (!(canAsk && sourceTalkNodeId && runtime?.waitingNodeId === sourceTalkNodeId && !runtime.dialogEnded && !runtime.sessionEnded)) return false;
  if (messageId !== undefined || messages !== undefined) {
    if (!messageId || !messages) return false;
    for (let index = messages.length - 1; index >= 0; index--) {
      if (messages[index].participantKind === "bot" && messages[index].payload?.sourceTalkNodeId === sourceTalkNodeId) return messages[index].id === messageId;
    }
    return false;
  }
  return true;
}

export function canSendAidotRoomChat(connected: boolean, sendingGeneral: boolean) {
  return connected && !sendingGeneral;
}

export async function createAidotSharedRoom(serverUrl: string, botId: string, participantName = "사용자", signal?: AbortSignal) {
  return callAidotChannelApi<AidotSharedRoomMembership>(serverUrl, `/webchat/bots/${encodeURIComponent(botId)}/shared-rooms`, {
    method: "POST", body: JSON.stringify({ participant_name: participantName.trim() || "사용자" }), signal,
  });
}

export async function joinAidotSharedRoom(serverUrl: string, botId: string, roomId: string, participantName = "사용자", signal?: AbortSignal) {
  return callAidotChannelApi<AidotSharedRoomMembership>(serverUrl, `/webchat/bots/${encodeURIComponent(botId)}/shared-rooms/${encodeURIComponent(roomId)}/participants`, {
    method: "POST", body: JSON.stringify({ participant_name: participantName.trim() || "사용자" }), signal,
  });
}

export async function fetchAidotSharedRoom(serverUrl: string, botId: string, member: AidotSharedRoomIdentity, signal?: AbortSignal) {
  return callAidotChannelApi<AidotSharedRoomHistoryResponse>(serverUrl, `/webchat/bots/${encodeURIComponent(botId)}/shared-rooms/${encodeURIComponent(member.roomId)}/messages`, {
    method: "GET", headers: { "X-Aidot-Conversation-Token": member.conversationToken }, signal,
  });
}

export async function sendAidotSharedRoomMessage(serverUrl: string, botId: string, member: AidotSharedRoomIdentity, message: string, addressed: boolean, signal?: AbortSignal) {
  return callAidotChannelApi<AidotSharedRoomMessageResponse>(serverUrl, `/webchat/bots/${encodeURIComponent(botId)}/rooms/${encodeURIComponent(member.roomId)}/messages`, {
    method: "POST", headers: { "X-Aidot-Conversation-Token": member.conversationToken },
    body: JSON.stringify({ message, addressed }), signal,
  });
}


export type AidotClientParticipantKind = "user" | "bot";
export type AidotClientMembership = AidotSharedRoomMembership & { participantKind: AidotClientParticipantKind };
export type AidotClientRoomResponse = AidotChannelRoomResponse & AidotClientMembership;

export async function createAidotClientRoom(serverUrl: string, channel: AidotChannelType, botId: string, roomMode: "private" | "shared", participantKind: AidotClientParticipantKind = "user", participantName = "사용자", signal?: AbortSignal) {
  return callAidotChannelApi<AidotClientRoomResponse>(serverUrl, `/channels/${channel}/rooms`, {
    method: "POST", body: JSON.stringify({ bot_id: botId, room_mode: roomMode, participant_kind: participantKind, participant_name: participantName }), signal,
  });
}

export async function joinAidotClientRoom(serverUrl: string, channel: AidotChannelType, roomId: string, participantKind: AidotClientParticipantKind = "user", participantName = "사용자", signal?: AbortSignal) {
  return callAidotChannelApi<AidotClientMembership>(serverUrl, `/channels/${channel}/rooms/${encodeURIComponent(roomId)}/participants`, {
    method: "POST", body: JSON.stringify({ participant_kind: participantKind, participant_name: participantName }), signal,
  });
}

export async function fetchAidotClientRoom(serverUrl: string, channel: AidotChannelType, member: AidotSharedRoomIdentity, signal?: AbortSignal) {
  return callAidotChannelApi<AidotChannelRoomDetailResponse & AidotRoomClientState & { turn: AidotRoomTurnState }>(serverUrl, `/channels/${channel}/rooms/${encodeURIComponent(member.roomId)}`, {
    method: "GET", headers: { "X-Aidot-Conversation-Token": member.conversationToken }, signal,
  });
}

export async function sendAidotClientMessage(serverUrl: string, channel: AidotChannelType, member: AidotSharedRoomIdentity, message: string, addressed: boolean, eventId?: string, signal?: AbortSignal) {
  return callAidotChannelApi<AidotSharedRoomMessageResponse>(serverUrl, `/channels/${channel}/rooms/${encodeURIComponent(member.roomId)}/messages`, {
    method: "POST", headers: { "X-Aidot-Conversation-Token": member.conversationToken, ...(eventId === undefined ? {} : { "X-Aidot-Event-Id": eventId }) },
    body: JSON.stringify({ message, addressed }), signal,
  });
}


export type AidotSlackConnection = { teamId: string; botUserId: string; botId: string };
export type AidotSlackEvent = {
  nativeRoomKey: string; nativeParticipantKey: string; participantKind: AidotClientParticipantKind;
  channelId: string; threadTs: string | null; roomMode: "private" | "shared";
  eventId: string; message: string; addressed: boolean; sourceMessageId?: string; sourceTalkNodeId?: string;
};
export type AidotSlackReplyPayload = {
  channel: string; text: string; thread_ts?: string; blocks?: Record<string, unknown>[];
  mrkdwn: false; parse: "none"; unfurl_links: false; unfurl_media: false;
};

function slackRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function slackId(value: unknown, field: string) {
  if (typeof value !== "string" || !/^[A-Za-z0-9]+$/.test(value) || value.length > 128) throw new Error(`Slack ${field}가 올바르지 않습니다.`);
  return value;
}
function slackTimestamp(value: unknown) {
  if (typeof value !== "string" || !/^\d+\.\d+$/.test(value) || value.length > 30) throw new Error("Slack 메시지 시각이 올바르지 않습니다.");
  return value;
}
function slackText(value: unknown, maximum: number, field: string) {
  if (typeof value !== "string" || !value.trim() || Array.from(value).length > maximum) throw new Error(`Slack ${field}는 1~${maximum}자여야 합니다.`);
  return value;
}
function slackScope(team: string, channel: string, thread: unknown, actor: string, kind: AidotClientParticipantKind) {
  const threadTs = thread === undefined || thread === null ? null : slackTimestamp(thread);
  return { nativeRoomKey: "slack:" + JSON.stringify([team, channel, threadTs]), nativeParticipantKey: `slack:${kind}:${actor}`, channelId: channel, threadTs };
}

// Slack 서명/설치 연결을 확인한 client가 호출한다. native ID는 CGA 소유권 증명이 아니다.
export function normalizeAidotSlackEvent(input: unknown, connection: AidotSlackConnection): AidotSlackEvent | null {
  const envelope = slackRecord(input);
  const team = slackId(envelope.team_id ?? slackRecord(envelope.team).id, "workspace ID");
  if (team !== slackId(connection.teamId, "연결 workspace ID")) throw new Error("다른 Slack workspace의 메시지입니다.");
  const selfUser = slackId(connection.botUserId, "봇 사용자 ID"), selfBot = slackId(connection.botId, "봇 ID");
  if (envelope.type === "block_actions") {
    const user = slackId(slackRecord(envelope.user).id, "참가자 ID");
    if (user === selfUser) return null;
    if (!Array.isArray(envelope.actions) || envelope.actions.length !== 1) throw new Error("Slack 버튼 응답은 한 개씩 처리합니다.");
    const action = slackRecord(envelope.actions[0]);
    if (typeof action.action_id !== "string" || !/^cga:\d+$/.test(action.action_id)) return null;
    let value: Record<string, unknown>;
    try { value = slackRecord(JSON.parse(String(action.value))); } catch { throw new Error("Slack 버튼 응답 형식이 올바르지 않습니다."); }
    if (value.cgaSlackActionVersion !== "1.0") throw new Error("지원하지 않는 Slack 버튼 응답입니다.");
    const sourceMessageId = slackText(value.messageId, 120, "원본 메시지 ID");
    const sourceTalkNodeId = slackText(value.sourceTalkNodeId, 120, "원본 Talk ID");
    const buttonValue = slackText(slackRecord(value.response).buttonValue, 2000, "선택값");
    const channel = slackId(slackRecord(envelope.channel).id, "채널 ID");
    const container = slackRecord(envelope.container), message = slackRecord(envelope.message);
    const scope = slackScope(team, channel, container.thread_ts ?? message.thread_ts, user, "user");
    const messageTs = slackTimestamp(container.message_ts ?? message.ts), actionTs = slackTimestamp(action.action_ts);
    return { ...scope, participantKind: "user", roomMode: channel.startsWith("D") ? "private" : "shared", addressed: true,
      eventId: `slack-action:${messageTs}:${actionTs}`, sourceMessageId, sourceTalkNodeId,
      message: JSON.stringify({ webchatRichFormVersion: "1.0", sourceTalkNodeId, sourceMessageId, response: { buttonValue } }) };
  }
  if (envelope.type !== "event_callback") return null;
  const event = slackRecord(envelope.event);
  if (event.type !== "message" && event.type !== "app_mention") return null;
  if (event.subtype && !["bot_message", "thread_broadcast"].includes(String(event.subtype))) return null;
  if (event.user === selfUser || event.bot_id === selfBot) return null;
  const kind: AidotClientParticipantKind = event.bot_id ? "bot" : "user";
  const actor = slackId(kind === "bot" ? event.bot_id : event.user, "참가자 ID");
  const channel = slackId(event.channel, "채널 ID"), ts = slackTimestamp(event.ts);
  const scope = slackScope(team, channel, event.thread_ts, actor, kind);
  const mode = event.channel_type === "im" || channel.startsWith("D") ? "private" : "shared";
  const raw = slackText(event.text, 4000, "질문");
  const mention = new RegExp(`<@${selfUser}(?:\\|[^>]+)?>`, "g");
  const addressed = mode === "private" || raw.match(mention) !== null;
  const message = slackText(addressed ? raw.replace(mention, "").trim() : raw, 4000, "질문");
  return { ...scope, participantKind: kind, roomMode: mode, eventId: `slack-message:${ts}`, message, addressed };
}

// 게시용 packet만 반환한다. Slack API 호출·자격증명 등록·자동 재시도는 하지 않는다.
export function buildAidotSlackReplyPayload(message: AidotChannelStoredMessage, destination: { channelId: string; threadTs?: string | null }): AidotSlackReplyPayload {
  const channel = slackId(destination.channelId, "응답 채널 ID");
  const text = slackText(message.text, 40000, "응답 텍스트");
  const escaped = slackText(text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"), 40000, "응답 텍스트");
  const result: AidotSlackReplyPayload = { channel, text: escaped, mrkdwn: false, parse: "none", unfurl_links: false, unfurl_media: false };
  if (destination.threadTs !== undefined && destination.threadTs !== null) result.thread_ts = slackTimestamp(destination.threadTs);
  const type = message.messageType || "text";
  if (type === "text") return result;
  if (!["button", "card", "table", "link-button", "carousel"].includes(type)) throw new Error(`Slack에서 지원하지 않는 Talk 표현입니다: ${type}`);
  const payload = slackRecord(message.payload), blocks: Record<string, unknown>[] = [];
  const plain = (value: unknown, maximum: number, field: string) => ({ type: "plain_text", text: slackText(value, maximum, field), emoji: false });
  const section = (value: unknown) => { if (typeof value === "string" && value) blocks.push({ type: "section", text: plain(value, 3000, "본문") }); };
  const heading = (value: unknown) => { if (typeof value === "string" && value) blocks.push({ type: "header", text: plain(value, 150, "제목") }); };
  const url = (value: unknown) => {
    const source = slackText(value, 3000, "URL"); let parsed: URL;
    try { parsed = new URL(source); } catch { throw new Error("Slack 링크 URL이 올바르지 않습니다."); }
    if (!["https:", "http:"].includes(parsed.protocol)) throw new Error("Slack 링크는 HTTP/HTTPS URL을 사용합니다.");
    return source;
  };
  const image = (value: unknown, title: unknown) => { if (typeof value === "string" && value) blocks.push({ type: "image", image_url: url(value), alt_text: slackText(title || "이미지", 2000, "이미지 설명") }); };
  let actionIndex = 0;
  const choice = (label: unknown, selected: unknown) => {
    const value = JSON.stringify({ cgaSlackActionVersion: "1.0", messageId: slackText(message.id, 120, "원본 메시지 ID"),
      sourceTalkNodeId: slackText(payload.sourceTalkNodeId, 120, "원본 Talk ID"), response: { buttonValue: String(selected ?? "") } });
    return { type: "button", action_id: `cga:${actionIndex++}`, text: plain(label, 75, "버튼 이름"), value: slackText(value, 2000, "버튼 값") };
  };
  const link = (label: unknown, target: unknown) => ({ type: "button", action_id: `cga-link:${actionIndex++}`, text: plain(label, 75, "버튼 이름"), url: url(target) });
  const actions = (items: Record<string, unknown>[]) => {
    for (let offset = 0; offset < items.length; offset += 25) blocks.push({ type: "actions", block_id: `${message.id}:${blocks.length}`, elements: items.slice(offset, offset + 25) });
  };
  section(text);
  if (type === "button" || type === "link-button") {
    if (!Array.isArray(payload.options) || !payload.options.length) throw new Error("Slack 버튼 항목이 없습니다.");
    actions(payload.options.map((item) => {
      if (typeof item === "string" && type === "button") return choice(item, item);
      const option = slackRecord(item), label = option.label ?? option.text ?? option.value;
      if (type === "link-button" || option.url) return link(label, option.url);
      return choice(label, option.value ?? option.messageText ?? label);
    }));
  } else if (type === "card") {
    const card = slackRecord(payload.card); heading(card.title); section(card.description); image(card.imageUrl, card.title);
  } else if (type === "table") {
    const columns = payload.columns, rows = payload.rows;
    if (!Array.isArray(columns) || !columns.length || columns.length > 20 || columns.some(c => typeof c !== "string" || !c) || !Array.isArray(rows) || !rows.length || rows.length > 99) throw new Error("Slack 표는 머리글 포함 100행·20열 이내여야 합니다.");
    const cell = (value: unknown) => ({ type: "raw_text", text: value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value) });
    const rowValues = rows.map(row => { if (row === null || typeof row !== "object" || Array.isArray(row)) throw new Error("Slack 표 행이 올바르지 않습니다."); return slackRecord(row); });
    blocks.push({ type: "table", rows: [columns.map(cell), ...rowValues.map(row => columns.map(column => cell(row[String(column)])))] });
    if (payload.selectable !== false) actions(rowValues.map(row => {
      const first = row[String(columns[0])], selected = payload.keyColumn && row[String(payload.keyColumn)] !== undefined ? row[String(payload.keyColumn)] : first;
      return choice(String(first ?? selected ?? "선택"), selected);
    }));
  } else {
    const card = slackRecord(payload.carousel); heading(card.title); image(card.imageUrl, card.itemTitle ?? card.title); heading(card.itemTitle); section(card.itemContents);
    const items: Record<string, unknown>[] = [];
    if (String(card.buttonType).toLowerCase() === "button") items.push(choice(card.itemButtonLabel, card.itemButtonValue));
    if (card.bottomButtonLabel || card.bottomButtonValue) items.push(choice(card.bottomButtonLabel, card.bottomButtonValue));
    actions(items);
  }
  if (blocks.length > 50) throw new Error("Slack 메시지는 50개 block 이내여야 합니다.");
  result.blocks = blocks;
  return result;
}


export type AidotSlackMembership = AidotClientMembership & Pick<AidotSlackEvent, "nativeRoomKey" | "nativeParticipantKey" | "channelId" | "threadTs" | "roomMode">;

function slackMembership(member: AidotClientMembership, event: AidotSlackEvent): AidotSlackMembership {
  if (member.participantKind !== event.participantKind) throw new Error("Slack 질문자 종류와 서버 발급 참가자가 다릅니다.");
  return { ...member, nativeRoomKey: event.nativeRoomKey, nativeParticipantKey: event.nativeParticipantKey,
    channelId: event.channelId, threadTs: event.threadTs, roomMode: event.roomMode };
}

export async function createAidotSlackRoom(serverUrl: string, botId: string, input: unknown, connection: AidotSlackConnection, signal?: AbortSignal) {
  const event = normalizeAidotSlackEvent(input, connection);
  if (event === null) return null;
  const member = await createAidotClientRoom(serverUrl, "slack", botId, event.roomMode, event.participantKind, event.nativeParticipantKey, signal);
  return slackMembership(member, event);
}

export async function joinAidotSlackRoom(serverUrl: string, room: Pick<AidotSlackMembership, "roomId" | "nativeRoomKey">, input: unknown, connection: AidotSlackConnection, signal?: AbortSignal) {
  const event = normalizeAidotSlackEvent(input, connection);
  if (event === null) return null;
  if (event.nativeRoomKey !== room.nativeRoomKey) throw new Error("다른 Slack 방/스레드에는 이 방 정보로 입장할 수 없습니다.");
  const member = await joinAidotClientRoom(serverUrl, "slack", room.roomId, event.participantKind, event.nativeParticipantKey, signal);
  return slackMembership(member, event);
}

export async function sendAidotSlackEvent(serverUrl: string, member: AidotSlackMembership, input: unknown, connection: AidotSlackConnection, signal?: AbortSignal): Promise<{ response: AidotSharedRoomMessageResponse | null; replies: AidotSlackReplyPayload[] }> {
  const event = normalizeAidotSlackEvent(input, connection);
  if (event === null) return { response: null, replies: [] };
  if (event.nativeRoomKey !== member.nativeRoomKey || event.nativeParticipantKey !== member.nativeParticipantKey || event.participantKind !== member.participantKind) throw new Error("Slack 방/질문자에 해당하는 서버 발급 증명이 필요합니다.");
  if (event.channelId !== member.channelId || event.threadTs !== member.threadTs || event.roomMode !== member.roomMode) throw new Error("Slack 응답 대상이 저장된 방과 다릅니다.");
  // 서버가 방 잠금 안에서 완료 이벤트를 먼저 복구하고 새 카드 응답만 검증한다.
  const response = await sendAidotClientMessage(serverUrl, "slack", member, event.message, event.addressed, event.eventId, signal);
  try {
    const messages = response.botMessages ?? (response.botMessage ? [response.botMessage] : []);
    return { response, replies: messages.map(message => buildAidotSlackReplyPayload(message, { channelId: event.channelId, threadTs: event.threadTs })) };
  } catch (cause) {
    // 생성은 완료되었으며 표현 실패를 새 유료 질문으로 바꾸지 않는다.
    throw Object.assign(new Error("CGA 답변은 수신했지만 Slack 표현을 변환하지 못했습니다."), { cause, cgaResponse: response });
  }
}


export type AidotDiscordConnection = { applicationId: string; botUserId: string; guildId: string | null; channelId: string; channelType: number };
export type AidotDiscordEvent = {
  nativeRoomKey: string; nativeParticipantKey: string; participantKind: AidotClientParticipantKind;
  channelId: string; roomMode: "private" | "shared"; eventId: string; message: string; addressed: boolean;
  sourceMessageId?: string; choiceIndex?: number;
};
export type AidotDiscordReplyPayload = { channel_id: string; content: string; allowed_mentions: { parse: string[]; replied_user: false }; embeds?: Record<string, unknown>[]; components?: Record<string, unknown>[] };
function discordRecord(value: unknown): Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function discordId(value: unknown) {
  if (typeof value !== "string" || !/^[1-9]\d{0,19}$/.test(value) || (value.length === 20 && value > "18446744073709551615")) throw new Error("Discord ID는 유효한 문자열 snowflake여야 합니다.");
  return value;
}
function discordText(value: unknown, maximum: number, field: string) {
  if (typeof value !== "string" || !value.trim() || value.length > maximum) throw new Error(`Discord ${field}는 1~${maximum}자여야 합니다.`);
  return value;
}
function discordUrl(value: unknown) {
  const source = discordText(value, 512, "URL"); let parsed: URL;
  try { parsed = new URL(source); } catch { throw new Error("Discord URL이 올바르지 않습니다."); }
  if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("Discord 링크는 HTTP/HTTPS URL을 사용합니다.");
  return source;
}

// 기존 client가 설치·Gateway/서명·채널 접근을 검증한 다음 호출한다. native ID는 CGA 소유권 증명이 아니다.
export function normalizeAidotDiscordEvent(input: unknown, connection: AidotDiscordConnection): AidotDiscordEvent | null {
  const envelope = discordRecord(input);
  const interaction = envelope.type === 3 || (envelope.op === 0 && envelope.t === "INTERACTION_CREATE");
  if (!interaction && !(envelope.op === 0 && envelope.t === "MESSAGE_CREATE")) return null;
  const data = envelope.type === 3 ? envelope : discordRecord(envelope.d);
  if (interaction && data.type !== 3) return null;
  const app = discordId(connection.applicationId), self = discordId(connection.botUserId), channel = discordId(connection.channelId);
  const dm = connection.channelType === 1, noGuild = dm || connection.channelType === 3;
  if (![0, 1, 3, 5, 10, 11, 12].includes(connection.channelType)) throw new Error("Discord 채널 종류를 확인할 수 없습니다.");
  const guild = connection.guildId === null ? null : discordId(connection.guildId);
  if (noGuild !== (guild === null)) throw new Error("Discord 채널 종류와 서버 범위가 다릅니다.");
  if ((data.guild_id ?? null) !== guild || discordId(data.channel_id ?? discordRecord(data.channel).id) !== channel) throw new Error("다른 Discord 서버/채널의 입력입니다.");
  const author = interaction ? discordRecord(discordRecord(data.member).user ?? data.user) : discordRecord(data.author);
  if (author.id === self || (!interaction && data.application_id === app)) return null;
  const actor = discordId(author.id), kind: AidotClientParticipantKind = author.bot === true ? "bot" : "user";
  const common = { nativeRoomKey: "discord:" + JSON.stringify([guild, channel]), nativeParticipantKey: `discord:${kind}:${actor}`, participantKind: kind, channelId: channel, roomMode: dm ? "private" as const : "shared" as const };
  if (interaction) {
    if (discordId(data.application_id) !== app || discordRecord(discordRecord(data.message).author).id !== self) throw new Error("연결된 Discord 봇의 버튼만 처리합니다.");
    const selected = discordRecord(data.data);
    if (selected.component_type !== 2 || typeof selected.custom_id !== "string" || !selected.custom_id.startsWith("cga:")) return null;
    const match = /^cga:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(\d{1,2})$/.exec(selected.custom_id);
    if (!match || Number(match[2]) > 24) throw new Error("Discord 버튼 식별자가 올바르지 않습니다.");
    return { ...common, eventId: `discord-interaction:${discordId(data.id)}`, message: "", addressed: true, sourceMessageId: match[1], choiceIndex: Number(match[2]) };
  }
  if (data.type !== undefined && data.type !== 0 && data.type !== 19) return null;
  const raw = discordText(data.content, 4000, "질문"), mention = new RegExp(`<@!?${self}>`, "g");
  const addressed = dm || (Array.isArray(data.mentions) && data.mentions.some(item => discordRecord(item).id === self) && raw.match(mention) !== null);
  const message = addressed ? discordText(raw.replace(mention, "").trim(), 4000, "질문") : raw;
  return { ...common, eventId: `discord-message:${discordId(data.id)}`, message, addressed };
}

function discordSelectionMessage(source: AidotChannelStoredMessage, value: string) {
  const wire = JSON.stringify({ webchatRichFormVersion: "1.0", sourceMessageId: source.id, sourceTalkNodeId: discordText(source.payload?.sourceTalkNodeId, 120, "원본 Talk ID"), response: { buttonValue: value } });
  if (Array.from(wire).length > 4000) throw new Error("Discord 선택 응답이 기존 메시지 한도를 넘습니다.");
  return wire;
}
function discordReply(message: AidotChannelStoredMessage, destination: { channelId: string }) {
  const packet: AidotDiscordReplyPayload = { channel_id: discordId(destination.channelId), content: discordText(message.text, 2000, "본문"), allowed_mentions: { parse: [], replied_user: false } };
  const type = message.messageType || "text", choices: string[] = [], buttons: Record<string, unknown>[] = [], embeds: Record<string, unknown>[] = [];
  if (type === "text") return { packet, choices };
  if (!["button", "card", "table", "link-button", "carousel"].includes(type)) throw new Error(`Discord에서 지원하지 않는 Talk 표현입니다: ${type}`);
  const payload = discordRecord(message.payload);
  const choice = (label: unknown, value: unknown) => {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(message.id)) throw new Error("Discord 버튼 원본 CGA 메시지 ID가 올바르지 않습니다.");
    const index = choices.length, selected = String(value ?? "");
    if (selected.length > 4000) throw new Error("Discord 선택값이 기존 메시지 한도를 넘습니다.");
    discordSelectionMessage(message, selected); choices.push(selected);
    buttons.push({ type: 2, style: 1, label: discordText(label, 80, "버튼 이름"), custom_id: `cga:${message.id}:${index}` });
  };
  const link = (label: unknown, url: unknown) => buttons.push({ type: 2, style: 5, label: discordText(label, 80, "버튼 이름"), url: discordUrl(url) });
  const cardEmbed = (card: Record<string, unknown>, title: unknown, description: unknown) => {
    const embed: Record<string, unknown> = {};
    if (title) embed.title = discordText(title, 256, "제목");
    if (description) embed.description = discordText(description, 4096, "설명");
    if (card.imageUrl) embed.image = { url: discordUrl(card.imageUrl) };
    return embed;
  };
  if (type === "button" || type === "link-button") {
    if (!Array.isArray(payload.options)) throw new Error("Discord 버튼 항목이 올바르지 않습니다.");
    for (const item of payload.options) {
      if (typeof item === "string" && type === "button") choice(item, item);
      else { const option = discordRecord(item), label = option.label ?? option.text ?? option.value;
        if (type === "link-button" || option.url) link(label, option.url); else choice(label, option.value ?? option.messageText ?? label); }
    }
  } else if (type === "card") {
    const card = discordRecord(payload.card), embed = cardEmbed(card, card.title, card.description);
    if (Object.keys(embed).length) embeds.push(embed);
  } else if (type === "table") {
    const columns = payload.columns, rows = payload.rows;
    if (!Array.isArray(columns) || columns.some(column => typeof column !== "string" || !column) || !Array.isArray(rows) || rows.length > 25) throw new Error("Discord 표 구조와 25행 한도를 확인합니다.");
    if (!rows.length) return { packet, choices };
    if (!columns.length) throw new Error("Discord 표의 열이 없습니다.");
    const fields = rows.map((item, index) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error("Discord 표 행이 올바르지 않습니다.");
      const row = discordRecord(item), first = row[String(columns[0])], key = typeof payload.keyColumn === "string" && row[payload.keyColumn] !== undefined ? row[payload.keyColumn] : first;
      const stringify = (value: unknown) => value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
      const label = stringify(first) || stringify(key) || `행 ${index + 1}`;
      if (payload.selectable !== false) choice(label, stringify(key));
      return { name: discordText(label, 256, "표 행 이름"), value: discordText(columns.map(column => `${column}: ${stringify(row[String(column)])}`).join("\n"), 1024, "표 행"), inline: false };
    });
    embeds.push({ fields });
  } else {
    const card = discordRecord(payload.carousel), embed = cardEmbed(card, card.title, card.itemContents);
    if (card.itemTitle) embed.fields = [{ name: discordText(card.itemTitle, 256, "항목 제목"), value: "\u200b", inline: false }];
    if (Object.keys(embed).length) embeds.push(embed);
    if (String(card.buttonType).toLowerCase() === "button") choice(card.itemButtonLabel, card.itemButtonValue);
    if (card.bottomButtonLabel || card.bottomButtonValue) choice(card.bottomButtonLabel, card.bottomButtonValue);
  }
  if (buttons.length > 25) throw new Error("Discord 버튼은 5행·25개 이내여야 합니다.");
  if (buttons.length) packet.components = Array.from({ length: Math.ceil(buttons.length / 5) }, (_, index) => ({ type: 1, components: buttons.slice(index * 5, index * 5 + 5) }));
  if (embeds.some(embed => !Object.keys(embed).length)) throw new Error("Discord 카드 내용이 비어 있습니다.");
  let total = 0;
  for (const embed of embeds) {
    for (const value of [embed.title, embed.description]) if (typeof value === "string") total += value.length;
    if (Array.isArray(embed.fields)) for (const field of embed.fields) { const value = discordRecord(field); total += String(value.name ?? "").length + String(value.value ?? "").length; }
  }
  if (total > 6000) throw new Error("Discord embed 합계는 6000자 이내여야 합니다.");
  if (embeds.length) packet.embeds = embeds;
  return { packet, choices };
}
export function buildAidotDiscordReplyPayload(message: AidotChannelStoredMessage, destination: { channelId: string }) { return discordReply(message, destination).packet; }
export type AidotDiscordMembership = AidotClientMembership & Pick<AidotDiscordEvent, "nativeRoomKey" | "nativeParticipantKey" | "channelId" | "roomMode">;
function discordMembership(member: AidotClientMembership, event: AidotDiscordEvent): AidotDiscordMembership {
  if (member.participantKind !== event.participantKind) throw new Error("Discord 질문자 종류와 서버 발급 참가자가 다릅니다.");
  return { ...member, nativeRoomKey: event.nativeRoomKey, nativeParticipantKey: event.nativeParticipantKey, channelId: event.channelId, roomMode: event.roomMode };
}
export async function createAidotDiscordRoom(serverUrl: string, botId: string, input: unknown, connection: AidotDiscordConnection, signal?: AbortSignal) {
  const event = normalizeAidotDiscordEvent(input, connection); if (event === null) return null;
  if (event.sourceMessageId) throw new Error("Discord 버튼은 기존 대화방에서만 이어갈 수 있습니다.");
  return discordMembership(await createAidotClientRoom(serverUrl, "discord", botId, event.roomMode, event.participantKind, event.nativeParticipantKey, signal), event);
}
export async function joinAidotDiscordRoom(serverUrl: string, room: Pick<AidotDiscordMembership, "roomId" | "nativeRoomKey">, input: unknown, connection: AidotDiscordConnection, signal?: AbortSignal) {
  const event = normalizeAidotDiscordEvent(input, connection); if (event === null) return null;
  if (event.nativeRoomKey !== room.nativeRoomKey) throw new Error("다른 Discord 채널/스레드에는 이 방 정보로 입장할 수 없습니다.");
  return discordMembership(await joinAidotClientRoom(serverUrl, "discord", room.roomId, event.participantKind, event.nativeParticipantKey, signal), event);
}
export async function sendAidotDiscordEvent(serverUrl: string, member: AidotDiscordMembership, input: unknown, connection: AidotDiscordConnection, signal?: AbortSignal): Promise<{ response: AidotSharedRoomMessageResponse | null; replies: AidotDiscordReplyPayload[] }> {
  const event = normalizeAidotDiscordEvent(input, connection); if (event === null) return { response: null, replies: [] };
  if (event.nativeRoomKey !== member.nativeRoomKey || event.nativeParticipantKey !== member.nativeParticipantKey || event.participantKind !== member.participantKind || event.channelId !== member.channelId || event.roomMode !== member.roomMode) throw new Error("Discord 방/질문자에 해당하는 서버 발급 증명이 필요합니다.");
  let message = event.message;
  if (event.sourceMessageId) {
    // 완료 이벤트의 복구를 막지 않도록 GET에서는 최신 여부/차례를 검사하지 않는다.
    const history = await fetchAidotClientRoom(serverUrl, "discord", member, signal);
    const source = history.messages?.find(item => item.id === event.sourceMessageId && item.participantKind === "bot");
    if (!source) throw new Error("Discord 버튼 원본 기록을 확인할 수 없어 재실행하지 않습니다.");
    const value = discordReply(source, { channelId: event.channelId }).choices[event.choiceIndex ?? -1];
    if (value === undefined || !source.payload?.sourceTalkNodeId) throw new Error("Discord 버튼 선택 출처가 올바르지 않습니다.");
    message = discordSelectionMessage(source, value);
  }
  const response = await sendAidotClientMessage(serverUrl, "discord", member, message, event.addressed, event.eventId, signal);
  try { return { response, replies: (response.botMessages ?? (response.botMessage ? [response.botMessage] : [])).map(item => buildAidotDiscordReplyPayload(item, { channelId: event.channelId })) }; }
  catch (cause) { throw Object.assign(new Error("CGA 답변은 수신했지만 Discord 표현을 변환하지 못했습니다."), { cause, cgaResponse: response }); }
}


export type AidotTelegramConnection = { botUserId: string; botUsername: string; chatId: string; chatType: "private" | "group" | "supergroup" };
export type AidotTelegramEvent = {
  nativeRoomKey: string; nativeParticipantKey: string; participantKind: AidotClientParticipantKind;
  chatId: string; threadId: number | null; roomMode: "private" | "shared"; eventId: string; message: string; addressed: boolean;
  sourceMessageId?: string; choiceIndex?: number;
};
export type AidotTelegramReplyPayload = { method: "sendMessage" | "sendPhoto"; parameters: {
  chat_id: string; message_thread_id?: number; text?: string; photo?: string; caption?: string; parse_mode?: "HTML";
  reply_markup?: { inline_keyboard: Record<string, unknown>[][] };
} };
function telegramRecord(value: unknown): Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function telegramId(value: unknown, positive = false) {
  const source = typeof value === "number" && Number.isSafeInteger(value) ? String(value) : value;
  if (typeof source !== "string" || !/^-?[1-9]\d*$/.test(source) || !Number.isSafeInteger(Number(source)) || (positive && Number(source) < 1)) throw new Error("Telegram ID를 정확히 확인할 수 없습니다.");
  return source;
}
function telegramInteger(value: unknown, minimum: number, field: string) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) throw new Error(`Telegram ${field}가 올바르지 않습니다.`);
  return value;
}
function telegramText(value: unknown, maximum: number, field: string) {
  if (typeof value !== "string" || !value.trim() || Array.from(value).length > maximum) throw new Error(`Telegram ${field}는 1~${maximum}자여야 합니다.`);
  return value;
}
function telegramUrl(value: unknown) {
  if (typeof value !== "string" || !value.trim()) throw new Error("Telegram URL이 올바르지 않습니다.");
  let url: URL; try { url = new URL(value); } catch { throw new Error("Telegram URL이 올바르지 않습니다."); }
  if (!["http:", "https:"].includes(url.protocol)) throw new Error("Telegram 이미지는 HTTP/HTTPS URL을 사용합니다.");
  return value;
}
// 기존 client가 Bot API 연결/검증을 마친 입력이다. anonymous sender_chat을 개인 actor로 합치지 않는다.
export function normalizeAidotTelegramEvent(input: unknown, connection: AidotTelegramConnection): AidotTelegramEvent | null {
  const update = telegramRecord(input), query = update.callback_query === undefined ? null : telegramRecord(update.callback_query);
  if (query === null && update.message === undefined) return null;
  if (query !== null && update.message !== undefined) throw new Error("Telegram update 종류가 중복되었습니다.");
  const message = telegramRecord(query ? query.message : update.message), chat = telegramRecord(message.chat);
  const self = telegramId(connection.botUserId, true), chatId = telegramId(chat.id), expectedChat = telegramId(connection.chatId);
  if (!["private", "group", "supergroup"].includes(String(chat.type)) || chat.type !== connection.chatType || chatId !== expectedChat) throw new Error("연결된 Telegram 개인/그룹 채팅이 아닙니다.");
  if (message.sender_chat !== undefined) throw new Error("Telegram 익명/채널 발신자를 개인 질문자로 합칠 수 없습니다.");
  const from = telegramRecord(query ? query.from : message.from);
  if (String(from.id) === self) return null;
  if (!query && typeof message.text !== "string") {
    if (["photo", "document", "audio", "voice", "video", "caption"].some(key => key in message)) throw new Error("Telegram 미디어 질문은 이번 연결에서 지원하지 않습니다.");
    return null;
  }
  const actor = telegramId(from.id, true), kind: AidotClientParticipantKind = from.is_bot === true ? "bot" : "user";
  const threadId = message.message_thread_id === undefined ? null : telegramInteger(message.message_thread_id, 1, "주제 ID");
  if (message.is_topic_message === true && threadId === null) throw new Error("Telegram 주제를 기본 방으로 전환할 수 없습니다.");
  const common = { nativeRoomKey: "telegram:" + JSON.stringify([self, chat.type, chatId, threadId]), nativeParticipantKey: `telegram:${kind}:${actor}`, participantKind: kind, chatId, threadId, roomMode: chat.type === "private" ? "private" as const : "shared" as const,
    eventId: `telegram-update:${telegramInteger(update.update_id, 0, "update ID")}` };
  telegramInteger(message.message_id, 1, "메시지 ID");
  if (query) {
    telegramText(query.id, 200, "callback ID");
    if (telegramInteger(message.date, 1, "원본 메시지 시각") < 1 || telegramId(telegramRecord(message.from).id, true) !== self) throw new Error("Telegram 버튼 원본을 확인할 수 없습니다.");
    if (typeof query.data !== "string" || !query.data.startsWith("cga:")) return null;
    if (new TextEncoder().encode(query.data).length > 64) throw new Error("Telegram callback_data는 64바이트 이내여야 합니다.");
    const match = /^cga:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(\d+)$/.exec(query.data);
    if (!match || !Number.isSafeInteger(Number(match[2]))) throw new Error("Telegram 버튼 식별자가 올바르지 않습니다.");
    return { ...common, message: "", addressed: true, sourceMessageId: match[1], choiceIndex: Number(match[2]) };
  }
  const raw = telegramText(message.text, 4096, "질문"), username = connection.botUsername;
  if (typeof username !== "string" || !/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(username)) throw new Error("Telegram 봇 username이 올바르지 않습니다.");
  if (message.entities !== undefined && !Array.isArray(message.entities)) throw new Error("Telegram entity 목록이 올바르지 않습니다.");
  const removals: { start: number; end: number }[] = [];
  let addressedCommand = "";
  for (const item of (Array.isArray(message.entities) ? message.entities : [])) {
    const entity = telegramRecord(item), start = telegramInteger(entity.offset, 0, "entity offset"), length = telegramInteger(entity.length, 1, "entity length"), end = start + length;
    if (end > raw.length) throw new Error("Telegram entity 범위가 올바르지 않습니다.");
    const selected = raw.slice(start, end);
    const targetCommand = entity.type === "bot_command" && /^\/[A-Za-z0-9_]+@[A-Za-z0-9_]+$/.test(selected) && selected.split("@")[1].toLowerCase() === username.toLowerCase();
    if (targetCommand && !addressedCommand) addressedCommand = selected.split("@")[0];
    if ((entity.type === "mention" && selected.toLowerCase() === `@${username}`.toLowerCase())
      || targetCommand
      || (entity.type === "text_mention" && telegramRecord(entity.user).id !== undefined && telegramId(telegramRecord(entity.user).id, true) === self)) removals.push({ start, end });
  }
  const addressed = common.roomMode === "private" || removals.length > 0;
  let text = raw;
  if (addressed) for (const range of removals.sort((a, b) => b.start - a.start)) text = text.slice(0, range.start) + text.slice(range.end);
  return { ...common, addressed, message: telegramText(addressed ? text.trim() || addressedCommand : raw, 4000, "CGA 질문") };
}
function telegramSelectionMessage(source: AidotChannelStoredMessage, value: string) {
  const wire = JSON.stringify({ webchatRichFormVersion: "1.0", sourceMessageId: source.id, sourceTalkNodeId: telegramText(source.payload?.sourceTalkNodeId, 120, "원본 Talk ID"), response: { buttonValue: value } });
  if (Array.from(wire).length > 4000) throw new Error("Telegram 선택 응답이 기존 메시지 한도를 넘습니다.");
  return wire;
}
function telegramReply(message: AidotChannelStoredMessage, destination: { chatId: string; threadId?: number | null }) {
  const body = telegramText(message.text, 4096, "본문"), parameters: AidotTelegramReplyPayload["parameters"] = { chat_id: telegramId(destination.chatId), text: body };
  if (destination.threadId !== undefined && destination.threadId !== null) parameters.message_thread_id = telegramInteger(destination.threadId, 1, "응답 주제 ID");
  const packet: AidotTelegramReplyPayload = { method: "sendMessage", parameters }, type = message.messageType || "text", choices: string[] = [], buttons: Record<string, unknown>[] = [];
  if (type === "text") return { packet, choices };
  if (!["button", "card", "table", "link-button", "carousel"].includes(type)) throw new Error(`Telegram에서 지원하지 않는 Talk 표현입니다: ${type}`);
  const payload = telegramRecord(message.payload), escape = (value: string) => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const scalar = (value: unknown): string => value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
  const label = (value: unknown) => { if (typeof value !== "string" || !value.trim()) throw new Error("Telegram 버튼 이름이 없습니다."); return value; };
  const choice = (name: unknown, value: unknown) => {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(message.id)) throw new Error("Telegram 버튼 원본 CGA 메시지 ID가 올바르지 않습니다.");
    const selected = scalar(value); telegramSelectionMessage(message, selected);
    const data = `cga:${message.id}:${choices.length}`; if (new TextEncoder().encode(data).length > 64) throw new Error("Telegram callback_data가 너무 깁니다.");
    choices.push(selected); buttons.push({ text: label(name), callback_data: data });
  };
  const link = (name: unknown, value: unknown) => buttons.push({ text: label(name), url: telegramUrl(value) });
  if (type === "button" || type === "link-button") {
    if (!Array.isArray(payload.options)) throw new Error("Telegram 버튼 항목이 올바르지 않습니다.");
    for (const item of payload.options) {
      if (typeof item === "string" && type === "button") choice(item, item);
      else { const option = telegramRecord(item), name = option.label ?? option.text ?? option.value;
        if (type === "link-button" || option.url) link(name, option.url); else choice(name, option.value ?? option.messageText ?? name); }
    }
  } else if (type === "table") {
    const columns = payload.columns, rows = payload.rows;
    if (!Array.isArray(columns) || columns.some(column => typeof column !== "string" || !column) || !Array.isArray(rows)) throw new Error("Telegram 표 구조가 올바르지 않습니다.");
    if (!rows.length) return { packet, choices }; if (!columns.length) throw new Error("Telegram 표의 열이 없습니다.");
    const lines = rows.map(item => {
      if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error("Telegram 표 행이 올바르지 않습니다.");
      const row = telegramRecord(item), first = row[String(columns[0])], key = typeof payload.keyColumn === "string" && row[payload.keyColumn] !== undefined ? row[payload.keyColumn] : first;
      if (payload.selectable !== false) choice(scalar(first) || scalar(key) || "선택", key);
      return columns.map(column => scalar(row[String(column)])).join(" | ");
    });
    const table = [columns.join(" | "), ...lines].join("\n"); telegramText(body + "\n\n" + table, 4096, "표 본문");
    parameters.text = escape(body) + "\n\n<pre>" + escape(table) + "</pre>"; parameters.parse_mode = "HTML";
  } else {
    const raw = type === "card" ? payload.card : payload.carousel;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("Telegram 카드 구조가 올바르지 않습니다.");
    const card = telegramRecord(raw), parts = type === "card" ? [body, card.title, card.description] : [body, card.title, card.itemTitle, card.itemContents];
    const texts = parts.filter(part => part !== undefined && part !== null && part !== "").map(part => { if (typeof part !== "string") throw new Error("Telegram 카드 내용이 올바르지 않습니다."); return part; });
    const plain = texts.join("\n"); telegramText(plain, card.imageUrl ? 1024 : 4096, card.imageUrl ? "사진 설명" : "카드 본문");
    parameters.parse_mode = "HTML";
    if (card.imageUrl) { packet.method = "sendPhoto"; delete parameters.text; parameters.photo = telegramUrl(card.imageUrl); parameters.caption = texts.map(escape).join("\n"); }
    else parameters.text = texts.map(escape).join("\n");
    if (type === "carousel") {
      if (String(card.buttonType).toLowerCase() === "button") choice(card.itemButtonLabel, card.itemButtonValue);
      if (card.bottomButtonLabel || card.bottomButtonValue) choice(card.bottomButtonLabel, card.bottomButtonValue);
    }
  }
  if (buttons.length) parameters.reply_markup = { inline_keyboard: buttons.map(button => [button]) };
  return { packet, choices };
}
export function buildAidotTelegramReplyPayload(message: AidotChannelStoredMessage, destination: { chatId: string; threadId?: number | null }) { return telegramReply(message, destination).packet; }
export type AidotTelegramMembership = AidotClientMembership & Pick<AidotTelegramEvent, "nativeRoomKey" | "nativeParticipantKey" | "chatId" | "threadId" | "roomMode">;
function telegramMembership(member: AidotClientMembership, event: AidotTelegramEvent): AidotTelegramMembership {
  if (member.participantKind !== event.participantKind) throw new Error("Telegram 질문자 종류와 서버 발급 참가자가 다릅니다.");
  return { ...member, nativeRoomKey: event.nativeRoomKey, nativeParticipantKey: event.nativeParticipantKey, chatId: event.chatId, threadId: event.threadId, roomMode: event.roomMode };
}
export async function createAidotTelegramRoom(serverUrl: string, botId: string, input: unknown, connection: AidotTelegramConnection, signal?: AbortSignal) {
  const event = normalizeAidotTelegramEvent(input, connection); if (event === null) return null;
  if (event.sourceMessageId) throw new Error("Telegram 버튼은 기존 대화방에서만 이어갈 수 있습니다.");
  return telegramMembership(await createAidotClientRoom(serverUrl, "telegram", botId, event.roomMode, event.participantKind, event.nativeParticipantKey, signal), event);
}
export async function joinAidotTelegramRoom(serverUrl: string, room: Pick<AidotTelegramMembership, "roomId" | "nativeRoomKey">, input: unknown, connection: AidotTelegramConnection, signal?: AbortSignal) {
  const event = normalizeAidotTelegramEvent(input, connection); if (event === null) return null;
  if (event.nativeRoomKey !== room.nativeRoomKey) throw new Error("다른 Telegram 채팅/주제에는 이 방 정보로 입장할 수 없습니다.");
  return telegramMembership(await joinAidotClientRoom(serverUrl, "telegram", room.roomId, event.participantKind, event.nativeParticipantKey, signal), event);
}
export async function sendAidotTelegramEvent(serverUrl: string, member: AidotTelegramMembership, input: unknown, connection: AidotTelegramConnection, signal?: AbortSignal): Promise<{ response: AidotSharedRoomMessageResponse | null; replies: AidotTelegramReplyPayload[] }> {
  const event = normalizeAidotTelegramEvent(input, connection); if (event === null) return { response: null, replies: [] };
  if (event.nativeRoomKey !== member.nativeRoomKey || event.nativeParticipantKey !== member.nativeParticipantKey || event.participantKind !== member.participantKind || event.chatId !== member.chatId || event.threadId !== member.threadId || event.roomMode !== member.roomMode) throw new Error("Telegram 방/질문자에 해당하는 서버 발급 증명이 필요합니다.");
  let message = event.message;
  if (event.sourceMessageId) {
    const history = await fetchAidotClientRoom(serverUrl, "telegram", member, signal);
    const source = history.messages?.find(item => item.id === event.sourceMessageId && item.participantKind === "bot");
    if (!source) throw new Error("Telegram 버튼 원본 기록을 확인할 수 없어 재실행하지 않습니다.");
    const value = telegramReply(source, { chatId: event.chatId, threadId: event.threadId }).choices[event.choiceIndex ?? -1];
    if (value === undefined || !source.payload?.sourceTalkNodeId) throw new Error("Telegram 버튼 선택 출처가 올바르지 않습니다.");
    message = telegramSelectionMessage(source, value);
  }
  const response = await sendAidotClientMessage(serverUrl, "telegram", member, message, event.addressed, event.eventId, signal);
  try { return { response, replies: (response.botMessages ?? (response.botMessage ? [response.botMessage] : [])).map(item => buildAidotTelegramReplyPayload(item, { chatId: event.chatId, threadId: event.threadId })) }; }
  catch (cause) { throw Object.assign(new Error("CGA 답변은 수신했지만 Telegram 표현을 변환하지 못했습니다."), { cause, cgaResponse: response }); }
}


export type AidotGoogleChatConnection = { botUserName: string; spaceName: string; spaceType: "DIRECT_MESSAGE" | "SPACE" | "GROUP_CHAT" };
export type AidotGoogleChatEvent = {
  nativeRoomKey: string; nativeParticipantKey: string; participantKind: AidotClientParticipantKind;
  spaceName: string; threadName: string | null; roomMode: "private" | "shared";
  eventId: string; addressed: boolean; message: string; sourceMessageId?: string; choiceIndex?: number;
};
export type AidotGoogleChatReplyPayload = {
  parent: string; body: { text: string; cardsV2?: { cardId: string; card: { sections: { widgets: Record<string, unknown>[] }[] } }[]; thread?: { name: string } };
  messageReplyOption?: "REPLY_MESSAGE_OR_FAIL";
};
function googleChatRecord(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function googleChatName(value: unknown, kind: "user" | "space" | "message" | "thread", space?: string): string {
  const segment = "[A-Za-z0-9_.~-]+", pattern = kind === "user" ? `users/${segment}` : kind === "space" ? `spaces/${segment}` : `spaces/${segment}/${kind === "message" ? "messages" : "threads"}/${segment}`;
  if (typeof value !== "string" || !new RegExp(`^${pattern}$`).test(value) || (space && !value.startsWith(space + "/"))) throw new Error(`Google Chat ${kind} 범위가 올바르지 않습니다.`);
  return value;
}
function googleChatText(value: unknown, label: string): string { if (typeof value !== "string" || !value.trim()) throw new Error(`Google Chat ${label}이 없습니다.`); return value; }
function googleChatSpaceType(space: Record<string, unknown>): AidotGoogleChatConnection["spaceType"] {
  const value = space.spaceType ?? (space.type === "DM" ? "DIRECT_MESSAGE" : space.type === "ROOM" ? "SPACE" : undefined);
  if (value !== "DIRECT_MESSAGE" && value !== "SPACE" && value !== "GROUP_CHAT") throw new Error("Google Chat 공간 종류를 확인할 수 없습니다.");
  if (space.type !== undefined && ((space.type === "DM") !== (value === "DIRECT_MESSAGE") || !["DM", "ROOM"].includes(String(space.type)))) throw new Error("Google Chat 공간 종류가 모순됩니다.");
  return value;
}
function googleChatUuid(value: unknown): string {
  if (typeof value !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)) throw new Error("Google Chat 원본 CGA 메시지 ID가 올바르지 않습니다."); return value;
}
async function googleChatEventId(parts: unknown[]) {
  const bytes = new TextEncoder().encode(JSON.stringify(parts)), digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return "google-chat:" + Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, "0")).join("");
}
export async function normalizeAidotGoogleChatEvent(input: unknown, connection: AidotGoogleChatConnection): Promise<AidotGoogleChatEvent | null> {
  const event = googleChatRecord(input); if (event.type !== "MESSAGE" && event.type !== "CARD_CLICKED") return null;
  const self = googleChatName(connection.botUserName, "user"), space = googleChatRecord(event.space), spaceName = googleChatName(space.name, "space"), spaceType = googleChatSpaceType(space);
  if (spaceName !== googleChatName(connection.spaceName, "space") || spaceType !== connection.spaceType) throw new Error("다른 Google Chat 공간에는 이 연결을 사용할 수 없습니다.");
  const message = googleChatRecord(event.message), sender = googleChatRecord(message.sender), user = googleChatRecord(event.user), isClick = event.type === "CARD_CLICKED";
  if (!isClick && sender.name === self) return null;
  const actor = googleChatName(user.name, "user"); if (user.type !== "HUMAN" && user.type !== "BOT") throw new Error("Google Chat 질문자 종류를 확인할 수 없습니다.");
  if (!isClick && (sender.name !== actor || sender.type !== user.type)) throw new Error("Google Chat 메시지와 질문자가 다릅니다.");
  if (isClick && (sender.name !== self || sender.type !== "BOT")) throw new Error("Google Chat 버튼 원본 봇이 다릅니다.");
  const messageName = googleChatName(message.name, "message", spaceName), messageSpace = googleChatRecord(message.space);
  if (message.space !== undefined && (messageSpace.name !== spaceName || ((messageSpace.spaceType !== undefined || messageSpace.type !== undefined) && googleChatSpaceType(messageSpace) !== spaceType))) throw new Error("Google Chat 메시지 공간이 다릅니다.");
  let threadName: string | null = null;
  for (const thread of [message.thread, event.thread]) if (thread !== undefined) {
    const name = googleChatName(googleChatRecord(thread).name, "thread", spaceName);
    if (threadName !== null && threadName !== name) throw new Error("Google Chat 스레드가 모순됩니다."); threadName = name;
  }
  if (threadName === null && event.threadKey !== undefined) throw new Error("Google Chat 스레드 키를 기본 방으로 전환할 수 없습니다.");
  const participantKind: AidotClientParticipantKind = user.type === "BOT" ? "bot" : "user";
  const common = { nativeRoomKey: "google-chat:" + JSON.stringify([self, spaceName, spaceType, threadName]), nativeParticipantKey: `google-chat:${participantKind}:${actor}`, participantKind, spaceName, threadName, roomMode: spaceType === "DIRECT_MESSAGE" ? "private" as const : "shared" as const };
  if (isClick) {
    if (event.isDialogEvent === true) throw new Error("Google Chat 대화상자 입력은 지원하지 않습니다.");
    const action = googleChatRecord(event.action), commonEvent = googleChatRecord(event.common);
    const fn = action.actionMethodName ?? commonEvent.invokedFunction; if (fn !== "cga.select") return null;
    if (action.actionMethodName !== undefined && commonEvent.invokedFunction !== undefined && action.actionMethodName !== commonEvent.invokedFunction) throw new Error("Google Chat 버튼 함수가 모순됩니다.");
    const parameters: Record<string, string> = {};
    if (action.parameters !== undefined) {
      if (!Array.isArray(action.parameters)) throw new Error("Google Chat 버튼 매개변수가 올바르지 않습니다.");
      for (const entry of action.parameters) { const item = googleChatRecord(entry); if (typeof item.key !== "string" || typeof item.value !== "string" || Object.hasOwn(parameters, item.key)) throw new Error("Google Chat 버튼 매개변수가 중복/누락됐습니다."); parameters[item.key] = item.value; }
    }
    if (commonEvent.parameters !== undefined) {
      const values = googleChatRecord(commonEvent.parameters); for (const [key, value] of Object.entries(values)) { if (typeof value !== "string" || (Object.hasOwn(parameters, key) && parameters[key] !== value)) throw new Error("Google Chat 버튼 매개변수가 모순됩니다."); parameters[key] = value; }
    }
    if (Object.keys(parameters).length !== 2 || !Object.hasOwn(parameters, "sourceMessageId") || !/^(0|[1-9]\d*)$/.test(parameters.choiceIndex ?? "") || !Number.isSafeInteger(Number(parameters.choiceIndex))) throw new Error("Google Chat 버튼 선택이 올바르지 않습니다.");
    const sourceMessageId = googleChatUuid(parameters.sourceMessageId), choiceIndex = Number(parameters.choiceIndex), time = googleChatText(event.eventTime, "이벤트 시각");
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/.test(time) || !Number.isFinite(Date.parse(time))) throw new Error("Google Chat 이벤트 시각이 올바르지 않습니다.");
    return { ...common, addressed: true, message: "", sourceMessageId, choiceIndex, eventId: await googleChatEventId(["CARD_CLICKED", messageName, actor, time, "cga.select", sourceMessageId, choiceIndex]) };
  }
  const raw = googleChatText(message.text, "질문"); if (new TextEncoder().encode(raw).length > 32000) throw new Error("Google Chat 질문이 너무 깁니다.");
  if (message.annotations !== undefined && !Array.isArray(message.annotations)) throw new Error("Google Chat annotation 목록이 올바르지 않습니다.");
  const ranges: { start: number; end: number }[] = []; let command = "";
  for (const value of (Array.isArray(message.annotations) ? message.annotations : [])) {
    const annotation = googleChatRecord(value), start = annotation.startIndex ?? 0, length = annotation.length ?? 0;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(length) || Number(start) < 0 || Number(length) < 0 || Number(start) + Number(length) > raw.length) throw new Error("Google Chat annotation 범위가 올바르지 않습니다.");
    const mention = googleChatRecord(annotation.userMention), slash = googleChatRecord(annotation.slashCommand);
    const targetMention = annotation.type === "USER_MENTION" && mention.type === "MENTION" && googleChatRecord(mention.user).name === self;
    const targetCommand = annotation.type === "SLASH_COMMAND" && googleChatRecord(slash.bot).name === self;
    if (targetMention || targetCommand) {
      if (!length) throw new Error("Google Chat 호출 annotation의 길이가 없습니다.");
      if (targetCommand && slash.triggersDialog === true) throw new Error("Google Chat 대화상자 명령은 지원하지 않습니다.");
      if (targetCommand && !command) command = raw.slice(Number(start), Number(start) + Number(length));
      ranges.push({ start: Number(start), end: Number(start) + Number(length) });
    }
  }
  const addressed = common.roomMode === "private" || ranges.length > 0; let text = raw, previous = raw.length;
  if (addressed) for (const range of ranges.sort((a, b) => b.start - a.start)) { if (range.end > previous) throw new Error("Google Chat 호출 annotation이 겹칩니다."); text = text.slice(0, range.start) + text.slice(range.end); previous = range.start; }
  text = addressed ? text.trim() || command : raw;
  if (!text.trim() || Array.from(text).length > 4000) throw new Error("Google Chat CGA 질문은 1~4000자여야 합니다.");
  return { ...common, addressed, message: text, eventId: await googleChatEventId(["MESSAGE", messageName]) };
}
function googleChatSelectionMessage(source: AidotChannelStoredMessage, value: string) {
  const wire = JSON.stringify({ webchatRichFormVersion: "1.0", sourceMessageId: source.id, sourceTalkNodeId: googleChatText(source.payload?.sourceTalkNodeId, "원본 Talk ID"), response: { buttonValue: value } });
  if (Array.from(wire).length > 4000) throw new Error("Google Chat 선택 응답이 기존 메시지 한도를 넘습니다."); return wire;
}
function googleChatReply(message: AidotChannelStoredMessage, destination: { spaceName: string; threadName?: string | null }) {
  const parent = googleChatName(destination.spaceName, "space"); if (typeof message.text !== "string") throw new Error("Google Chat 답변 본문이 올바르지 않습니다.");
  const packet: AidotGoogleChatReplyPayload = { parent, body: { text: message.text } }, type = message.messageType || "text", widgets: Record<string, unknown>[] = [], choices: string[] = [];
  if (destination.threadName !== undefined && destination.threadName !== null) { packet.body.thread = { name: googleChatName(destination.threadName, "thread", parent) }; packet.messageReplyOption = "REPLY_MESSAGE_OR_FAIL"; }
  const payload = googleChatRecord(message.payload), escape = (value: string) => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const scalar = (value: unknown): string => value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
  const url = (value: unknown): string => { if (typeof value !== "string" || !/^https?:\/\/[^\s]+$/i.test(value)) throw new Error("Google Chat URL은 HTTP/HTTPS여야 합니다."); return value; };
  const choice = (label: unknown, value: unknown) => {
    const selected = scalar(value); googleChatUuid(message.id); googleChatSelectionMessage(message, selected);
    const button = { text: googleChatText(label, "버튼 이름"), onClick: { action: { function: "cga.select", parameters: [{ key: "sourceMessageId", value: message.id }, { key: "choiceIndex", value: String(choices.length) }] } } }; choices.push(selected); return button;
  };
  const paragraph = (value: unknown) => { if (value !== undefined && value !== null && value !== "") widgets.push({ textParagraph: { text: escape(googleChatText(value, "카드 내용")).replaceAll("\n", "<br>") } }); };
  if (type === "button" || type === "link-button") {
    if (!Array.isArray(payload.options)) throw new Error("Google Chat 버튼 항목이 올바르지 않습니다.");
    const buttons = payload.options.map(item => {
      if (typeof item === "string" && type === "button") return choice(item, item);
      const option = googleChatRecord(item), label = option.label ?? option.text ?? option.value;
      if (type === "link-button" || option.url) return { text: googleChatText(label, "버튼 이름"), onClick: { openLink: { url: url(option.url) } } };
      return choice(label, option.value ?? option.messageText ?? label);
    });
    if (buttons.length) widgets.push({ buttonList: { buttons } });
  } else if (type === "table") {
    const columns = payload.columns, rows = payload.rows;
    if (!Array.isArray(columns) || columns.some(column => typeof column !== "string" || !column) || !Array.isArray(rows) || (rows.length && !columns.length)) throw new Error("Google Chat 표 구조가 올바르지 않습니다.");
    if (rows.length) {
      paragraph(columns.join(" | "));
      for (const item of rows) {
        if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error("Google Chat 표 행이 올바르지 않습니다.");
        const row = googleChatRecord(item), first = row[String(columns[0])], key = typeof payload.keyColumn === "string" && row[payload.keyColumn] !== undefined ? row[payload.keyColumn] : first;
        const decoratedText: Record<string, unknown> = { text: escape(columns.map(column => scalar(row[String(column)])).join(" | ")).replaceAll("\n", "<br>"), wrapText: true };
        if (payload.selectable !== false) decoratedText.button = choice(scalar(first) || scalar(key) || "선택", key);
        widgets.push({ decoratedText });
      }
    }
  } else if (type === "card" || type === "carousel") {
    const value = type === "card" ? payload.card : payload.carousel;
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Google Chat 카드 구조가 올바르지 않습니다.");
    const card = googleChatRecord(value); paragraph(card.title);
    if (card.imageUrl) { const imageUrl = url(card.imageUrl); if (!/^https:\/\//i.test(imageUrl)) throw new Error("Google Chat 이미지 URL은 HTTPS여야 합니다."); widgets.push({ image: { imageUrl, altText: typeof card.title === "string" && card.title ? card.title : "이미지" } }); }
    if (type === "card") paragraph(card.description);
    else {
      paragraph(card.itemTitle); paragraph(card.itemContents);
      const buttons = []; if (String(card.buttonType).toLowerCase() === "button") buttons.push(choice(card.itemButtonLabel, card.itemButtonValue));
      if (card.bottomButtonLabel || card.bottomButtonValue) buttons.push(choice(card.bottomButtonLabel, card.bottomButtonValue));
      if (buttons.length) widgets.push({ buttonList: { buttons } });
    }
  } else if (type !== "text") throw new Error(`Google Chat에서 지원하지 않는 Talk 표현입니다: ${type}`);
  if (widgets.length > 100) throw new Error("Google Chat 카드 widget은 100개 이내여야 합니다.");
  if (widgets.length) packet.body.cardsV2 = [{ cardId: message.id, card: { sections: [{ widgets }] } }];
  if ((!packet.body.text.trim() && !widgets.length) || new TextEncoder().encode(JSON.stringify(packet.body)).length > 32000) throw new Error("Google Chat 답변이 비었거나 32000바이트 한도를 넘습니다.");
  return { packet, choices };
}
export function buildAidotGoogleChatReplyPayload(message: AidotChannelStoredMessage, destination: { spaceName: string; threadName?: string | null }) { return googleChatReply(message, destination).packet; }
export type AidotGoogleChatMembership = AidotClientMembership & Pick<AidotGoogleChatEvent, "nativeRoomKey" | "nativeParticipantKey" | "spaceName" | "threadName" | "roomMode">;
function googleChatMembership(member: AidotClientMembership, event: AidotGoogleChatEvent): AidotGoogleChatMembership {
  if (member.participantKind !== event.participantKind) throw new Error("Google Chat 질문자와 서버 발급 종류가 다릅니다."); return { ...member, nativeRoomKey: event.nativeRoomKey, nativeParticipantKey: event.nativeParticipantKey, spaceName: event.spaceName, threadName: event.threadName, roomMode: event.roomMode };
}
export async function createAidotGoogleChatRoom(serverUrl: string, botId: string, input: unknown, connection: AidotGoogleChatConnection, signal?: AbortSignal) {
  const event = await normalizeAidotGoogleChatEvent(input, connection); if (!event) return null;
  if (event.sourceMessageId) throw new Error("Google Chat 버튼은 기존 대화방에서만 이어갈 수 있습니다.");
  return googleChatMembership(await createAidotClientRoom(serverUrl, "google-chat", botId, event.roomMode, event.participantKind, event.nativeParticipantKey, signal), event);
}
export async function joinAidotGoogleChatRoom(serverUrl: string, room: Pick<AidotGoogleChatMembership, "roomId" | "nativeRoomKey">, input: unknown, connection: AidotGoogleChatConnection, signal?: AbortSignal) {
  const event = await normalizeAidotGoogleChatEvent(input, connection); if (!event) return null;
  if (event.nativeRoomKey !== room.nativeRoomKey) throw new Error("다른 Google Chat 공간/스레드에는 이 방 정보로 입장할 수 없습니다.");
  return googleChatMembership(await joinAidotClientRoom(serverUrl, "google-chat", room.roomId, event.participantKind, event.nativeParticipantKey, signal), event);
}
export async function sendAidotGoogleChatEvent(serverUrl: string, member: AidotGoogleChatMembership, input: unknown, connection: AidotGoogleChatConnection, signal?: AbortSignal): Promise<{ response: AidotSharedRoomMessageResponse | null; replies: AidotGoogleChatReplyPayload[] }> {
  const event = await normalizeAidotGoogleChatEvent(input, connection); if (!event) return { response: null, replies: [] };
  if (event.nativeRoomKey !== member.nativeRoomKey || event.nativeParticipantKey !== member.nativeParticipantKey || event.participantKind !== member.participantKind || event.spaceName !== member.spaceName || event.threadName !== member.threadName || event.roomMode !== member.roomMode) throw new Error("Google Chat 방/질문자의 서버 발급 증명이 필요합니다.");
  let message = event.message;
  if (event.sourceMessageId) {
    const history = await fetchAidotClientRoom(serverUrl, "google-chat", member, signal), source = history.messages?.find(item => item.id === event.sourceMessageId && item.participantKind === "bot");
    if (!source) throw new Error("Google Chat 버튼 원본 기록이 없어 재실행하지 않습니다.");
    const value = googleChatReply(source, { spaceName: event.spaceName, threadName: event.threadName }).choices[event.choiceIndex ?? -1];
    if (value === undefined || !source.payload?.sourceTalkNodeId) throw new Error("Google Chat 버튼 선택 출처가 올바르지 않습니다."); message = googleChatSelectionMessage(source, value);
  }
  const response = await sendAidotClientMessage(serverUrl, "google-chat", member, message, event.addressed, event.eventId, signal);
  try { return { response, replies: (response.botMessages ?? (response.botMessage ? [response.botMessage] : [])).map(item => buildAidotGoogleChatReplyPayload(item, { spaceName: event.spaceName, threadName: event.threadName })) }; }
  catch (cause) { throw Object.assign(new Error("CGA 답변은 수신했지만 Google Chat 표현을 변환하지 못했습니다."), { cause, cgaResponse: response }); }
}
