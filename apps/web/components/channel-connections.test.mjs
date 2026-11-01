import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import ts from "typescript";
const text=readFileSync(new URL("./channel-connections-panel.tsx",import.meta.url),"utf8");
const ast=ts.createSourceFile("panel.tsx",text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
const declaration=ast.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text==="buildConnectionPayload");
assert.ok(declaration);
const js=ts.transpileModule(declaration.getText(ast),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText.replace(/export /g, "");
const build=new Function("exports",js+";return buildConnectionPayload;")({});
test("blank credential edits preserve existing credentials without submitting masks",()=>{
 const payload=build({name:" 연결 A ",bot_id:"bot1",installation_id:"T1",account_id:"B1",target_id:"C1"},{bot_token:"",app_token:""});
 assert.equal(payload.name,"연결 A");assert.equal(payload.credentials,undefined);assert.equal(payload.target_id,"C1");
});
test("explicit credential input stays in protected child payload",()=>{
 const payload=build({name:"A",bot_id:"bot1",installation_id:"T1",account_id:"B1",target_id:"C1"},{bot_token:"synthetic-only",app_token:""});
 assert.deepEqual(payload.credentials,{bot_token:"synthetic-only"});assert.equal(payload.auth_config,undefined);
});

let submitDeclaration;
function visit(n){if(ts.isFunctionDeclaration(n)&&n.name?.text==="handleSave")submitDeclaration=n;ts.forEachChild(n,visit);}visit(ast);
const submitJs=ts.transpileModule(submitDeclaration.getText(ast),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
function subject(fail=false){
 const events=[],draft={name:"A",bot_id:"bot",installation_id:"T1",account_id:"B1",target_id:"C1"};
 const bindings={busy:false,token:"fixture",channel:{id:"parent"},draft,credentials:{bot_token:"synthetic-only"},editing:"connection",copy:{saved:"saved",error:"failed"},connectionErrorMessage:extract("connectionErrorMessage"),buildConnectionPayload:build,
 saveChannelConnection:async(...args)=>{events.push(["request",...args]);if(fail)throw Error("provider-secret-must-not-display");},
 resetForm:()=>events.push(["clear"]),reload:async()=>events.push(["reload"]),setBusy:v=>events.push(["busy",v]),setError:v=>events.push(["error",v]),setNotice:v=>events.push(["notice",v])};
 const submit=new Function(...Object.keys(bindings),submitJs+";return handleSave;")(...Object.values(bindings));return {submit:()=>submit({preventDefault(){}}),events};
}
test("successful save clears credential form before reloading",async()=>{
 const s=subject();await s.submit();const clear=s.events.findIndex(e=>e[0]==="clear");assert.ok(clear>=0&&clear<s.events.findIndex(e=>e[0]==="reload"));assert.equal(s.events.filter(e=>e[0]==="request").length,1);
});
test("failed save preserves input and never retries or displays raw external errors",async()=>{
 const s=subject(true);await s.submit();assert.equal(s.events.filter(e=>e[0]==="request").length,1);assert.ok(!s.events.some(e=>e[0]==="clear"));assert.ok(s.events.some(e=>e[0]==="error"&&e[1]==="failed"));assert.ok(!JSON.stringify(s.events.filter(e=>e[0]!=="request")).includes("provider-secret"));
});


function extract(name,bindings={}) {
 let found;function visit(n){if(ts.isFunctionDeclaration(n)&&n.name?.text===name)found=n;ts.forEachChild(n,visit);}visit(ast);assert.ok(found,`${name} missing`);
 const output=ts.transpileModule(found.getText(ast),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.React}}).outputText;
 return new Function("exports",...Object.keys(bindings),output+`;return ${name};`)({},...Object.values(bindings));
}
test("known connection errors explain key setup and duplicate targets without exposing raw errors",()=>{
 const show=extract("connectionErrorMessage");const copy={error:"generic",keyUnavailable:"configure server key",duplicate:"already registered",disconnectFirst:"disconnect first",invalidCredentials:"check credential fields"};
 assert.equal(show(new Error("CONNECTION_KEY_UNAVAILABLE: internal"),copy),"configure server key");
 assert.equal(show(new Error("DUPLICATE_CONNECTION: internal"),copy),"already registered");
 assert.equal(show(new Error("unknown provider secret-value"),copy),"generic");
 for (const code of ["__proto__", "constructor", "toString"]) assert.equal(show(new Error(code+": internal"),copy),"generic");
});
test("connection list renders assigned bot and last-check information",async()=>{
 const React=await import("react");const {renderToStaticMarkup}=await import("react-dom/server");const Details=extract("ConnectionDetails",{React});
 const copy={bot:"Answer bot",lastChecked:"Last checked",notChecked:"Not checked"};
 const base={item:{bot_id:"bot-a",last_checked_at:null},bots:[{id:"bot-a",name:"Support A"}],copy,language:"en"};
 const initial=renderToStaticMarkup(React.createElement(Details,base));assert.ok(initial.includes("Support A")&&initial.includes("Not checked"));
 const checked=renderToStaticMarkup(React.createElement(Details,{...base,item:{...base.item,last_checked_at:"2026-11-02T00:00:00Z"}}));assert.ok(checked.includes("2026")&&!checked.includes("Not checked"));
});
