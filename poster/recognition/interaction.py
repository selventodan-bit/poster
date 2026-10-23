"""Attribute the first JS mutation to the selected click, not to a timing window.

The private header is removed by the browser gate before any network operation.
Timers and unrelated buttons never inherit the click. Async follow-up operations
still require the observed response dependency in SubmissionScope.
"""

ACTION_HEADER = "x-poster-interaction"

INTERACTION_JS = r"""(() => {
 let active = null, selected = null, ticket = null, lastButton=null;
 window.__posterNativeReceipt=null;
 const submit = e => {
   active = e.isTrusted && lastButton===e.submitter && e.submitter?.matches(selected) ? ticket : null;
   if(active) window.__poster_native_submit(active,new URL(e.submitter.getAttribute('formaction')||e.target.getAttribute('action')||location.href,document.baseURI).href);
 };
 window.__posterArm = (selector, value) => {selected=selector;ticket=value;active=null;};
 window.addEventListener('click', e => {
   active = e.isTrusted && selected && e.composedPath().some(n=>n.matches?.(selected)) ? ticket : null;
   lastButton=active?e.composedPath().find(n=>n.matches?.(selected)):null;
   // submit does not cross a shadow boundary. Bind the actual owned form too.
   if(lastButton?.form) lastButton.form.addEventListener('submit',submit,{capture:true,once:true});
   setTimeout(()=>{lastButton=null;},0);
 }, true);
 window.addEventListener('click', () => {active=null;});
 window.addEventListener('submit', submit, true);
 window.addEventListener('submit', () => {active=null;});
 const originalFetch=window.fetch;
 window.fetch=function(input, init) {
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url, location.href);
   let captureId=null;
   if(url.origin===location.origin) {
     init={...init,headers:new Headers(init?.headers || (input instanceof Request?input.headers:undefined))};
     if(active) init.headers.set('x-poster-interaction',active);
     captureId=crypto.randomUUID();init.headers.set('x-poster-capture',captureId);
   }
   return originalFetch.call(this,input,init).then(async response=>{
     // Navigation can destroy Chromium's response-body handle immediately after
     // an SPA reads JSON. Observe a bounded clone before returning that response.
     if(captureId && window.__poster_response_body) {
       try {
         const reader=response.clone().body?.getReader();let size=0,parts=[];
         if(reader) while(true) {
           const {done,value}=await reader.read();if(done) break;
           size+=value.length;if(size>2000000) {await reader.cancel();parts=[];break;}
           parts.push(value);
         }
         if(parts.length) {
           const bytes=new Uint8Array(size);let at=0;
           for(const p of parts){bytes.set(p,at);at+=p.length;}
           await window.__poster_response_body(captureId,new TextDecoder().decode(bytes));
         }
       } catch(_) {}
     }
     return response;
   });
 };
 const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
 const addresses=new WeakMap();
 XMLHttpRequest.prototype.open=function(method,url,...rest) {
   addresses.set(this,new URL(url,location.href));return open.call(this,method,url,...rest);
 };
 XMLHttpRequest.prototype.send=function(body) {
   if(active && addresses.get(this)?.origin===location.origin) this.setRequestHeader('x-poster-interaction',active);
   return send.call(this,body);
 };
})();"""
