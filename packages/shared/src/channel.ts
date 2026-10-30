export type AidotChannelType = "webchat" | "kakao" | "ms-teams" | "slack";

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
      message: JSON.stringify({ webchatRichFormVersion: "1.0", sourceTalkNodeId, response: { buttonValue } }) };
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
  if (event.sourceMessageId) {
    const history = await fetchAidotClientRoom(serverUrl, "slack", member, signal);
    const allowed = canAskAidotRoomBot(history.turn, member.participantId, true, false);
    if (!canRespondAidotRoomCard(history.runtime, event.sourceTalkNodeId, allowed, event.sourceMessageId, history.messages)) {
      throw Object.assign(new Error("현재 질문자의 최신 Slack 카드에만 응답할 수 있습니다."), { status: 409 });
    }
  }
  const response = await sendAidotClientMessage(serverUrl, "slack", member, event.message, event.addressed, event.eventId, signal);
  try {
    const messages = response.botMessages ?? (response.botMessage ? [response.botMessage] : []);
    return { response, replies: messages.map(message => buildAidotSlackReplyPayload(message, { channelId: event.channelId, threadTs: event.threadTs })) };
  } catch (cause) {
    // 생성은 완료되었으며 표현 실패를 새 유료 질문으로 바꾸지 않는다.
    throw Object.assign(new Error("CGA 답변은 수신했지만 Slack 표현을 변환하지 못했습니다."), { cause, cgaResponse: response });
  }
}
