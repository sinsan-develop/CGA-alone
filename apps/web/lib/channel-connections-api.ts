import { apiRequest } from "@/lib/api";
export type ChannelConnection = {
  id: string; channel_id: string; bot_id: string; name: string; provider: string;
  installation_id: string; account_id: string; target_id: string;
  desired_state: "connected" | "disconnected";
  observed_state: "connected" | "disconnected" | "connecting" | "disconnecting" | "error";
  generation: number; credentials_registered: boolean; identity_verified: boolean;
  last_error_code: string | null; last_checked_at: string | null;
};
export type ConnectionDraft = Pick<ChannelConnection, "name" | "bot_id" | "installation_id" | "account_id" | "target_id">;
export type ConnectionPayload = ConnectionDraft & { credentials?: Record<string, string> };
function path(channelId: string, id?: string) {
  return `/api/v1/admin/channels/${encodeURIComponent(channelId)}/connections${id ? `/${encodeURIComponent(id)}` : ""}`;
}
export function fetchChannelConnections(token: string, channelId: string) {
  return apiRequest<{items: ChannelConnection[]; execution_available: boolean}>(path(channelId), {}, token);
}
export function fetchConnectionBots(token: string, channelId: string) {
  return apiRequest<{items: {id: string; name: string}[]}>(`${path(channelId)}/bots`, {}, token);
}
export function saveChannelConnection(token: string, channelId: string, payload: ConnectionPayload, id?: string) {
  return apiRequest<{item: ChannelConnection}>(path(channelId, id), {method: id ? "PATCH" : "POST", body: JSON.stringify(payload)}, token);
}
export function actOnChannelConnection(token: string, channelId: string, id: string, action: "check" | "connect" | "disconnect" | "delete") {
  return apiRequest<{item?: ChannelConnection; configuration_valid?: boolean; external_verified?: boolean}>(`${path(channelId, id)}${action === "delete" ? "" : `/${action}`}`, {method: action === "delete" ? "DELETE" : "POST"}, token);
}
