import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import ts from "typescript";
const text=readFileSync(new URL("../app/me/password/page.tsx",import.meta.url),"utf8");
const ast=ts.createSourceFile("password.tsx",text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
let declaration;function visit(n){if(ts.isFunctionDeclaration(n)&&n.name?.text==="handleSubmit")declaration=n;ts.forEachChild(n,visit);}visit(ast);assert.ok(declaration);
const js=ts.transpileModule(declaration.getText(ast),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
function subject({api=async()=>({message:"changed"}),confirm="new-fixture-password"}={}){
 const events=[],calls=[],navigation=[];
 const state=name=>value=>events.push([name,value]);
 const bindings={apiRequest:async(...args)=>{calls.push(args);return api(...args);},token:"fixture-session-token",currentPassword:"old-fixture-password",newPassword:"new-fixture-password",newPasswordConfirm:confirm,
 copy:{mismatch:"mismatch",changeFailed:"failed"},router:{replace:path=>{events.push(["navigate",path]);navigation.push(path);}},
 setMessage:state("message"),setErrorMessage:state("error"),setSubmitting:state("submitting"),setCurrentPassword:state("current"),setNewPassword:state("new"),setNewPasswordConfirm:state("confirm")};
 const submit=new Function(...Object.keys(bindings),js+";return handleSubmit;")(...Object.values(bindings));
 return {submit:()=>submit({preventDefault(){}}),events,calls,navigation};
}
test("successful password change retains the existing token and replaces the form with workspace",async()=>{
 const s=subject();await s.submit();assert.deepEqual(s.navigation,["/studio/workspace"]);assert.equal(s.calls.length,1);assert.equal(s.calls[0][0],"/api/v1/auth/change-password");assert.equal(s.calls[0][2],"fixture-session-token");
 for(const key of ["current","new","confirm"]){const reset=s.events.findIndex(e=>e[0]===key&&e[1]==="");assert.ok(reset>=0,`${key} password field was not cleared`);assert.ok(reset<s.events.findIndex(e=>e[0]==="navigate"));}
});
test("failed password change keeps the form and the entered values",async()=>{
 const s=subject({api:async()=>{throw new Error("current password rejected");}});await s.submit();assert.deepEqual(s.navigation,[]);assert.ok(s.events.some(e=>e[0]==="error"&&e[1]==="current password rejected"));assert.ok(!s.events.some(e=>["current","new","confirm"].includes(e[0])));assert.deepEqual(s.events.at(-1),["submitting",false]);
});
test("pending password change cannot navigate before the server confirms success",async()=>{
 let resolve;const waiting=new Promise(r=>resolve=r);const s=subject({api:()=>waiting});const pending=s.submit();assert.deepEqual(s.navigation,[]);resolve({message:"changed"});await pending;assert.deepEqual(s.navigation,["/studio/workspace"]);
});
test("confirmation mismatch neither calls the server nor navigates",async()=>{
 const s=subject({confirm:"different"});await s.submit();assert.deepEqual(s.calls,[]);assert.deepEqual(s.navigation,[]);assert.ok(s.events.some(e=>e[0]==="error"&&e[1]==="mismatch"));
});
