import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
const path = new URL("../lib/studio-bots-api.ts", import.meta.url);
function subject(name, globals = {}) {
  const source = ts.createSourceFile("api.ts", fs.readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true);
  const node = source.statements.find(row => ts.isFunctionDeclaration(row) && row.name?.text === name);
  assert.ok(node, `승인된 지속 대화 함수 ${name}이 필요합니다`);
  const code = ts.transpileModule(node.getText(source).replace(/^export /, ""), {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  return vm.runInNewContext(`${code}; ${name}`, globals);
}
test("대기·불명·읽기 전용·로컬 전송 잠금 중에는 다음 질문을 막는다", () => {
  const canSend = subject("canSendPersistentTurn");
  const room = {status:"open", can_send:true};
  assert.equal(canSend(room, null, false), true);
  assert.equal(canSend(room, {phase:"committed"}, false), true);
  for (const phase of ["reserved", "pending_delivery", "unknown", "needs_reconcile", "response_pending", "cancelled"]) {
    assert.equal(canSend(room, {phase}, false), false);
  }
  assert.equal(canSend(room, null, true), false);
  assert.equal(canSend({...room, status:"closed"}, null, false), false);
  assert.equal(canSend({...room, can_send:false}, null, false), false);
});
test("서버 메시지가 DOM에 그대로 보인 뒤에만 ID와 해시로 확인한다", () => {
  const receipt = subject("persistentDisplayReceipt");
  const turn = {phase:"pending_delivery", message:{id:"m1", hash:"a".repeat(64), text:"<b>답변</b>\n두 줄"}};
  const element = {dataset:{persistentMessageId:"m1"},textContent:turn.message.text};
  assert.equal(JSON.stringify(receipt(turn, element)), JSON.stringify({message_id:"m1",content_hash:"a".repeat(64)}));
  assert.equal(receipt(turn, {...element,textContent:"답변"}), null);
  assert.equal(receipt(turn, {...element,dataset:{persistentMessageId:"other"}}), null);
  assert.equal(receipt({...turn,phase:"unknown"}, element), null);
  assert.equal(receipt(turn, null), null);
});
test("재접속 힌트는 사용자·봇별이며 인증 자격 증명을 넣지 않는다", () => {
  const key = subject("persistentRoomStorageKey");
  assert.notEqual(key("a","bot"), key("b","bot"));
  assert.notEqual(key("a","bot"), key("a","other"));
  assert.equal(key("a","bot"), key("a","bot"));
});
test("질문 POST는 동일 키와 generation을 한 번만 보내며 실패를 재시도하지 않는다", async () => {
  let calls = [];
  const send = subject("submitStudioPersistentTurn", {apiSameOriginRequest:async(...args)=>{calls.push(args);throw new Error("timeout");}});
  await assert.rejects(send("jwt","bot","room",{expected_generation:2,message_key:"key",question:"질문",dialog_id:"module"}), /timeout/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/v1/bots/bot/persistent/rooms/room/turns");
  assert.equal(JSON.parse(calls[0][1].body).expected_generation, 2);
  assert.equal(calls[0][2], "jwt");
});
test("새 대화는 명시 확인·generation만 보내고 과거 본문을 복사하지 않는다", async () => {
  let calls = [];
  const reset = subject("resetStudioPersistentRoom", {apiSameOriginRequest:async(...args)=>{calls.push(args);return {};}});
  await reset("jwt","bot","room",3,"module");
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0][1].body), {expected_generation:3,confirm_reset:true,dialog_id:"module"});
});
test("기존 모드는 별도 컴포넌트로 보존하고 지속 모드에서 단발 NLU를 실행하지 않는다", () => {
  const source = fs.readFileSync(new URL("./simulator-page.tsx", import.meta.url), "utf8");
  assert.ok(/function LegacySimulatorPage/.test(source));
  const start = source.indexOf("function PersistentSimulator");
  const end = source.indexOf("function LegacySimulatorPage");
  assert.ok(start >= 0 && end > start);
  const sample = source.slice(start, end);
  assert.doesNotMatch(sample, /generateStudioBotVersionLlmAnswer|testStudioBotVersion.*Intent|dangerouslySetInnerHTML/);
  assert.match(sample, /persistentDisplayReceipt/);
  assert.match(sample, /AbortController/);
});

test("worker가 두 조회 사이에 확정하면 오래된 기록으로 완료 화면을 만들지 않는다", () => {
  const refresh = subject("persistentTranscriptNeedsRefresh");
  const room={current_event_id:"event-1",can_send:false};
  const turn={event_id:"event-1",phase:"committed"};
  const messages=[{event_id:"event-1",memory_committed:false},{event_id:"event-1",memory_committed:false}];
  assert.equal(refresh({room,messages},turn),true);
  assert.equal(refresh({room:{...room,can_send:true},messages:messages.map(row=>({...row,memory_committed:true}))},turn),false);
  assert.equal(refresh({room,messages},{...turn,phase:"pending_delivery"}),false);
});

test("확정 미접수만 잠금을 풀고 timeout·5xx·상태불명은 보존한다", () => {
  const rejected=subject("isPersistentSubmissionRejected");
  for (const status of [400,401,403,404,409,422]) assert.equal(rejected({status}),true);
  for (const error of [{status:408},{status:500},{status:502},{status:504},new Error("timeout"),null]) assert.equal(rejected(error),false);
  const valid=subject("isPersistentQuestionValid");
  assert.equal(valid("x".repeat(4000)),true);assert.equal(valid("x".repeat(4001)),false);assert.equal(valid("  "),false);
});
test("same-origin 확정 거부의 HTTP 상태를 보존하고 재전송하지 않는다", async () => {
  const source=ts.createSourceFile("api.ts",fs.readFileSync(new URL("../lib/api.ts",import.meta.url),"utf8"),ts.ScriptTarget.Latest,true);
  const node=source.statements.find(row=>ts.isFunctionDeclaration(row)&&row.name?.text==="apiSameOriginRequest");
  const code=ts.transpileModule(node.getText(source).replace(/^export /,""),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  let calls=0;
  const fn=vm.runInNewContext(`${code}; apiSameOriginRequest`,{fetch:async()=>{calls++;return new Response(JSON.stringify({detail:"rejected"}),{status:422});},buildHeaders:()=>({}),performance,API_SLOW_REQUEST_THRESHOLD_MS:0,formatApiErrorDetail:value=>value,redirectToLogin:()=>{}});
  await assert.rejects(fn("/submit",{method:"POST"},"jwt"),error=>error.status===422&&error.message==="rejected");
  assert.equal(calls,1);
});
