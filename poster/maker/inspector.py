"""Browser-side element picker and selector checks, without GUI dependencies."""

import re

from .actions import Target
from .model import GraphError
from .navigation import safe_url
from .record_script import SELECTOR_EXPRESSION, SELECTOR_FUNCTION

PICKER_JS = r"""(() => {
 if(window.__posterMakerPickerInstalled)return;
 window.__posterMakerPickerInstalled=true;
 const selector=SELECTOR;
 let outlined=null, previous='';
 function clear(){if(outlined){outlined.style.outline=previous;outlined=null;}}
 window.__posterMakerPickStop=()=>{window.__posterMakerPicking=false;clear();};
 document.addEventListener('mousemove',event=>{
   if(!window.__posterMakerPicking)return;
   const e=event.composedPath()[0];if(!e?.style||e===outlined)return;
   clear();outlined=e;previous=e.style.outline;e.style.outline='3px solid #f97316';
 },true);
 function xpath(e){
   const parts=[];
   while(e&&e.nodeType===1){
     let index=1, p=e.previousElementSibling;
     while(p){if(p.tagName===e.tagName)index++;p=p.previousElementSibling;}
     parts.unshift(e.tagName.toLowerCase()+'['+index+']');e=e.parentElement;
   }
   return '/'+parts.join('/');
 }
 document.addEventListener('click',event=>{
   if(!window.__posterMakerPicking||!event.isTrusted)return;
   event.preventDefault();event.stopImmediatePropagation();
   const e=event.composedPath()[0];
   const attributes={};
   for(const attr of e.attributes||[]){
     attributes[attr.name]=/value|pass|token|secret|cookie|authorization/i.test(attr.name)?'[hidden]':attr.value.slice(0,300);
   }
   const data={css:selector(e),xpath:'xpath='+xpath(e),tag:e.tagName.toLowerCase(),attributes};
   window.__posterMakerPickStop();window.__poster_maker_pick(data).catch(()=>{});
 },true);
 document.addEventListener('keydown',event=>{if(event.key==='Escape'){
   window.__posterMakerPickStop();window.__poster_maker_pick({cancel:true}).catch(()=>{});
 }},true);
})();""".replace("SELECTOR", SELECTOR_FUNCTION)


class Inspector:
    def __init__(self, session, on_pick=None):
        self.session = session
        self.on_pick = on_pick or (lambda data: None)

    async def picked(self, source, data):
        if data.get("cancel"):
            await self.disable()
            return
        frame = source["frame"]
        path = []
        while frame.parent_frame:
            path.insert(0, await (await frame.frame_element()).evaluate(SELECTOR_EXPRESSION))
            frame = frame.parent_frame
        data["frame"] = path
        # Filter again across the binding boundary; never trust page-supplied attributes.
        data["attributes"] = {
            str(k)[:100]: "[hidden]"
            if re.search(r"value|pass|token|secret|cookie|authorization", k, re.I)
            else str(v)[:300]
            for k, v in data.get("attributes", {}).items()
        }
        for key in ("href", "src", "action", "formaction"):
            if key in data["attributes"]:
                data["attributes"][key] = safe_url(data["attributes"][key])
        await self.disable()
        self.on_pick(data)

    async def enable(self):
        if not self.session.page or self.session.page.is_closed():
            raise GraphError("TAB_MISSING")
        session = self.session
        session.inspector_target = self
        if not getattr(session, "inspector_binding", False):
            await session.context.expose_binding(
                "__poster_maker_pick", lambda source, data: session.inspector_target.picked(source, data)
            )
            await session.context.add_init_script(PICKER_JS)
            session.inspector_binding = True
        for frame in session.page.frames:
            await frame.evaluate(PICKER_JS)
            await frame.evaluate("window.__posterMakerPicking=true")
        await session.page.bring_to_front()

    async def disable(self):
        for page in self.session.context.pages if self.session.context else []:
            for frame in page.frames:
                try:
                    await frame.evaluate("window.__posterMakerPickStop?.()")
                except Exception:
                    pass

    async def check(self, selector, frames=()):
        target = Target(selector=selector, frame=list(frames))
        root = self.session.page
        if not root or root.is_closed():
            raise GraphError("TAB_MISSING")
        for item in target.frame:
            root = root.frame_locator(item)
        found = root.locator(target.selector)
        count = await found.count()
        if count > 200:
            raise GraphError("SELECTOR_TOO_BROAD")
        if count:
            await found.first.scroll_into_view_if_needed(timeout=3000)
        await found.evaluate_all("""elements=>{
            for(const e of document.querySelectorAll('[data-poster-maker-highlight]')){
                e.style.outline=e.dataset.posterMakerHighlight;e.removeAttribute('data-poster-maker-highlight');
            }
            for(const e of elements){e.dataset.posterMakerHighlight=e.style.outline;e.style.outline='3px solid #16a34a';}
        }""")
        return {"count": count, "selector": selector, "frame": list(frames)}
