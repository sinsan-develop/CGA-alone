export type AidotChannelType = "webchat" | "kakao" | "ms-teams" | "slack" | "discord";

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
