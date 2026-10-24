"""DOM actions only: no request bodies, cookies or generated plaintext code files."""

SELECTOR_JS = r"""element => {
 const root=element.getRootNode(), query=s=>{try{return root.querySelectorAll(s).length===1}catch{return false}};
 let local='';
 if(element.id && query('#'+CSS.escape(element.id))) local='#'+CSS.escape(element.id);
 for(const attr of ['data-testid','data-test','name','aria-label']) {
   if(local) break;
   const value=element.getAttribute(attr);
   if(value) {const s=element.tagName.toLowerCase()+'['+attr+'='+JSON.stringify(value)+']';if(query(s))local=s;}
 }
 if(!local) {
   const parts=[];let e=element;
   while(e && e.nodeType===1) {
     const siblings=e.parentElement?[...e.parentElement.children].filter(x=>x.tagName===e.tagName):[e];
     parts.unshift(e.tagName.toLowerCase()+':nth-of-type('+(siblings.indexOf(e)+1)+')');e=e.parentElement;
   }
   local=parts.join(' > ');
 }
 if(root.host) return ('('+SELECTOR+')')(root.host)+' '+local;
 return local;
}"""
# Replace the recursive expression with a named function at injection/evaluation time.
SELECTOR_FUNCTION = SELECTOR_JS.replace("('('+SELECTOR+')')(root.host)", "selector(root.host)")
SELECTOR_EXPRESSION = "element => { const selector=" + SELECTOR_FUNCTION + ";return selector(element); }"

RECORD_JS = r"""(() => {
 if(window.__posterMakerInstalled) return;
 window.__posterMakerInstalled=true;
 window.__posterMakerDocumentId=globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+Math.random().toString(36);
 const selector=SELECTOR_FUNCTION;
 let active=true, sequence=0;
 const pending=new Map(), protectedFields=new WeakSet(), sent=new Set();
 function sensitive(e) {
   const yes=e.type==='password'||protectedFields.has(e)||/password|passwd|pwd|secret|token|one.time.code/i.test([e.name,e.id,e.autocomplete].join(' '));
   if(yes) protectedFields.add(e);return yes;
 }
 const track=e=>{if(e?.matches?.('input[type=password]'))protectedFields.add(e);e?.querySelectorAll?.('input[type=password]').forEach(x=>protectedFields.add(x));};
 new MutationObserver(items=>items.forEach(m=>{
   if(m.type==='attributes' && m.oldValue==='password') protectedFields.add(m.target);
   m.addedNodes.forEach(track);
 })).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['type'],attributeOldValue:true});
 function emit(kind,e,more={}) {
   if(!active) return Promise.resolve();
   const data={kind,selector:e?selector(e):'',document_id:window.__posterMakerDocumentId,sequence:++sequence,...more};
   const p=window.__poster_maker_action(data).catch(()=>{});sent.add(p);p.finally(()=>sent.delete(p));return p;
 }
 function flush(e) {
   if(!pending.has(e))return Promise.resolve();
   clearTimeout(pending.get(e));pending.delete(e);
   return emit('input',e,{value:e.isContentEditable?e.textContent:e.value,sensitive:sensitive(e)});
 }
 const flushAll=()=>Promise.all([...pending.keys()].map(flush));
 window.__posterMakerFlush=async()=>{await flushAll();await Promise.all([...sent]);};
 window.__posterMakerStop=async()=>{await window.__posterMakerFlush();active=false;};
 window.__posterMakerStart=()=>{active=true;};
 document.addEventListener('input',event=>{
   if(!active||!event.isTrusted||window.__posterMakerPicking)return;
   const e=event.composedPath()[0];
   if(!e?.matches?.('input:not([type=hidden]):not([type=checkbox]):not([type=radio]),textarea,[contenteditable=true]'))return;
   sensitive(e);clearTimeout(pending.get(e));pending.set(e,setTimeout(()=>flush(e),150));
 },true);
 document.addEventListener('click',event=>{
   if(!active||!event.isTrusted||window.__posterMakerPicking)return;
   const original=event.composedPath()[0];if(!original?.tagName)return;
   const e=original.closest('button,a,input,textarea,select,[role=button],label')||original;
   if(e.tagName==='SELECT'||(e.tagName==='LABEL'&&e.control))return;
   flushAll();emit('click',e,{button:['left','middle','right'][event.button]||'left'});
 },true);
 document.addEventListener('change',event=>{
   if(!active||!event.isTrusted||window.__posterMakerPicking)return;
   const e=event.composedPath()[0];
   if(e?.tagName==='SELECT'){flushAll();emit('select',e,{values:[...e.selectedOptions].map(o=>o.value)});}
 },true);
 document.addEventListener('focusout',event=>{if(active)flush(event.composedPath()[0]);},true);
})();""".replace("SELECTOR_FUNCTION", SELECTOR_FUNCTION)
