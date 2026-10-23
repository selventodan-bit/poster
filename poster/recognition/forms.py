"""Relational field analysis across light DOM, open shadow DOM and allowed iframe editors."""

import re
from urllib.parse import urlsplit

from .localization import Localizations
from .types import FieldRef, FormCandidate

SNAPSHOT_JS = r"""() => {
 const all=root=>[...root.querySelectorAll('*')].flatMap(e=>[e,...(e.shadowRoot?all(e.shadowRoot):[])]);
 const elements=all(document);
 for(const e of elements) for(const attr of ['data-poster-field','data-poster-submit','data-poster-form']) e.removeAttribute(attr);
 const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
 const roots=elements.filter(e=>e.matches('form,[role="form"]'));
 for(const e of elements.filter(e=>e.matches('textarea,[contenteditable="true"],[role="textbox"]'))) {
   if(!roots.some(r=>r.contains(e))) {
     let root=e.closest('section,article,dialog,[role="dialog"],fieldset');
     if(!root) for(let p=e.parentElement,depth=0;p&&p!==document.body&&depth<6;p=p.parentElement,depth++) {
       if(p.querySelector('input[type="text"],input:not([type])')&&p.querySelector('button,[role="button"]')) {root=p;break;}
     }
     root=root||e.parentElement;
     if(root&&!roots.includes(root)) roots.push(root);
   }
 }
 return roots.map((root,i)=>{
  const key='form-'+i;root.setAttribute('data-poster-form',key);
  const descendants=[root,...all(root)];
  const controls=descendants.filter(e=>e.matches('input,textarea,select,[contenteditable="true"],[role="textbox"]'));
  const editor=controls.find(e=>e.matches('textarea,[contenteditable="true"],[role="textbox"]'));
  const fields=controls.map((e,j)=>{
   const id=key+'-field-'+j;e.setAttribute('data-poster-field',id);
   const accessibility=[e.getAttribute('aria-label'),e.getAttribute('placeholder'),...[...(e.labels||[])].map(n=>n.innerText),
    ...(e.getAttribute('aria-labelledby')||'').split(' ').map(id=>e.getRootNode().getElementById?.(id)?.textContent)].filter(Boolean).join(' ');
   const classes=e.className||'';
   let adapter=/mce-content-body/.test(classes)?'tinymce':/ck-editor|cke_editable/.test(classes)?'ckeditor':
    /ql-editor/.test(classes)?'quill':/ProseMirror|tiptap/.test(classes)?'prosemirror':
    /note-editable/.test(classes)?'summernote':/fr-element/.test(classes)?'froala':'native';
   return {selector:e.tagName==='BODY'&&e.isContentEditable?'body[contenteditable="true"]':'[data-poster-field="'+id+'"]',label:[e.name,e.id,accessibility].filter(Boolean).join(' ').slice(0,500),
    accessibility:accessibility.slice(0,400),name:e.name||'',tag:e.tagName.toLowerCase(),type:e.type||'',
    editable:e.isContentEditable,visible:visible(e),adapter,
    before_editor:!!editor && e!==editor && !!(e.compareDocumentPosition(editor)&Node.DOCUMENT_POSITION_FOLLOWING),
    required:e.required||e.getAttribute('aria-required')==='true',maxlength:e.maxLength||0};
  });
  const buttons=descendants.filter(e=>e.matches('button,input[type="submit"],[role="button"]')).filter(visible).map((e,j)=>{
   const id=key+'-submit-'+j;e.setAttribute('data-poster-submit',id);
   return {selector:'[data-poster-submit="'+id+'"]',text:[e.innerText,e.value,e.getAttribute('aria-label'),e.title].filter(Boolean).join(' ').slice(0,300),
    type:e.type||'',tag:e.tagName.toLowerCase(),form_owned:e.form===root,
    action:[e.name,e.getAttribute('data-action'),e.getAttribute('formaction')].filter(Boolean).join(' '),
    editor_relation:!!editor && ((e.getAttribute('aria-controls')||'').split(' ').includes(editor.id) && !!editor.id),
    js_submit:!!e.onclick && /fetch\s*\(|\.send\s*\(|requestSubmit\s*\(/.test(String(e.onclick)),
    component:/composer|editor|submit|primary/i.test([root.className,e.className,e.getAttribute('data-action')].join(' ')),
    endpoint:e.getAttribute('data-endpoint')||e.getAttribute('data-url')||''};
  });
  return {key,tag:root.tagName.toLowerCase(),action:new URL(root.getAttribute('action')||location.href,root.baseURI).href,method:root.getAttribute('method')||'',fields,buttons,
   publication_kind:root.getAttribute('data-publication-type')||'',
   purpose:[root.getAttribute('aria-label'),root.querySelector('legend,h1,h2,h3')?.textContent].filter(Boolean).join(' ').slice(0,500),
   structure:[root.getRootNode() instanceof ShadowRoot?'shadow':'light',root.matches('dialog,[role="dialog"]')?'dialog':'inline']};
 });
}"""


class UniversalFormDetector:
    version = "relational-2.1"

    def __init__(self, locales=None, publication_kind="auto"):
        self.publication_kind = publication_kind
        self.locales = locales or Localizations()

    async def inspect(self, page, allowed_hosts) -> list[FormCandidate]:
        candidates, iframe_parents = [], {}
        for index, frame in enumerate(page.frames):
            if index:
                try:
                    await frame.wait_for_load_state("domcontentloaded", timeout=3000)
                    owner = await frame.frame_element()
                    iframe_parents[index] = await owner.evaluate(
                        "e=>e.closest('[data-poster-form]')?.getAttribute('data-poster-form')"
                    )
                except Exception:
                    continue
            if index:
                try:
                    # about:srcdoc (TinyMCE) and about:blank inherit their
                    # embedding origin; the URL alone cannot establish trust.
                    inherited = await frame.evaluate("window.origin")
                    if inherited != await page.main_frame.evaluate("window.origin"):
                        continue
                except Exception:
                    continue
            elif urlsplit(frame.url).hostname not in allowed_hosts:
                continue
            try:
                snapshots = await frame.evaluate(SNAPSHOT_JS)
            except Exception:
                continue
            for snapshot in snapshots:
                candidate = self.analyze(snapshot, index)
                if candidate:
                    candidates.append(candidate)
        for child in list(candidates):
            parent = next(
                (c for c in candidates if c.frame == 0 and c.key == f"0:{iframe_parents.get(child.frame)}"),
                None,
            )
            if parent and "body" in child.fields:
                parent.fields["body"] = child.fields["body"]
                parent.fields["body"].kind = "iframe:" + child.fields["body"].kind
                parent.evidence.append("same-origin-iframe-editor")
                parent.confidence = self.confidence(parent.fields)
                candidates.remove(child)
        return sorted(candidates, key=lambda c: c.confidence, reverse=True)

    @staticmethod
    def confidence(fields):
        return round(sum({"title": 0.3, "body": 0.4, "submit": 0.25}.get(r, 0) for r in fields), 2)

    def analyze(self, snapshot, frame=0):
        fields, evidence, alternatives = {}, [], {}
        if re.search(
            r"\b(edit|delete|vote|profile|settings|contact|newsletter|subscribe|login|register|search)\b",
            snapshot.get("purpose", ""),
            re.I,
        ):
            return None
        if re.search(
            r"/(edit|delete|vote|profile|settings|contact|newsletter|subscribe|login|register|search)(?:[./?]|$)",
            urlsplit(snapshot.get("action", "")).path,
            re.I,
        ):
            return None
        visible = [
            f
            for f in snapshot["fields"]
            if f["visible"] and f["type"] not in {"hidden", "submit", "password"}
        ]
        if any(f["visible"] and f["type"] == "password" for f in snapshot["fields"]):
            return None
        text_fields = [
            f
            for f in visible
            if f["tag"] == "input"
            and f["type"] in {"text", ""}
            and not self.locales.has("search", f["label"])
        ]
        for role in ("title", "body", "category", "tags"):
            choices = []
            for f in visible:
                features = []
                score = 0
                single = f["tag"] == "input" and f["type"] in {"text", ""}
                multi = f["editable"] or f["tag"] == "textarea" or f.get("adapter", "native") != "native"
                if role == "title" and not single or role == "body" and not multi:
                    continue
                if f["type"] == "search" or self.locales.has("search", f["label"]):
                    continue
                if role in {"title", "body"}:
                    score = 0.25 if single else 0.65
                    features.append("single-line" if single else "multiline-editor")
                    if self.locales.has(role, f["label"]):
                        score += 0.45
                        features.append("semantic-attributes")
                    if self.locales.has(role, f.get("accessibility", "")):
                        score += 0.15
                        features.append("accessible-label")
                    if role == "title" and f.get("before_editor"):
                        features.append("before-editor")
                    elif role == "title":
                        features.append("after-editor")
                    if role == "title" and len(text_fields) == 1:
                        score += 0.3
                        features.append("unique-title-near-editor")
                else:
                    if re.search(
                        {"category": r"category|forum|categor[ií]a|раздел", "tags": r"tags|etiquetas|теги"}[
                            role
                        ],
                        f["label"],
                        re.I,
                    ):
                        score = 0.8
                        features.append("semantic-attributes")
                if score:
                    choices.append(
                        FieldRef(
                            role,
                            f["selector"],
                            frame,
                            "contenteditable" if f["editable"] else f["tag"],
                            min(0.98, score),
                            text_fields.index(f) if f in text_fields else 0,
                            f.get("adapter", "native"),
                            features,
                        )
                    )
            choices.sort(key=lambda f: f.confidence, reverse=True)
            alternatives[role] = choices
            threshold = 0.5 if role == "title" else 0.6
            if (
                choices
                and choices[0].confidence >= threshold
                and (len(choices) == 1 or choices[0].confidence - choices[1].confidence >= 0.15)
            ):
                fields[role] = choices[0]
                evidence.append("relational-" + role)
        buttons = []
        for b in snapshot["buttons"]:
            # Native forms often have several submit controls: preview, draft and
            # attachment uploads must never compete with actual publication.
            if re.search(r"preview|draft|attachment|upload|cancel", b.get("action", ""), re.I):
                continue
            score = 0.65 if b["type"] == "submit" else 0
            features = ["form-submit-control"] if score else []
            if self.locales.has("publish", b["text"]):
                score += 0.3
                features.append("accessible-publication-intent")
            if (
                "body" in fields
                and b["type"] != "submit"
                and (
                    b.get("editor_relation")
                    and (b.get("js_submit") or b.get("component"))
                    or b.get("js_submit")
                    and b.get("component")
                )
            ):
                score += 0.8
                features.append("editor-controller")
            if score:
                buttons.append(
                    FieldRef("submit", b["selector"], frame, "button", min(0.98, score), features=features)
                )
        buttons.sort(key=lambda b: b.confidence, reverse=True)
        alternatives["submit"] = buttons
        if buttons and (len(buttons) == 1 or buttons[0].confidence - buttons[1].confidence >= 0.15):
            fields["submit"] = buttons[0]
        if not ("body" in fields or "title" in fields):
            return None
        candidate = FormCandidate(
            f"{frame}:{snapshot['key']}",
            fields,
            self.confidence(fields),
            frame,
            snapshot["method"],
            snapshot["action"],
            evidence,
            alternatives=alternatives,
            structure=snapshot.get("structure", []),
        )
        kind = snapshot.get("publication_kind", "")
        action = snapshot.get("action", "")
        if re.search(r"(?:[/=?&;_-])(?:edit|delete|vote|settings|update)(?:[/=?&;_-]|$)", action, re.I):
            return None
        if kind not in {"topic", "reply", "comment"}:
            kind = (
                "comment"
                if re.search(r"(?:[/=?&;_-])comments?(?:[/=?&;_-]|$)", action, re.I)
                else (
                    "reply"
                    if re.search(r"(?:[/=?&;_-])(?:reply|postreply)(?:[/=?&;_-]|$)", action, re.I)
                    else ""
                )
            )
        candidate.publication_kind = (
            self.publication_kind
            if self.publication_kind != "auto"
            else kind or ("topic" if "title" in fields else "unknown")
        )
        if candidate.publication_kind in {"reply", "comment"}:
            candidate.fields.pop("title", None)
            candidate.confidence = 0.95 if {"body", "submit"} <= fields.keys() else self.confidence(fields)
        candidate.text_inputs = [
            next(
                (r for r in alternatives.get("title", []) if r.selector == f["selector"]),
                FieldRef("title", f["selector"], frame, "input", 0.3, i),
            )
            for i, f in enumerate(text_fields)
        ]
        return candidate
