"""Editor integration through public APIs where exposed, with native input fallback.

Readback is only a fill check. The captured request and independent permalink must
still contain the content before either browser or HTTP publication is verified.
"""

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class EditorAdapter:
    name: str
    revision: int = 1

    async def fill(self, locator, value, *, staged=False):
        accepted = False
        if self.name != "native":
            accepted = await locator.evaluate(
                r"""async (e,arg) => {
              const [name,text,markup]=arg, w=e.ownerDocument.defaultView;
              if(name==='tinymce') {
                const host=w.frameElement, tm=typeof w.tinymce?.get==='function'?w.tinymce:w.parent?.tinymce;
                const editor=tm?.get(host?.id?.replace(/_ifr$/,'')||e.id);
                if(editor){editor.setContent(markup);editor.save();return true;}
              }
              if(name==='ckeditor') {
                const instance=e.ckeditorInstance||e.closest('[data-ckeditor]')?.ckeditorInstance;
                if(instance?.setData){instance.setData(markup);return true;}
                const ck=w.CKEDITOR||w.parent?.CKEDITOR;
                for(const instance of Object.values(ck?.instances||{})) {
                  if(instance.editable?.()?.$===e){
                    if(instance.status==='ready') await Promise.race([
                      new Promise(resolve=>instance.setData(markup,{callback:resolve})),
                      new Promise((_,reject)=>setTimeout(()=>reject(new Error('EDITOR_SYNC')),5000))]);
                    else instance.setData(markup);
                    instance.updateElement();return true;}
                }
              }
              if(name==='quill') {
                const instance=e.closest('.ql-container')?.__quill || w.Quill?.find?.(e.closest('.ql-container'));
                if(instance?.setText){instance.setText(text);return true;}
              }
              if(name==='prosemirror') {
                const view=e.pmViewDesc?.view;
                if(view?.state && view.dispatch){view.dispatch(view.state.tr.insertText(text,0,view.state.doc.content.size));return true;}
              }
              if(name==='summernote' && w.jQuery?.fn?.summernote){
                const host=e.closest('.note-editor')?.previousElementSibling||e;
                w.jQuery(host).summernote('code',markup);return true;}
              if(name==='froala') {
                const instance=e['data-froala.editor']||w.jQuery?.(e)?.data?.('froala.editor');
                if(instance?.html?.set){instance.html.set(markup);instance.events.trigger('contentChanged');return true;}
              }
              return false;
            }""",
                [self.name, value, html.escape(value, quote=False).replace("\n", "<br>")],
            )
        if not accepted:
            if staged and not await locator.is_visible():
                await locator.evaluate(
                    "(e,v)=>Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,v)",
                    value,
                )
            else:
                await locator.fill(value)
        await locator.evaluate("""e => {
          e.dispatchEvent(new InputEvent('input',{bubbles:true,composed:true,inputType:'insertText'}));
          e.dispatchEvent(new Event('change',{bubbles:true}));
          e.dispatchEvent(new FocusEvent('blur',{bubbles:true}));
        }""")
        actual = await locator.evaluate("e=>e.isContentEditable?e.innerText:e.value")
        if " ".join(str(actual).split()) != " ".join(value.split()):
            raise ValueError("EDITOR_SYNC")


EDITORS = {
    name: EditorAdapter(name)
    for name in ("native", "tinymce", "ckeditor", "quill", "prosemirror", "summernote", "froala")
}


def adapter_for(ref):
    return EDITORS.get(ref.editor, EDITORS["native"])
