// Minimal DOM shim: renders the editor side panels in Node and checks grouping,
// rename cascades with one-step undo, and that no "null"/"undefined" text shows
class Node_ { constructor(){ this.childNodes=[]; this.parentNode=null; this.listeners={}; } 
  append(...ns){ for(const n of ns){ const c = (n instanceof Node_) ? n : new Text_(String(n)); c.parentNode=this; this.childNodes.push(c);} }
  prepend(n){ n.parentNode=this; this.childNodes.unshift(n); }
  replaceChild(a,b){ const i=this.childNodes.indexOf(b); this.childNodes[i]=a; a.parentNode=this; }
  remove(){ if(this.parentNode){ const c=this.parentNode.childNodes; c.splice(c.indexOf(this),1);} }
  addEventListener(t,f){ (this.listeners[t]=this.listeners[t]||[]).push(f); }
  removeEventListener(){}
  get textContent(){ return this.childNodes.map(c=>c.textContent).join(""); }
  set textContent(v){ this.childNodes=[new Text_(String(v))]; }
  get childElementCount(){ return this.childNodes.filter(c=>c instanceof El).length; }
}
class Text_ extends Node_ { constructor(t){ super(); this.data=t; this.nodeType=3; } get textContent(){return this.data;} }
class El extends Node_ { constructor(tag){ super(); this.tagName=tag.toUpperCase(); this.nodeType=1; this.attrs={}; this.style={}; this.dataset={}; this.value=""; this.checked=false;
  const self=this; this.classList={ add(c){ self.className=((self.className||"")+" "+c).trim(); }, remove(c){ self.className=(self.className||"").split(" ").filter(x=>x!==c).join(" "); }, toggle(c,on){ const has=(self.className||"").split(" ").includes(c); if(on===undefined) on=!has; on?this.add(c):this.remove(c);} , contains(c){ return (self.className||"").split(" ").includes(c);} }; }
  setAttribute(k,v){ this.attrs[k]=v; if(k==="value") this.value=v; if(k==="checked") this.checked=true; }
  getAttribute(k){ return this.attrs[k]; }
  set innerHTML(v){ this.childNodes=[]; } get innerHTML(){ return ""; }
  get options(){ return this.childNodes.filter(c=>c.tagName==="OPTION"); }
  querySelector(){ return null; } querySelectorAll(){ return []; }
  focus(){} click(){} getBoundingClientRect(){ return {left:0,top:0,width:800,height:600}; }
  getContext(){ return new Proxy({}, { get:(t,k)=> k==="measureText"? (()=>({width:10})) : (typeof k==="string" && !["fillStyle","strokeStyle","lineWidth","font","imageSmoothingEnabled"].includes(k) ? ()=>{} : t[k]), set:(t,k,v)=>{t[k]=v; return true;} }); }
  get isConnected(){ return true; } matches(){ return false; }
}
globalThis.Node = Node_; Node_.TEXT_NODE = 3;
globalThis.document = { createElement:(t)=>new El(t), createTextNode:(t)=>new Text_(t), getElementById:()=>new El("div"), body:new El("body"), addEventListener(){}, removeEventListener(){}, querySelectorAll:()=>[] };
globalThis.window = { addEventListener(){}, devicePixelRatio:1 };
globalThis.ResizeObserver = class { observe(){} disconnect(){} };
globalThis.localStorage = { getItem:()=>null, setItem(){} };
globalThis.CSS = { escape:(s)=>s };
globalThis.confirm = () => true; globalThis.prompt = () => "renamed";
const root = process.argv[2];
const api = await import(`${root}/api.js`);
const responses = { models: { models: [{ path: "m.onnx", name: "m.onnx", kind: "bubble", location: "template" }] } };
globalThis.fetch = async (path) => ({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => (path.includes("models") ? responses.models : {}) });
api.state.caps = { field_types: { QTYPE_INT: { bubbleValues: ["0","1","2","3","4","5","6","7","8","9"], direction: "vertical" } }, zone_types: ["barcode","qrcode","ocr","icr"], barcode_formats: ["Code128"] };
const { TemplateEditor } = await import(`${root}/editor.js`);
const ed = new TemplateEditor(new El("div"), { onClose(){} });
ed.detail = { name: null, status: "ready", template: {} };
ed.id = "t1";
ed.doc = {
  pageDimensions: [800, 600], bubbleDimensions: [20, 20], preProcessors: [{ name: "TimingMarkAlignment", options: { tracks: { left: { marks: [[1,2],[1,5]] } }, markDimensions: [20,10] } }],
  fieldBlocks: { Roll: { fieldType: "QTYPE_INT", fieldLabels: ["roll1..5"], origin: [10,10], bubblesGap: 30, labelsGap: 30 }, Roll2: { fieldType: "QTYPE_INT", fieldLabels: ["r6..7"], origin: [300,10], bubblesGap: 30, labelsGap: 30 } },
  zones: { bc: { type: "barcode", origin: [400,400], dimensions: [100,40], options: { fallbackZone: "sr" } }, sr: { type: "ocr", origin: [400,450], dimensions: [100,40], options: { psm: 11 } } },
  customLabels: { Roll: ["roll1..6"] }, validate: { Roll: { length: 6 } },
  checks: [{ name: "c1", sources: ["bc", "sr"], normalize: { regex: "0*([0-9]+)", group: 1 } }],
};
ed.configText = JSON.stringify({ review_params: { min_confidence: 0.4 } }); ed.savedConfigText = ed.configText; ed.evaluationText = ""; ed.savedEvaluationText = "";
ed.errors = []; ed.report = null;
ed.build();
const walk = (n, out=[]) => { if (n.tagName) out.push(n); for (const c of n.childNodes) walk(c, out); return out; };
const text = () => ed.side.textContent;
if (!text().includes("Missing column(s)") && !text().includes("roll6 not defined")) throw new Error("broken group not shown");
for (const sel of [{kind:"block",name:"Roll"},{kind:"zone",name:"bc"},{kind:"zone",name:"sr"}]) { ed.selected = sel; for (const k of ["validate:roll1,roll2,roll3,roll4,roll5","validate:bc","barcode-adv:bc","group-block:Roll"]) ed.openSections.add(k); ed.renderSide(); const t = text(); const i = t.search(/null|undefined/); if (i >= 0) throw new Error(sel.name + ": " + t.slice(i - 80, i + 30)); }
// grouping a block
ed.doc.customLabels = {}; delete ed.doc.validate;
ed.selected = {kind:"block",name:"Roll2"}; ed.renderSide();
const cb = walk(ed.side).find((n) => n.tagName === "INPUT" && n.attrs.type === "checkbox" && n.parentNode.textContent.includes("Output as one field"));
cb.checked = true; cb.listeners.change[0]();
if (!ed.doc.customLabels.r) throw new Error("block not grouped: " + JSON.stringify(ed.doc.customLabels));
if (JSON.stringify(ed.doc.validate.r) !== JSON.stringify({ required: true, allowGaps: false, length: 2 })) throw new Error("validation not offered");
// field labels rename cascades
await ed.setFieldLabels(ed.blockInfo("Roll2"), ["rr6..7"]);
if (JSON.stringify(ed.doc.customLabels.r) !== '["rr6..7"]') throw new Error("rename cascade failed " + JSON.stringify(ed.doc.customLabels));
ed.doUndo(); if (JSON.stringify(ed.doc.customLabels.r) !== '["r6..7"]') throw new Error("undo is not one step");
// multi selection
ed.selected = null; ed.multi = [{kind:"block",name:"Roll"},{kind:"block",name:"Roll2"}]; ed.renderSide();
if (!text().includes("Group as one field")) throw new Error("multi panel missing");
ed.multi = []; ed.selected = null; ed.renderSide();
for (const k of ["review","alignment","models","pdf","output","barcode-defaults"]) ed.openSections.add(k);
ed.testResult = { file_id: "x", status: "ok", fields: { roll1: { bubbles: [{ fill_ratio: 0.8, marked: true }, { fill_ratio: 0.1, marked: false }] } } };
ed.renderSide();
await new Promise((r) => setTimeout(r, 10));
ed.renderSide();
if (!text().includes("m.onnx")) throw new Error("model picker missing");
if (text().includes("null") || text().includes("undefined")) throw new Error("null/undefined rendered: " + text().slice(text().search(/null|undefined/) - 80, text().search(/null|undefined/) + 40));
if (ed.titleEl.textContent !== "t1") throw new Error("title null");
console.log("editor panels ok");
