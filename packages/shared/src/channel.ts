export type AidotChannelType = "webchat" | "kakao" | "ms-teams";

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
