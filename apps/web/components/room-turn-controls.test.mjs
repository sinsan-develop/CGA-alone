import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
const source=readFileSync(new URL("../../../packages/shared/src/channel.ts",import.meta.url),"utf8");
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const holder={exports:{}};
new Function("exports","module","process",compiled)(holder.exports,holder,process);
const api=holder.exports;
const idle={state:"idle",ownerParticipantId:null,canAskBot:true};
const waiting={state:"waiting_input",ownerParticipantId:"server:A",canAskBot:false};


test("test_other_participant_bot_input_is_disabled",()=>{
  assert.equal(api.canAskAidotRoomBot({...waiting,canAskBot:true},"server:B",true,false),false);
  assert.equal(api.canAskAidotRoomBot(waiting,"server:B",true,false),false);
});

test("test_owner_followup_remains_enabled",()=>{
  assert.equal(api.canAskAidotRoomBot({...waiting,canAskBot:true},"server:A",true,false),true);
  assert.equal(api.canAskAidotRoomBot({...waiting,canAskBot:true},"server:A",true,true),false);
  assert.equal(api.canAskAidotRoomBot({...waiting,state:"processing",canAskBot:true},"server:A",true,false),false);
});

test("test_general_chat_remains_enabled",()=>{
  assert.equal(api.canSendAidotRoomChat(true,false),true);
  assert.equal(api.canSendAidotRoomChat(true,true),false);
  assert.equal(api.canSendAidotRoomChat(false,false),false);
});

test("test_turn_resets_on_end_and_error",()=>{
  assert.equal(api.canAskAidotRoomBot(idle,"server:B",true,false),true);
  assert.equal(api.canAskAidotRoomBot(null,"server:B",true,false),false);
  assert.equal(api.canAskAidotRoomBot(idle,"server:B",false,false),false);
  assert.equal(api.canAskAidotRoomBot({...idle,ownerParticipantId:"server:A"},"server:B",true,false),false);
});

async function capture(run){
  const original=globalThis.fetch;const calls=[];
  globalThis.fetch=async(url,init)=>{calls.push({url,init});return new Response(JSON.stringify({data:{roomId:"room-fixture",participantId:"server:A",conversationToken:"opaque-fixture-token",turn:idle,messages:[],botMessages:[],botMessage:null}}),{status:200});};
  try{await run(calls);}finally{globalThis.fetch=original;}
}

test("shared create and join use existing endpoints without creator JWT",async()=>{
  await capture(async(calls)=>{
    await api.createAidotSharedRoom("http://qa.invalid","bot-fixture","A");
    await api.joinAidotSharedRoom("http://qa.invalid","bot-fixture","room-fixture","B");
    assert.equal(calls[0].url,"http://qa.invalid/api/v1/webchat/bots/bot-fixture/shared-rooms");
    assert.equal(calls[1].url,"http://qa.invalid/api/v1/webchat/bots/bot-fixture/shared-rooms/room-fixture/participants");
    assert.equal(JSON.parse(calls[0].init.body).participant_name,"A");
    assert.equal(new Headers(calls[0].init.headers).has("Authorization"),false);
  });
});

test("shared bot and general sends use per-member credential and explicit addressed",async()=>{
  await capture(async(calls)=>{
    const member={roomId:"room-fixture",participantId:"server:A",conversationToken:"opaque-fixture-token"};
    await api.sendAidotSharedRoomMessage("http://qa.invalid","bot-fixture",member,"질문",true);
    await api.sendAidotSharedRoomMessage("http://qa.invalid","bot-fixture",member,"일반 대화",false);
    assert.equal(calls[0].url,"http://qa.invalid/api/v1/webchat/bots/bot-fixture/rooms/room-fixture/messages");
    assert.equal(new Headers(calls[0].init.headers).get("X-Aidot-Conversation-Token"),member.conversationToken);
    assert.deepEqual(JSON.parse(calls[0].init.body),{message:"질문",addressed:true});
    assert.deepEqual(JSON.parse(calls[1].init.body),{message:"일반 대화",addressed:false});
  });
});

test("shared status refresh is read-only and carries the same member capability",async()=>{
  await capture(async(calls)=>{
    const member={roomId:"room-fixture",participantId:"server:A",conversationToken:"opaque-fixture-token"};
    const result=await api.fetchAidotSharedRoom("http://qa.invalid","bot-fixture",member);
    assert.equal(calls[0].init.method,"GET");
    assert.equal(calls[0].init.body,undefined);
    assert.equal(new Headers(calls[0].init.headers).get("X-Aidot-Conversation-Token"),member.conversationToken);
    assert.deepEqual(result.turn,idle);
  });
});

function storedRenderer(){
 const text=readFileSync(new URL("./simulator-page.tsx",import.meta.url),"utf8");
 const tree=ts.createSourceFile("simulator-page.tsx",text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 const node=tree.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text==="sharedMessageToSimulatorMessage");
 assert.ok(node,"shared server message adapter must exist");
 const output=ts.transpileModule(node.getText(tree),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 return new Function(`${output};return sharedMessageToSimulatorMessage;`)();
}
const stored=(messageType,payload={})=>({id:"saved-1",participantId:"server:A",participantKind:"bot",participantName:"답변봇",messageType,text:"원문\n한글",createdAt:"2026-10-30T00:00:00Z",payload:{sourceTalkNodeId:"talk-1",...payload}});
test("stored identity and text survive shared rendering",()=>{
 const value=storedRenderer()({...stored("text"),participantKind:"user",participantName:"참여자 A"});
 assert.equal(value.id,"saved-1");assert.equal(value.sender,"user");assert.equal(value.participantName,"참여자 A");assert.equal(value.text,"원문\n한글");assert.equal(value.timestamp,"2026-10-30T00:00:00Z");assert.equal(value.sourceTalkNodeId,"talk-1");
});
test("stored buttons and safe links retain native values",()=>{
 const render=storedRenderer();assert.deepEqual(render(stored("button",{options:["예약","취소"]})).quickReplies,["예약","취소"]);
 assert.deepEqual(render(stored("link-button",{options:[{label:"문서",url:"https://example.com/doc"},{label:"위험",url:"javascript:alert(1)"}]})).cardItems,[{label:"문서",value:"https://example.com/doc",href:"https://example.com/doc"}]);
});
test("stored card and carousel preserve native fields",()=>{
 const render=storedRenderer();const card=render(stored("card",{card:{title:"제목",description:"설명",imageUrl:"https://example.com/p.png"}}));assert.equal(card.cardTitle,"제목");assert.equal(card.cardDescription,"설명");assert.equal(card.cardImageUrl,"https://example.com/p.png");
 const carousel=render(stored("carousel",{carousel:{title:"상품",itemTitle:"항목",itemContents:"내용",itemButtonValue:"상품-1"}}));assert.equal(carousel.cardTitle,"항목");assert.equal(carousel.cardDescription,"내용");assert.deepEqual(carousel.quickReplies,["상품-1"]);
});
test("stored table and DTMF preserve server values",()=>{
 const render=storedRenderer();assert.deepEqual(render(stored("table",{columns:["코드","이름"],rows:[{"코드":"A","이름":"어울"}],keyColumn:"코드",selectable:true})).table,{columns:["코드","이름"],rows:[{key:"A",values:["어울"]}],selectable:true});
 const dtmf={minLength:1,maxLength:3,endCharacter:"#",firstInputTimeoutMs:1000,overallInputTimeoutMs:2000};assert.deepEqual(render(stored("dtmf",{dtmf})).dtmf,dtmf);
});
test("stored forms reuse rich renderer payload",()=>{
 const render=storedRenderer();const richForm=[{type:"INPUT",key:"name",title:"이름"}];const adaptiveCard={type:"AdaptiveCard",version:"1.6",body:[]};
 assert.deepEqual(render(stored("form",{richForm})).template.rawJson,richForm);assert.equal(render(stored("form",{richForm})).template.kind,"rich-form");assert.deepEqual(render(stored("form-a-card",{adaptiveCard})).template.rawJson,adaptiveCard);assert.equal(render(stored("form-a-card",{adaptiveCard})).template.kind,"adaptive-card");
});

function inputChangeHandler(sharedMode, confirmResult=true){
 const text=readFileSync(new URL("./simulator-page.tsx",import.meta.url),"utf8");
 const tree=ts.createSourceFile("simulator-page.tsx",text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 let found; function visit(node){if(ts.isFunctionDeclaration(node)&&node.name?.text==="handleInputChange")found=node;ts.forEachChild(node,visit);}visit(tree);
 assert.ok(found);const output=ts.transpileModule(found.getText(tree),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 const calls={confirm:0,script:0,inputs:[]};
 const handler=new Function("sharedMode","loadedScript","window","getStudioRuntimeMessage","uiLanguage","setLoadedScript","setInputValue",`${output};return handleInputChange;`)(
  sharedMode,{active:true,sentences:["미리보기 문장"],index:0},{confirm:()=>{calls.confirm++;return confirmResult;}},(_,text)=>text,"ko",()=>{calls.script++;},value=>calls.inputs.push(value));
 return {handler,calls};
}
test("shared general input does not stop or prompt for preserved preview script",()=>{
 const {handler,calls}=inputChangeHandler(true,false);handler("공유방 일반 대화");
 assert.equal(calls.confirm,0);assert.equal(calls.script,0);assert.deepEqual(calls.inputs,["공유방 일반 대화"]);
});
test("private preview script still asks before editing its current sentence",()=>{
 const {handler,calls}=inputChangeHandler(false,false);handler("편집 문장");
 assert.equal(calls.confirm,1);assert.equal(calls.script,0);assert.deepEqual(calls.inputs,[]);
});

test("shared table preserves visible column order when selection key is not first",()=>{
 const rendered=storedRenderer()(stored("table",{columns:["이름","코드","수량"],rows:[{"이름":"어울","코드":"A-1","수량":2}],keyColumn:"코드",selectable:true}));
 assert.deepEqual(rendered.table.columns,["이름","코드","수량"]);
 assert.equal(rendered.table.rows[0].key,"A-1");
 assert.equal(rendered.table.rows[0].firstCell,"어울");
 assert.deepEqual(rendered.table.rows[0].values,["A-1","2"]);
});

test("only the current server waiting card accepts the owner response",()=>{
 const runtime={dialogEnded:false,sessionEnded:false,waitingNodeId:"current"};
 assert.equal(api.canRespondAidotRoomCard(runtime,"current",true),true);
 assert.equal(api.canRespondAidotRoomCard(runtime,"previous",true),false);
 assert.equal(api.canRespondAidotRoomCard(runtime,"current",false),false);
 assert.equal(api.canRespondAidotRoomCard({...runtime,dialogEnded:true},"current",true),false);
 assert.equal(api.canRespondAidotRoomCard({...runtime,sessionEnded:true},"current",true),false);
 assert.equal(api.canRespondAidotRoomCard(undefined,"current",true),false);
});

test("repeated waiting node enables only the latest stored card instance",()=>{
 const runtime={dialogEnded:false,sessionEnded:false,waitingNodeId:"current"};
 const messages=[{id:"older",participantKind:"bot",payload:{sourceTalkNodeId:"current"}},{id:"newer",participantKind:"bot",payload:{sourceTalkNodeId:"current"}}];
 assert.equal(api.canRespondAidotRoomCard(runtime,"current",true,"older",messages),false);
 assert.equal(api.canRespondAidotRoomCard(runtime,"current",true,"newer",messages),true);
 assert.equal(api.canRespondAidotRoomCard(runtime,"current",true,"missing",messages),false);
});

test("shared form result displays the selected answer while keeping stored wire unchanged",()=>{
 const render=storedRenderer();const wire=JSON.stringify({webchatRichFormVersion:"1.0",sourceTalkNodeId:"followup",response:{input:{value:"폼 확인"},buttonValue:"폼 확인"}});
 const user={...stored("text"),participantKind:"user",text:wire};
 assert.equal(render(user).text,"폼 확인");assert.equal(user.text,wire);
 assert.equal(render({...user,participantKind:"bot"}).text,wire);
 const ordinary=JSON.stringify({response:{buttonValue:"일반 JSON"}});
 assert.equal(render({...user,text:ordinary}).text,ordinary);
});

test("shared room controls use all seven existing simulator languages",()=>{
 const source=readFileSync(new URL("../lib/i18n/simulator.ts",import.meta.url),"utf8");
 const output=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 const holder={exports:{}};new Function("exports","module",output)(holder.exports,holder);
 const labels=["대화 방식","1:1 미리보기","공유방","참여자 이름","방 ID","공유방 ID","새 공유방","참가자로 입장","현재 방:","답변 처리 중","내 후속 답변을 기다립니다.","다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.","봇에게 질문할 수 있습니다.","방 상태 확인 중","일반 대화","봇에게 질문","운영 버전 확인 중"];
 for(const key of labels){assert.equal(holder.exports.getSimulatorLabel("ko",key),key);for(const lang of ["en","zh-CN","ja","vi","fr","de"]){const value=holder.exports.getSimulatorLabel(lang,key);assert.ok(value&&!/[가-힣]/.test(value),`${lang}: ${key}`);}}
});

test("shared card submission preserves source and value while typed text stays plain",()=>{
 const text=readFileSync(new URL("./simulator-page.tsx",import.meta.url),"utf8");
 const tree=ts.createSourceFile("simulator-page.tsx",text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 let found;function visit(node){if(ts.isFunctionDeclaration(node)&&node.name?.text==="submitUtterance")found=node;ts.forEachChild(node,visit);}visit(tree);assert.ok(found);
 const output=ts.transpileModule(found.getText(tree),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 const calls=[];
 const submit=new Function("sharedMode","canRespondAidotRoomCard","sharedHistory","sharedCanAsk","sendSharedMessage",`${output};return submitUtterance;`)(true,api.canRespondAidotRoomCard,{runtime:{waitingNodeId:"wait",dialogEnded:false,sessionEnded:false}},true,(value,addressed)=>{calls.push({value,addressed});return Promise.resolve();});
 for(const value of ["예약","row-1","123"]){submit(value,{},"wait");assert.deepEqual(JSON.parse(calls.at(-1).value),{webchatRichFormVersion:"1.0",sourceTalkNodeId:"wait",response:{buttonValue:value}});assert.equal(calls.at(-1).addressed,true);}
 submit("직접 입력",{});assert.deepEqual(calls.at(-1),{value:"직접 입력",addressed:true});
});

test("shared form templates keep identity across identical polling and ordinary messages",()=>{
 const source=readFileSync(new URL("./simulator-page.tsx",import.meta.url),"utf8");const tree=ts.createSourceFile("simulator-page.tsx",source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 const names=["sharedMessageToSimulatorMessage","sharedMessagesForSimulator"];
 const functions=names.map(name=>{const node=tree.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text===name);assert.ok(node,name);return node.getText(tree);});
 const output=ts.transpileModule(functions.join("\n"),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 const render=new Function(`${output};return sharedMessagesForSimulator;`)();const cache=new Map();
 const form=stored("form",{richForm:[{type:"INPUT",key:"details",required:true}]});const first=render([form],cache)[0].template;
 assert.equal(render([form],cache)[0].template,first);
 assert.equal(render(JSON.parse(JSON.stringify([form])),cache)[0].template,first);
 assert.equal(render([JSON.parse(JSON.stringify(form)),{...stored("text"),id:"general",participantKind:"user"}],cache)[0].template,first);
 const changed=structuredClone(form);changed.payload.richForm[0].title="새 내용";assert.notEqual(render([changed],cache)[0].template,first);
 assert.equal(cache.size,1);render([{...form,id:"new-room-card"}],cache);assert.equal(cache.size,1);assert.equal(cache.has("saved-1"),false);
 const adaptive={...stored("form-a-card",{adaptiveCard:{type:"AdaptiveCard",body:[]}}),id:"adaptive"};const card=render([adaptive],cache)[0].template;
 assert.equal(render(JSON.parse(JSON.stringify([adaptive])),cache)[0].template,card);
 render([],cache);assert.equal(cache.size,0);
});

test("shared Adaptive submit includes collected inputs while legacy serialization stays unchanged",()=>{
 const source=readFileSync(new URL("./simulator-page.tsx",import.meta.url),"utf8");const tree=ts.createSourceFile("simulator-page.tsx",source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 const component=tree.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text==="AdaptiveCardRenderer");assert.ok(component);
 let handler;function visit(node){if(ts.isBinaryExpression(node)&&node.left.getText(tree)==="card.onExecuteAction")handler=node.right;ts.forEachChild(node,visit);}visit(component);assert.ok(handler);
 const output=ts.transpileModule(`const handler=${handler.getText(tree)};`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 class SubmitAction{constructor(){this.data={submitted:true,details:"카드 입력 보존 답변"};}toJSON(){return {type:"Action.Submit",data:{submitted:true}};}}
 for(const includeSubmittedInputs of [true,false]){const calls=[];const execute=new Function("disabled","onActionRef","AdaptiveCards","includeSubmittedInputs",`${output};return handler;`)(false,{current:v=>calls.push(v)},{SubmitAction},includeSubmittedInputs);execute(new SubmitAction());assert.deepEqual(JSON.parse(calls[0]),includeSubmittedInputs?{submitted:true,details:"카드 입력 보존 답변"}:{submitted:true});}
});


test("common client SDK binds channel, credential and external event without retry", async()=>{
  await capture(async(calls)=>{
    const member={roomId:"room/id",participantId:"server:A",conversationToken:"opaque-token"};
    await api.createAidotClientRoom("http://localhost:8320","ms-teams","bot/id","shared","bot","LLM session");
    await api.joinAidotClientRoom("http://localhost:8320","ms-teams",member.roomId,"user","B");
    await api.fetchAidotClientRoom("http://localhost:8320","ms-teams",member);
    await api.sendAidotClientMessage("http://localhost:8320","ms-teams",member,"질문",true,"event-1");
    assert.equal(calls.length,4);
    assert.equal(calls[0].url,"http://localhost:8320/api/v1/channels/ms-teams/rooms");
    assert.deepEqual(JSON.parse(calls[0].init.body),{bot_id:"bot/id",room_mode:"shared",participant_kind:"bot",participant_name:"LLM session"});
    assert.match(calls[1].url,/room%2Fid\/participants$/);
    assert.equal(calls[2].init.headers.get("X-Aidot-Conversation-Token"),member.conversationToken);
    assert.equal(calls[3].init.headers.get("X-Aidot-Event-Id"),"event-1");
    assert.deepEqual(JSON.parse(calls[3].init.body),{message:"질문",addressed:true});
    assert.equal(calls[3].init.headers.has("Authorization"),false);
  });
  const original=globalThis.fetch;let count=0;
  globalThis.fetch=async()=>{count++;throw new TypeError("synthetic transport unknown");};
  try{await assert.rejects(()=>api.sendAidotClientMessage("http://localhost:8320","webchat",{roomId:"r",participantId:"a",conversationToken:"t"},"q",true,"e"));assert.equal(count,1);}
  finally{globalThis.fetch=original;}
});


test("registered channel templates use existing Talk selector and preserve native fields",()=>{
  const designer=readFileSync(new URL("./flow-designer-page.tsx",import.meta.url),"utf8");
  const ast=ts.createSourceFile("designer.tsx",designer,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  const names=new Set(["normalizeTalkMessageType","getTalkMessageTypeMeta","isActiveTalkTemplate","buildTalkMessageTypeOptions","normalizeTalkChannelTemplateMap","applyTalkChannelTemplateToConfig","isTalkMessageTypeAvailableForBotMode"]);
  const pieces=[];
  for(const statement of ast.statements){
    if(ts.isFunctionDeclaration(statement)&&names.has(statement.name?.text))pieces.push(statement.getText(ast));
    if(ts.isVariableStatement(statement)&&statement.declarationList.declarations.some(d=>d.name.getText(ast)==="TALK_MESSAGE_TYPE_OPTIONS"))pieces.push(statement.getText(ast));
  }
  assert.equal(pieces.length,8);
  const js=ts.transpileModule(pieces.join("\n"),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  const subject=new Function(js+";return {buildTalkMessageTypeOptions,normalizeTalkChannelTemplateMap,applyTalkChannelTemplateToConfig};")();
  for(const name of ["webchat","kakao","ms-teams"]){
    const fixture=JSON.parse(readFileSync(new URL(`../../api/tests/cga_alone/fixtures/client_templates/${name}.json`,import.meta.url),"utf8"));
    const registered=[{id:"stored-template-id",channel_code:fixture.code,name:"저장한 템플릿",renderer_type:fixture.renderer,status:"active"},
      {channel_code:fixture.code,name:"비활성",renderer_type:"text",status:"inactive"}];
    const options=subject.buildTalkMessageTypeOptions(registered,fixture.code,"text");
    assert.equal(options.length,1);assert.equal(options[0].value,fixture.type);assert.equal(options[0].label,"저장한 템플릿");
    const template={...fixture.talk,tableColumnMappings:[],tableManualRows:[],linkButtonItems:[]};
    const map=subject.normalizeTalkChannelTemplateMap({[fixture.code]:template},registered,"text");
    const applied=subject.applyTalkChannelTemplateToConfig({channelTemplates:map,basicMessages:["보존"]},fixture.code,"text");
    assert.equal(applied.messageType,fixture.type);
    assert.deepEqual(applied.messages,fixture.talk.messages);
    assert.deepEqual(applied.basicMessages,["보존"]);
  }
});


const slackFixture=JSON.parse(readFileSync(new URL("../../api/tests/cga_alone/fixtures/client_templates/slack.json",import.meta.url),"utf8"));
const slackBinding={roomId:"slack-room",participantId:"server:A",participantKind:"user",conversationToken:"opaque-slack-fixture",nativeRoomKey:'slack:["TQA001","CQA001",null]',nativeParticipantKey:"slack:user:UQA001",channelId:"CQA001",threadTs:null,roomMode:"shared",turn:idle};
async function slackCapture(run,history=null){
  const original=globalThis.fetch,calls=[];
  globalThis.fetch=async(url,init)=>{calls.push({url,init});return new Response(JSON.stringify({data:init.method==="GET"?history:{...slackBinding,botMessages:[],botMessage:null}}),{status:200});};
  try{await run(calls);}finally{globalThis.fetch=original;}
}

test("Slack caller uses common API, native scope and server credentials",async()=>{
  await slackCapture(async(calls)=>{
    const member=await api.createAidotSlackRoom("http://localhost:8320","bot-id",slackFixture.addressed,slackFixture.connection);
    assert.equal(member.nativeRoomKey,slackBinding.nativeRoomKey);
    const other=structuredClone(slackFixture.general);
    await api.joinAidotSlackRoom("http://localhost:8320",member,other,slackFixture.connection);
    const result=await api.sendAidotSlackEvent("http://localhost:8320",member,slackFixture.addressed,slackFixture.connection);
    assert.deepEqual(result.replies,[]);
    assert.equal(calls.length,3);
    assert.equal(calls[0].url,"http://localhost:8320/api/v1/channels/slack/rooms");
    assert.deepEqual(JSON.parse(calls[0].init.body),{bot_id:"bot-id",room_mode:"shared",participant_kind:"user",participant_name:"slack:user:UQA001"});
    assert.match(calls[1].url,/slack-room\/participants$/);
    assert.equal(calls[2].init.headers.get("X-Aidot-Conversation-Token"),slackBinding.conversationToken);
    assert.equal(calls[2].init.headers.get("X-Aidot-Event-Id"),"slack-message:1793376000.000100");
    assert.deepEqual(JSON.parse(calls[2].init.body),{message:"미등록 슬랙 질문",addressed:true});
    assert.ok(calls.every(call=>!call.url.includes("slack.com")));
  });
});

test("Slack caller ignores self output and rejects wrong room, actor, kind without a request",async()=>{
  await slackCapture(async(calls)=>{
    const own=structuredClone(slackFixture.addressed);own.event.user=slackFixture.connection.botUserId;
    assert.deepEqual(await api.sendAidotSlackEvent("http://localhost:8320",slackBinding,own,slackFixture.connection),{response:null,replies:[]});
    for(const patch of [{nativeRoomKey:"other"},{nativeParticipantKey:"other"},{participantKind:"bot"}]){
      await assert.rejects(()=>api.sendAidotSlackEvent("http://localhost:8320",{...slackBinding,...patch},slackFixture.addressed,slackFixture.connection));
    }
    assert.equal(calls.length,0);
  });
  const original=globalThis.fetch;let calls=0;globalThis.fetch=async()=>{calls++;throw new Error("synthetic unknown transport");};
  try{await assert.rejects(()=>api.sendAidotSlackEvent("http://localhost:8320",slackBinding,slackFixture.addressed,slackFixture.connection));assert.equal(calls,1);}
  finally{globalThis.fetch=original;}
});

test("Slack callback delegates atomic validation and duplicate recovery to the server",async()=>{
  const message={id:"00000000-0000-4000-8000-000000000001",participantKind:"bot",messageType:"button",text:"선택",payload:{options:["확인"],sourceTalkNodeId:"followup"}};
  const button=api.buildAidotSlackReplyPayload(message,{channelId:"CQA001"}).blocks.find(block=>block.type==="actions").elements[0];
  const action={type:"block_actions",team:{id:"TQA001"},user:{id:"UQA001"},channel:{id:"CQA001"},container:{message_ts:"1793376000.000900"},actions:[{...button,action_ts:"1793376001.000100"}]};
  const original=globalThis.fetch;let calls=[];const response={roomId:"slack-room",botMessages:[],botMessage:null,turn:idle,runtime:{dialogEnded:true,sessionEnded:false}};
  globalThis.fetch=async(url,init)=>{calls.push(init);return new Response(JSON.stringify({data:init.method==="GET"?{...response,messages:[]}:response}),{status:200});};
  try{
    for(let i=0;i<2;i++) assert.deepEqual((await api.sendAidotSlackEvent("http://localhost:8320",slackBinding,action,slackFixture.connection)).response,response);
    assert.equal(calls.length,2);assert.ok(calls.every(call=>call.method==="POST"));
    const wire=JSON.parse(JSON.parse(calls[0].body).message);assert.equal(wire.sourceMessageId,message.id);assert.equal(wire.sourceTalkNodeId,"followup");
    assert.equal(calls[0].body,calls[1].body);assert.equal(calls[0].headers.get("X-Aidot-Event-Id"),calls[1].headers.get("X-Aidot-Event-Id"));
    calls=[];globalThis.fetch=async(url,init)=>{calls.push(init);return new Response(JSON.stringify({detail:"현재 카드가 아닙니다."}),{status:409});};
    await assert.rejects(()=>api.sendAidotSlackEvent("http://localhost:8320",slackBinding,action,slackFixture.connection),error=>error.status===409);assert.equal(calls.length,1);
  }finally{globalThis.fetch=original;}
});

test("Slack template draft preserves supported expressions and excludes unrepresentable ones",()=>{
  const source=readFileSync(new URL("../app/admin/templates/page.tsx",import.meta.url),"utf8");
  const ast=ts.createSourceFile("templates.tsx",source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX),pieces=[];
  const names=new Set(["RENDERER_OPTIONS","SLACK_RENDERER_TYPES","TEMPLATE_RENDERER_DEFAULTS"]);
  for(const node of ast.statements){
    if(ts.isVariableStatement(node)&&node.declarationList.declarations.some(d=>names.has(d.name.getText(ast))))pieces.push(node.getText(ast));
    if(ts.isFunctionDeclaration(node)&&["isKakaoTemplateChannel","normalizeTemplateDraftByChannel"].includes(node.name?.text))pieces.push(node.getText(ast));
  }
  const js=ts.transpileModule(pieces.join("\n"),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  const normalize=new Function(js+";return normalizeTemplateDraftByChannel;")();
  for(const renderer of ["text","button","card","table","link-button","carousel"]){assert.equal(normalize({channel_code:"SLACK",renderer_type:renderer,description:"설명",item_types:""},{}).renderer_type,renderer);}
  for(const renderer of ["form","html","dtmf","form-a-card"]){assert.equal(normalize({channel_code:"SLACK",renderer_type:renderer,description:"설명",item_types:""},{}).renderer_type,"text");}
  assert.equal(normalize({channel_code:"WEBCHAT",renderer_type:"form",description:"설명",item_types:""},{}).renderer_type,"form");
});


test("Slack payload error retains received CGA result without another request",async()=>{
  const original=globalThis.fetch;let calls=0;
  globalThis.fetch=async()=>{calls++;return new Response(JSON.stringify({data:{roomId:"slack-room",turn:idle,botMessages:[{id:"known-result",messageType:"html",text:"미지원 표현",payload:{}}],botMessage:{id:"known-result",messageType:"html",text:"미지원 표현",payload:{}}}}),{status:200});};
  try{await assert.rejects(()=>api.sendAidotSlackEvent("http://localhost:8320",slackBinding,slackFixture.addressed,slackFixture.connection),error=>error.cgaResponse?.botMessage?.id==="known-result");assert.equal(calls,1);}
  finally{globalThis.fetch=original;}
});


const discordFixture=JSON.parse(readFileSync(new URL("../../../apps/api/tests/cga_alone/fixtures/client_templates/discord.json",import.meta.url),"utf8"));
const discordBinding={roomId:"discord-room",participantId:"server:A",participantKind:"user",conversationToken:"opaque-discord-fixture",nativeRoomKey:'discord:["100000000000000001","100000000000000002"]',nativeParticipantKey:"discord:user:100000000000000010",channelId:"100000000000000002",roomMode:"shared",turn:idle};
function discordAction(message,index=0){return {type:3,id:"100000000000000201",application_id:discordFixture.connection.applicationId,guild_id:discordFixture.connection.guildId,channel_id:discordFixture.connection.channelId,member:{user:{id:"100000000000000010"}},message:{author:{id:discordFixture.connection.botUserId}},data:{component_type:2,custom_id:`cga:${message.id}:${index}`}};}

test("Discord caller uses existing issued credentials and binds native scope",async()=>{
 const original=globalThis.fetch,calls=[];
 globalThis.fetch=async(url,init)=>{calls.push({url,init});return new Response(JSON.stringify({data:{...discordBinding,botMessages:[],botMessage:null}}),{status:200});};
 try{
  const member=await api.createAidotDiscordRoom("http://localhost:8320","bot-id",discordFixture.addressed,discordFixture.connection);
  assert.equal(member.nativeRoomKey,discordBinding.nativeRoomKey);
  await api.joinAidotDiscordRoom("http://localhost:8320",member,discordFixture.addressed,discordFixture.connection);
  await api.sendAidotDiscordEvent("http://localhost:8320",member,discordFixture.addressed,discordFixture.connection);
  assert.ok(calls[0].url.endsWith("/channels/discord/rooms"));assert.equal(calls[2].init.headers.get("X-Aidot-Conversation-Token"),member.conversationToken);
  assert.equal(calls[2].init.headers.get("X-Aidot-Event-Id"),"discord-message:100000000000000101");
  const count=calls.length;await assert.rejects(()=>api.sendAidotDiscordEvent("http://localhost:8320",{...member,nativeParticipantKey:"other"},discordFixture.addressed,discordFixture.connection));assert.equal(calls.length,count);
  const own=structuredClone(discordFixture.addressed);own.d.author.id=discordFixture.connection.botUserId;assert.deepEqual(await api.sendAidotDiscordEvent("http://localhost:8320",member,own,discordFixture.connection),{response:null,replies:[]});assert.equal(calls.length,count);
 }finally{globalThis.fetch=original;}
});

test("Discord callback restores original table key and completed event without client turn rejection",async()=>{
 const original=globalThis.fetch,calls=[];
 const message={id:"00000000-0000-4000-8000-000000000001",participantKind:"bot",messageType:"table",text:"표",payload:{columns:["name","id"],rows:[{name:"표시",id:"선택 키"}],keyColumn:"id",selectable:true,sourceTalkNodeId:"followup"}};
 const history={messages:[message],turn:{state:"idle",ownerParticipantId:null,canAskBot:true},runtime:{dialogEnded:true,sessionEnded:false}},result={roomId:"discord-room",botMessage:null,botMessages:[],turn:idle};
 globalThis.fetch=async(url,init)=>{calls.push(init);return new Response(JSON.stringify({data:init.method==="GET"?history:result}),{status:200});};
 try{
  for(let i=0;i<2;i++)await api.sendAidotDiscordEvent("http://localhost:8320",discordBinding,discordAction(message),discordFixture.connection);
  assert.equal(calls.length,4);assert.deepEqual(calls.map(call=>call.method),["GET","POST","GET","POST"]);
  const wire=JSON.parse(JSON.parse(calls[1].body).message);assert.equal(wire.response.buttonValue,"선택 키");assert.equal(wire.sourceMessageId,message.id);assert.equal(wire.sourceTalkNodeId,"followup");assert.equal(calls[1].body,calls[3].body);
  calls.length=0;history.messages=[];await assert.rejects(()=>api.sendAidotDiscordEvent("http://localhost:8320",discordBinding,discordAction(message),discordFixture.connection));assert.equal(calls.length,1);
 }finally{globalThis.fetch=original;}
});

test("Discord callback cannot create an unrelated new room",async()=>{
 const original=globalThis.fetch;let count=0;globalThis.fetch=async()=>{count++;return new Response(JSON.stringify({data:discordBinding}),{status:200});};
 try{await assert.rejects(()=>api.createAidotDiscordRoom("http://localhost:8320","bot-id",discordAction({id:"00000000-0000-4000-8000-000000000001"}),discordFixture.connection));assert.equal(count,0);}finally{globalThis.fetch=original;}
});

test("Discord rejects a choice which cannot fit the original response wire",()=>{
 const message={id:"00000000-0000-4000-8000-000000000001",messageType:"carousel",text:"카드",payload:{sourceTalkNodeId:"followup",carousel:{title:"제목",itemTitle:"항목",itemContents:"내용",buttonType:"Button",itemButtonLabel:"선택",itemButtonValue:"x".repeat(3950)}}};
 assert.throws(()=>api.buildAidotDiscordReplyPayload(message,{channelId:discordBinding.channelId}));
});

test("Discord conversion failure preserves received result and never retries",async()=>{
 const original=globalThis.fetch;let count=0;globalThis.fetch=async()=>{count++;return new Response(JSON.stringify({data:{botMessage:{id:"known",messageType:"html",text:"미지원"},botMessages:[{id:"known",messageType:"html",text:"미지원"}]}}),{status:200});};
 try{await assert.rejects(()=>api.sendAidotDiscordEvent("http://localhost:8320",discordBinding,discordFixture.addressed,discordFixture.connection),error=>error.cgaResponse?.botMessage?.id==="known");assert.equal(count,1);}finally{globalThis.fetch=original;}
});


test("Discord preserves an explicitly empty original choice value",async()=>{
 const original=globalThis.fetch;let wire;
 const source={id:"00000000-0000-4000-8000-000000000001",participantKind:"bot",messageType:"carousel",text:"카드",payload:{sourceTalkNodeId:"followup",carousel:{title:"제목",itemTitle:"항목",itemContents:"내용",buttonType:"Button",itemButtonLabel:"선택",itemButtonValue:""}}};
 globalThis.fetch=async(url,init)=>{if(init.method==="POST")wire=JSON.parse(JSON.parse(init.body).message);return new Response(JSON.stringify({data:init.method==="GET"?{messages:[source]}:{botMessages:[],botMessage:null}}),{status:200});};
 try{await api.sendAidotDiscordEvent("http://localhost:8320",discordBinding,discordAction(source),discordFixture.connection);assert.equal(wire.response.buttonValue,"");}finally{globalThis.fetch=original;}
});
