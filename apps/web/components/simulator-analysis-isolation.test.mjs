import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import ts from "typescript";
const source=readFileSync(new URL("./simulator-page.tsx",import.meta.url),"utf8");
const tree=ts.createSourceFile("simulator.tsx",source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
function find(predicate){let result;function visit(node){if(predicate(node))result=node;ts.forEachChild(node,visit);}visit(tree);assert.ok(result);return result;}
const compile=text=>ts.transpileModule(text,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
test("shared room never displays personal analysis, including late preview completion",()=>{
 const expression=find(n=>ts.isVariableDeclaration(n)&&n.name.getText(tree)==="analysisVisible").initializer.getText(tree);
 const visible=new Function("sharedMode","embedded","analysisOpen",`return (${expression});`);
 for(const embedded of [true,false])for(const analysisOpen of [true,false]){
  assert.equal(visible(true,embedded,analysisOpen),false);
  assert.equal(visible(false,embedded,analysisOpen),!embedded||analysisOpen);
 }
});
for(const sharedMode of [true,false])test(`Adaptive error preserves analysis ownership: shared=${sharedMode}`,()=>{
 const declaration=find(n=>ts.isFunctionDeclaration(n)&&n.name?.text==="handleAdaptiveCardRenderError");
 const logs=[],analyses=[],variables={preview:"kept"};
 const handler=new Function("sharedMode","writeSimulatorLog","selectedAnalysisId","analyses","appendAnalysisLog","runtime","formatStudioRuntimeMessage","uiLanguage",compile(declaration.getText(tree))+";return handleAdaptiveCardRenderError;")(sharedMode,(...args)=>logs.push(args),"preview-id",[{id:"preview-id"}],(...args)=>analyses.push(args),{variables},(_lang,_text,args)=>args.error,"ko");
 handler({id:"message",sourceTalkNodeId:"talk",template:{title:"Card"}},"invalid card");
 assert.equal(logs.length,1);
 assert.equal(analyses.length,sharedMode?0:1);
 if(!sharedMode){assert.equal(analyses[0][0],"preview-id");assert.deepEqual(analyses[0][2],variables);}
});
