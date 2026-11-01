// 기존 SDK의 순수 변환만 실행한다. 네트워크/대화 실행/인증정보를 취급하지 않는다.
import { readFileSync } from "node:fs";
import * as sdk from "./channel.ts";
const normalizers = new Map([
  ["slack", sdk.normalizeAidotSlackEvent], ["discord", sdk.normalizeAidotDiscordEvent],
  ["telegram", sdk.normalizeAidotTelegramEvent], ["google-chat", sdk.normalizeAidotGoogleChatEvent], ["line", sdk.normalizeAidotLineEvent],
]);
const builders = new Map([
  ["slack", sdk.buildAidotSlackReplyPayload], ["discord", sdk.buildAidotDiscordReplyPayload],
  ["telegram", sdk.buildAidotTelegramReplyPayload], ["google-chat", sdk.buildAidotGoogleChatReplyPayload], ["line", sdk.buildAidotLineMessage],
]);
try {
  const source = readFileSync(0, "utf8");
  if (Buffer.byteLength(source) > 1048576) throw Error("size");
  const { provider, operation, payload } = JSON.parse(source);
  if (!normalizers.has(provider) || !payload || typeof payload !== "object") throw Error("input");
  let value;
  if (operation === "normalize") value = await normalizers.get(provider)(payload.input, payload.connection, payload.eventIndex);
  else if (operation === "replies") {
    if (!Array.isArray(payload.messages) || payload.messages.length > (provider === "line" ? 5 : 20)) throw Error("messages");
    value = payload.messages.map(message => builders.get(provider)(message, payload.destination));
  } else throw Error("operation");
  process.stdout.write(JSON.stringify({ ok: true, value }));
} catch {
  process.stdout.write(JSON.stringify({ ok: false, code: "CHANNEL_PACKET_INVALID" }));
  process.exitCode = 1;
}
