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
