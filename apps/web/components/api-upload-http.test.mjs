import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';
import ts from 'typescript';
const path=new URL('../lib/api-assets.ts',import.meta.url);
const code=ts.transpileModule(fs.readFileSync(path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
function api(crypto){const box={exports:{},crypto};vm.runInNewContext(code,box);return box.exports;}
function payload(){return {asset_format_version:1,apis:Array.from({length:5},(_,i)=>({id:'api-'+i,apiKey:'stable-key-'+i,name:'QA '+i,baseUrl:'https://example.invalid',methods:[{id:'method-'+i,name:'QA',httpMethod:'POST',methodUrl:'/qa',parameters:[{id:'parameter-'+i,name:'question',location:'body',dataType:'string',defaultValue:'synthetic-only'}],outputParameters:[{id:'output-'+i,name:'answer',path:'answer',dataType:'string'}]}]}))};}
function insecure(){return {getRandomValues:webcrypto.getRandomValues.bind(webcrypto)};}
test('HTTP에서도 내보낸 API 5개와 기존 키·중첩 ID를 보존한다',()=>{
 const source=payload(),result=api(insecure()).parseApiImportPayload(source);assert.equal(result.length,5);
 result.forEach((item,i)=>{assert.equal(item.id,source.apis[i].id);assert.equal(item.apiKey,source.apis[i].apiKey);assert.equal(item.methods[0].parameters[0].id,'parameter-'+i);assert.equal(item.methods[0].outputParameters[0].id,'output-'+i);});
});
test('HTTP에서 ID 없는 API와 메서드·매개변수를 UUIDv4로 생성한다',()=>{
 const m=api(insecure());const result=m.parseApiImportPayload({apis:[{name:'QA',methods:[{name:'method',parameters:[{name:'question'}],outputParameters:[{name:'answer'}]}]}]})[0];
 for(const value of [result.id,result.apiKey,result.methods[0].id,result.methods[0].parameters[0].id,result.methods[0].outputParameters[0].id])assert.match(value,uuid);
});
test('HTTP에서 새 API·메서드·매개변수 생성 ID가 중복되지 않는다',()=>{
 const m=api(insecure());const ids=Array.from({length:100},()=>m.createEmptyApiAsset()).flatMap(a=>[a.id,a.apiKey,a.methods[0].id]);
 ids.push(m.createEmptyApiParameter().id);assert.equal(new Set(ids).size,ids.length);ids.forEach(id=>assert.match(id,uuid));
});
test('HTTPS에서는 기존 native randomUUID를 그대로 사용한다',()=>{
 const expected='12345678-1234-4234-9234-123456789abc';const m=api({randomUUID:()=>expected,getRandomValues:()=>{throw Error('fallback should not run');}});assert.equal(m.createEmptyApiAsset().id,expected);
});
test('그룹 API 가져오기와 응답 샘플 생성도 HTTP ID 경로를 사용한다',()=>{
 const m=api(insecure());
 for(const [file,names,expression] of [
 ['group-api-list-page.tsx',['apiIdentity','mergeApisByApiKey'],`mergeApisByApiKey([],[{id:'',apiKey:'stable',name:'QA'}])[0].id`],
 ['api-store-list-page.tsx',['getSampleOutputType','buildOutputParametersFromSample'],`buildOutputParametersFromSample({answer:'QA'})[0].id`],
 ]){
  const text=fs.readFileSync(new URL(file,import.meta.url),'utf8');const ast=ts.createSourceFile(file,text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  const functions=names.map(name=>ast.statements.find(n=>ts.isFunctionDeclaration(n)&&n.name?.text===name).getText(ast));
  const js=ts.transpileModule(functions.join('\n'),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  const value=vm.runInNewContext(js+';'+expression,{crypto:insecure(),createApiAssetId:m.createApiAssetId});assert.match(value,uuid);
 }
});

test('HTTP 파일 선택 처리에서 API 5개를 한 번 저장하고 입력을 초기화한다',async()=>{
 const m=api(insecure());const text=fs.readFileSync(new URL('group-api-list-page.tsx',import.meta.url),'utf8');const ast=ts.createSourceFile('group.tsx',text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 function find(name){let found;function walk(node){if(ts.isFunctionDeclaration(node)&&node.name?.text===name)found=node;ts.forEachChild(node,walk);}walk(ast);assert.ok(found);return found.getText(ast);}
 const js=ts.transpileModule(['apiIdentity','mergeApisByApiKey','handleUploadFile'].map(find).join('\n'),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 const saved=[],errors=[],ref={current:{value:'chosen'}};
 const context={crypto:insecure(),createApiAssetId:m.createApiAssetId,parseApiImportPayload:m.parseApiImportPayload,canWriteApi:true,apis:[],copy:{uploaded:'uploaded'},formatApiManagementText:x=>x,setSaving:()=>{},setMessage:()=>{},setErrorMessage:x=>errors.push(x),saveApis:async a=>saved.push(a),fileInputRef:ref};
 const fn=vm.runInNewContext(js+';handleUploadFile',context);await fn(new Blob(['\uFEFF',JSON.stringify(payload())]));assert.equal(saved.length,1);assert.equal(saved[0].length,5);assert.equal(saved[0][0].apiKey,'stable-key-0');assert.equal(ref.current.value,'');assert.equal(errors.filter(Boolean).length,0);
});
