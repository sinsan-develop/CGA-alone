import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
const source=readFileSync(new URL("./bot-create-dialog.tsx",import.meta.url),"utf8");
const effect=source.slice(source.indexOf("  useEffect(() => {"),source.indexOf("  const nluModelOptions"));
function subject({bots=[],fail=false,session=true}={}){
 const state={allowed:false,existing:false,error:"",navigation:[]};let cleanup;let resolve,reject;
 const result=new Promise((a,b)=>{resolve=a;reject=b;});
 const bindings={useEffect:fn=>{cleanup=fn();},loadAuthSession:()=>session?{access_token:"synthetic-token"}:null,fetchStudioBots:()=>result,router:{replace:url=>state.navigation.push(url)},copy:{loginRequired:"login",createError:"failed",singleBotLimit:"one bot only"},setCreationAllowed:value=>state.allowed=value,setHasExistingBot:value=>state.existing=value,setErrorMessage:value=>state.error=value};
 new Function(...Object.keys(bindings),effect)(...Object.values(bindings));
 return {state,cleanup:()=>cleanup?.(),settle:async()=>{fail?reject(new Error("lookup failed")):resolve(bots);await new Promise(setImmediate);}};
}
test("existing bot keeps creation page, shows limit state and never redirects",async()=>{const s=subject({bots:[{id:"existing",data_json:{bot_kind:"bot"}}]});await s.settle();assert.deepEqual(s.state.navigation,[]);assert.equal(s.state.existing,true);assert.equal(s.state.allowed,false);});
test("existing hub is also limited without redirect",async()=>{const s=subject({bots:[{id:"hub",data_json:{bot_kind:"hub"}}]});await s.settle();assert.deepEqual(s.state.navigation,[]);assert.equal(s.state.existing,true);assert.equal(s.state.allowed,false);});
test("empty ownership result enables original creation",async()=>{const s=subject();assert.equal(s.state.allowed,false);await s.settle();assert.equal(s.state.allowed,true);assert.equal(s.state.existing,false);});
test("lookup failure remains blocked with error",async()=>{const s=subject({fail:true});await s.settle();assert.equal(s.state.allowed,false);assert.equal(s.state.error,"lookup failed");});
test("disposed request cannot enable creation or change notice",async()=>{const s=subject({bots:[{id:"existing"}]});s.cleanup();await s.settle();assert.equal(s.state.existing,false);assert.equal(s.state.allowed,false);assert.deepEqual(s.state.navigation,[]);});
test("limit notice and existing submit guard remain wired",()=>{assert.match(source,/hasExistingBot \? copy\.singleBotLimit/);assert.ok(source.includes('role="status"'));assert.ok(source.includes('if (!creationAllowed) return;'));assert.ok(source.includes('disabled={isSubmitting || !creationAllowed}'));});
