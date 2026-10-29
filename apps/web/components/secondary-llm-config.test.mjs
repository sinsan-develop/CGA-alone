import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
function subject(name) {
  const source = ts.createSourceFile("settings.ts", fs.readFileSync(new URL("../lib/bot-settings.ts", import.meta.url), "utf8"), ts.ScriptTarget.Latest, true);
  const node = source.statements.find(row => ts.isFunctionDeclaration(row) && row.name?.text === name);
  assert.ok(node, `${name} 설정 정규화 함수 필요`);
  const code = ts.transpileModule(node.getText(source).replace(/^export /, ""), {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  return vm.runInNewContext(`${code}; ${name}`);
}
test("미구성 초안은 빈 모듈 ID로 보존한다", () => {
  const normalize = subject("normalizeSecondaryLlmConfig");
  assert.equal(normalize(null).module_id, "");
  assert.equal(normalize({}).module_id, "");
});
test("표시명이 아닌 안정 모듈 ID만 저장하며 다른 필드를 섞지 않는다", () => {
  const normalize = subject("normalizeSecondaryLlmConfig");
  const result = normalize({module_id:"module-1", name:"변경 전 이름", secret:"fixture"});
  assert.deepEqual(Object.keys(result), ["module_id"]);
  assert.equal(result.module_id, "module-1");
});
