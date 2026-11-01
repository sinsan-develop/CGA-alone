// Teams 게시용 표현만 만든다. 기존 Adaptive Card 정규화를 재사용하며 네트워크는 없다.
import { normalizeAidotAdaptiveCard } from "./adaptive-card.ts";
const record=value=>value&&typeof value==="object"&&!Array.isArray(value)?value:{};
const valueText=value=>value===null||value===undefined?"":typeof value==="object"?JSON.stringify(value):String(value);
function url(value){const raw=valueText(value);const parsed=new URL(raw);if(!["https:","http:"].includes(parsed.protocol))throw Error("URL");return raw;}
function block(value){return {type:"TextBlock",text:valueText(value),wrap:true};}

export function buildAidotTeamsReplyPayload(message){
 const payload=record(message.payload),type=message.messageType||"text",text=valueText(message.text);
 const result={type:"message",text,textFormat:"plain"};
 if(type==="text"){if(!text||Buffer.byteLength(JSON.stringify(result))>24000)throw Error("text");return result;}
 const id=valueText(message.id),node=valueText(payload.sourceTalkNodeId);
 const metadata={webchatRichFormVersion:"1.0",sourceMessageId:id,sourceTalkNodeId:node};
 function submit(title,response={},data={}){if(!id||!node)throw Error("source");return {type:"Action.Submit",title:valueText(title),data:{...record(data),...metadata,response}};}
 function choices(items,body,actions){
  if(!items.length)throw Error("choices");
  if(items.length<=6){actions.push(...items.map(item=>submit(item.label,{buttonValue:valueText(item.value)})));return;}
  body.push({type:"Input.ChoiceSet",id:"__cga_buttonValue",style:"compact",isRequired:true,choices:items.map(item=>({title:valueText(item.label),value:valueText(item.value)}))});
  actions.push(submit("선택"));
 }
 let body=[block(text)],actions=[],card;
 if(type==="button"||type==="link-button"){
  if(!Array.isArray(payload.options)||!payload.options.length)throw Error("options");
  const selected=[];
  for(const option of payload.options){const item=record(option),label=item.label??item.text??item.value??option;
   if(type==="link-button"||item.url)actions.push({type:"Action.OpenUrl",title:valueText(label),url:url(item.url)});
   else selected.push({label,value:item.value??item.messageText??label});
  }
  if(selected.length)choices(selected,body,actions);
 }else if(type==="card"){
  const item=record(payload.card);if(item.title)body.push({...block(item.title),weight:"Bolder"});if(item.description)body.push(block(item.description));if(item.imageUrl)body.push({type:"Image",url:url(item.imageUrl),altText:valueText(item.title)});
 }else if(type==="table"){
  if(!Array.isArray(payload.columns)||!payload.columns.length||!Array.isArray(payload.rows)||!payload.rows.length)throw Error("table");
  const columns=payload.columns,rows=payload.rows;
  for(const row of rows){if(!row||typeof row!=="object"||Array.isArray(row))throw Error("row");body.push({type:"FactSet",facts:columns.map(column=>({title:valueText(column),value:valueText(row[column])}))});}
  if(payload.selectable!==false)choices(rows.map(row=>({label:row[columns[0]],value:payload.keyColumn&&row[payload.keyColumn]!==undefined?row[payload.keyColumn]:row[columns[0]]})),body,actions);
 }else if(type==="carousel"){
  const item=record(payload.carousel);for(const field of ["title","itemTitle","itemContents"])if(item[field])body.push(block(item[field]));if(item.imageUrl)body.push({type:"Image",url:url(item.imageUrl),altText:valueText(item.itemTitle||item.title)});
  if(String(item.buttonType).toLowerCase()==="button")actions.push(submit(item.itemButtonLabel,{buttonValue:valueText(item.itemButtonValue)}));
  if(item.bottomButtonLabel||item.bottomButtonValue)actions.push(submit(item.bottomButtonLabel,{buttonValue:valueText(item.bottomButtonValue)}));
  result.attachmentLayout="carousel";
 }else if(type==="form-a-card"){
  card=normalizeAidotAdaptiveCard(payload.adaptiveCard??payload.rawJson);if(!card)throw Error("card");
 }else throw Error("Teams 표현 미지원");
 if(!card)card=normalizeAidotAdaptiveCard({type:"AdaptiveCard",version:"1.6",body,actions});
 // 기존 원문 data/input을 보존하고 원본 메시지/노드 식별자는 서버 값으로 고정한다.
 function bind(value){
  if(Array.isArray(value))return value.map(bind);
  if(!value||typeof value!=="object")return value;
  const copy=Object.fromEntries(Object.entries(value).map(([key,child])=>[key,bind(child)]));
  if(typeof copy.type==="string"&&copy.type.startsWith("Input.")&&typeof copy.id==="string")copy.id="__cga_input_"+Buffer.from(copy.id,"utf8").toString("base64url");
  if(copy.type==="Action.Submit"||copy.type==="Action.Execute"){
   const data=typeof copy.data==="string"?{response:{buttonValue:copy.data}}:record(copy.data);
   copy.data={...data,...metadata,response:type==="form-a-card"?data:record(data.response)};
  }
  if(copy.type==="Action.OpenUrl")copy.url=url(copy.url);
  if(copy.type==="Image"&&copy.url)copy.url=url(copy.url);
  return copy;
 }
 result.attachments=[{contentType:"application/vnd.microsoft.card.adaptive",content:bind(card)}];
 if(Buffer.byteLength(JSON.stringify(result))>24000)throw Error("Teams 카드 크기");
 return result;
}
