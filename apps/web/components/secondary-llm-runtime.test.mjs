import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import ts from "typescript";
const path=new URL("../lib/secondary-llm.ts",import.meta.url);
async function subject(){assert.ok(fs.existsSync(path),"S04 client 공통 결과 helper 필요");const code=ts.transpileModule(fs.readFileSync(path,"utf8"),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;return import("data:text/javascript;base64,"+Buffer.from(code).toString("base64"));}
const config={module_id:"module",outcome:{variableName:"api.phase",answeredValue:"ok",unrecognizedValue:"no"}};
test("응답 텍스트가 아니라 제작자 결과값으로만 구분한다",async()=>{const s=await subject();for(const [value,status] of [["ok","answered"],["no","unrecognized"],["unknown","error"],["","error"]]){assert.equal(s.evaluateSecondaryOutcome(config,{"api.phase":value},{waiting:false,executionError:false}).status,status);}});
test("Talk 대기와 실행 오류를 정상 미인식으로 오인하지 않는다",async()=>{const s=await subject();assert.equal(s.evaluateSecondaryOutcome(config,{"api.phase":"ok"},{waiting:true,executionError:false}).status,"pending");assert.equal(s.evaluateSecondaryOutcome(config,{"api.phase":"no"},{waiting:false,executionError:true}).status,"error");});
test("이전 결과 alias만 비우고 원래 질문과 다른 변수를 보존한다",async()=>{const s=await subject();const previous={"input":"질문","kept":"보존","api.phase":"ok","api.response.phase":"ok"};const prepared=s.prepareSecondaryVariables(previous,config);assert.equal(prepared.input,"질문");assert.equal(prepared.kept,"보존");assert.equal(prepared["api.phase"],undefined);assert.equal(prepared["api.response.phase"],undefined);assert.equal(previous["api.phase"],"ok");});

function nativeFunction(name, bindings) {
 const source=ts.createSourceFile("simulator.tsx",fs.readFileSync(new URL("./simulator-page.tsx",import.meta.url),"utf8"),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);let found;
 function visit(node){if(ts.isFunctionDeclaration(node)&&node.name?.text===name)found=node;ts.forEachChild(node,visit);}visit(source);
 assert.ok(found,`기존 시뮬레이터의 ${name} 연결이 필요합니다`);
 const code=ts.transpileModule(found.getText(source),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
 return new Function(...Object.keys(bindings),code+`;return ${name};`)(...Object.values(bindings));
}
test("시뮬레이터는 지정된 모듈 ID만 실행하고 이전 결과를 제거한다",async()=>{
 const shared=await subject();let execution;
 const start=nativeFunction("startSecondaryRuntime",{
 secondaryConfig:config,dialogs:[{id:"shadow",name:"module",dialogType:0},{id:"module",name:"진짜",dialogType:0}],
 versionDocument:{},getDialogFlowGraph:()=>({nodes:[{id:"start"}]}),getGraphStartNodeId:()=>"start",
 prepareSecondaryVariables:shared.prepareSecondaryVariables,secondaryOutcomeMapping:shared.secondaryOutcomeMapping,
 normalizeRuntimeVariableName:name=>name.replace(/^\$/,""),continueRuntime:(runtime,analysis)=>{execution={runtime,analysis};},
 });
 assert.equal(start("a1",{input:"원래 질문","api.phase":"ok",kept:"보존"}),true);
 assert.equal(execution.runtime.dialog.id,"module");assert.equal(execution.runtime.variables["api.phase"],undefined);
 assert.equal(execution.runtime.variables.input,"원래 질문");assert.equal(execution.runtime.variables.kept,"보존");
 assert.deepEqual(execution.runtime.secondaryConfig,config);
});


async function nativeRuntimeHarness(phase="ok", failure=false, waiting=false) {
 const shared=await subject();const visible=[];let stored;let calls=0;let fallbacks=0;
 const graph={nodes:[{id:"function",kind:"function",title:"API",config:{}},{id:"talk",kind:"talk",title:"답변",config:{responseType:waiting?"relay":"none"}},{id:"end",kind:"end",title:"종료",config:{message:"",endSessionImmediately:false}}]};
 const variables={input:"원래 질문",kept:"보존"};
 const runtime={dialog:{id:"module",name:"모듈",dialogType:0},graph,nextNodeId:"function",variables,ended:false,secondaryConfig:config};
 const run=nativeFunction("continueRuntime",{
 simulatorSessionIdRef:{current:"qa"},maxCardsBetweenUserResponses:20,getNodeById:(g,id)=>g.nodes.find(n=>n.id===id),
 writeSimulatorLog:()=>{},appendAnalysisLog:()=>{},FLOW_CARD_LABELS:{function:"Function",talk:"Talk",end:"End"},
 executeFunctionNode:async(node,vars)=>{calls++;return {ok:!failure,variables:{...vars,"api.phase":phase}};},
 isCurrentSimulatorSession:()=>true,getNextNodeId:(g,id,port)=>port==="exception"?"":id==="function"?"talk":"end",
 buildTalkMessages:()=>[{sender:"bot",text:"화면 답변"}],getTalkConfigForChannel:node=>node.config,
 shouldWaitForTalkResponse:cfg=>cfg.responseType==="relay",restoreRuntimeReturn:()=>null,
 versionSettings:{conversationDefaults:{intentDetection:{beforeSessionEndModule:""}}},
 evaluateSecondaryOutcome:shared.evaluateSecondaryOutcome,resolveRuntimeVariableValue:(vars,key)=>vars[key],
 finishSecondaryFallback:()=>{fallbacks++;stored=null;},intentFallbackCountRef:{current:1},
 setMessages:update=>visible.push(...update([])),setRuntime:value=>{stored=value;},
 SIMULATOR_RUNTIME_MESSAGES:{systemError:"실행 오류"},getStudioRuntimeMessage:(lang,text)=>text,
 formatStudioRuntimeMessage:(lang,text)=>text,uiLanguage:"ko",runtimeLanguage:"ko",
 makeMessage:(sender,text)=>({sender,text}),
 });
 await run(runtime,"a1");return {visible,get stored(){return stored;},get calls(){return calls;},get fallbacks(){return fallbacks;},run};
}
for(const phase of ["ok","no","unexpected"])test(`실제 continueRuntime 결과 판정 ${phase}`,async()=>{
 const r=await nativeRuntimeHarness(phase);
 assert.equal(r.calls,1);
 if(phase==="ok"){assert.equal(r.visible[0].text,"화면 답변");assert.equal(r.stored.secondaryConfig,undefined);assert.equal(r.fallbacks,0);}
 else if(phase==="no"){assert.deepEqual(r.visible,[]);assert.equal(r.fallbacks,1);}
 else {assert.equal(r.visible[0].text,"실행 오류");assert.equal(r.fallbacks,0);}
});
test("실제 Function 실패는 정상 미인식 값이 있어도 오류",async()=>{const r=await nativeRuntimeHarness("no",true);assert.equal(r.fallbacks,0);assert.equal(r.visible[0].text,"실행 오류");assert.equal(r.calls,1);});
test("실제 Talk 대기 뒤 재개는 같은 모듈과 변수를 유지하고 API 재실행 없음",async()=>{
 const r=await nativeRuntimeHarness("ok",false,true);assert.equal(r.stored.waitingTalkNodeId,"talk");assert.deepEqual(r.stored.secondaryConfig,config);
 await r.run({...r.stored,waitingTalkNodeId:undefined},"a1");assert.equal(r.calls,1);assert.equal(r.stored.secondaryConfig,undefined);assert.equal(r.stored.ended,true);
});

for (const engine of ["ml","semantic"])for(const outcome of ["hit","miss","failure"])test(`실제 분류 진입 ${engine}/${outcome}`,async()=>{
 let primary=0,secondary=0,local=0,visible=[];const dialog={id:"local",name:"로컬",dialogType:1};
 const classify=async()=>{primary++;if(outcome==="failure")throw Error("synthetic unavailable");return outcome==="hit"?[{dialog,score:99}]:[];};
 const start=nativeFunction("startIntentRuntime",{
 runtime:null,startDialog:null,applyBlocklistPatterns:text=>text,findSmalltalk:()=>null,findExactingMatchingDialog:()=>null,findRuleTargetDialog:()=>null,
 nluModel:{type:engine},scoreCutoff:75,similarIntentScore:85,maxIntentResults:3,
 classifyMlIntentForSimulator:async()=>({scores:await classify(),scoreCutoff:75,similarIntentScore:85,maxIntentResults:3}),
 classifySemanticIntentForSimulator:classify,isSemanticNluType:type=>type==="semantic",getSimilarCandidates:scores=>scores,
 startDialogRuntimeWithAnswerVariables:async()=>{local++;},intentFallbackCountRef:{current:0},
 versionDocument:{},buildAnalysis:()=>({id:"a1"}),setAnalyses:()=>{},setSelectedAnalysisId:()=>{},appendAnalysisLog:()=>{},
 secondaryConfig:config,startSecondaryRuntime:(id,vars)=>{secondary++;assert.equal(vars.input,"원래 질문");return true;},
 setMessages:update=>{visible=update(visible);},makeMessage:(sender,text)=>({sender,text}),setRuntime:()=>{},
 SIMULATOR_RUNTIME_MESSAGES:{systemError:"실행 오류"},
 });
 assert.equal(await start("원래 질문",{kept:"보존"}),true);assert.equal(primary,1);
 assert.equal(secondary,outcome==="miss"?1:0);assert.equal(local,outcome==="hit"?1:0);
 if(outcome==="failure")assert.equal(visible[0].text,"실행 오류");
});
