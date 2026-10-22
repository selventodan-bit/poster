"""Semantic field analysis across ordinary, JS and same-origin iframe editors."""

import re

from .types import FieldRef, FormCandidate

SNAPSHOT_JS = r"""() => {
 const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
 const roots = [...document.querySelectorAll('form,[role="form"]')];
 for (const editor of document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]')) {
   if (!roots.some(r => r.contains(editor))) {
     const root=editor.closest('section,article,dialog') || editor.parentElement;
     if(root && !roots.includes(root)) roots.push(root);
   }
 }
 return roots.map((root,i) => {
   const key='form-'+i; root.setAttribute('data-poster-form',key);
   const fields=[...root.querySelectorAll('input,textarea,select,[contenteditable="true"],[role="textbox"]')]
    .map((e,j) => {
     const id=key+'-field-'+j; e.setAttribute('data-poster-field',id);
     const label=[e.name,e.id,e.getAttribute('aria-label'),e.getAttribute('placeholder'),
       ...[...(e.labels || [])].map(l=>l.innerText)].filter(Boolean).join(' ').slice(0,500);
     return {selector:'[data-poster-field="'+id+'"]', label, name:e.name||'', tag:e.tagName.toLowerCase(),
       type:e.type||'', editable:e.isContentEditable, visible:visible(e),
       options:e.tagName==='SELECT'?[...e.options].map(o=>({value:o.value,disabled:o.disabled})):[]};
    });
   const buttons=[...root.querySelectorAll('button,input[type="submit"],[role="button"]')]
    .filter(visible).map((e,j)=>{
     const id=key+'-submit-'+j; e.setAttribute('data-poster-submit',id);
     return {selector:'[data-poster-submit="'+id+'"]',text:(e.innerText||e.value||e.getAttribute('aria-label')||'').slice(0,300),
       type:e.type||'',tag:e.tagName.toLowerCase()};
    });
   return {key,tag:root.tagName.toLowerCase(),action:root.action||location.href,method:root.method||'',fields,buttons};
 });
}"""

ROLE_WORDS = {
    "title": r"\b(title|subject|asunto|sujet|titre|titolo|t[ií]tulo|betreff|заголовок|тема)\b",
    "body": r"\b(body|message|content|texto|mensaje|mensagem|texte|messaggio|сообщение|текст|editor)\b",
    "category": r"\b(category|forum|categor[ií]a|раздел|категория)\b",
    "tags": r"\b(tags|etiquetas|теги)\b",
    "csrf": r"csrf|nonce|token",
}
SUBMIT = re.compile(r"publish|post|submit|enviar|publicar|envoyer|invia|отправить|опубликовать", re.I)
SEARCH = re.compile(r"search|buscar|recherche|поиск", re.I)


class UniversalFormDetector:
    version = "semantic-2.0"

    async def inspect(self, page, allowed_hosts) -> list[FormCandidate]:
        from urllib.parse import urlsplit

        candidates, iframe_parents = [], {}
        for index, frame in enumerate(page.frames):
            if index:
                try:
                    await frame.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    continue
            if urlsplit(frame.url).hostname not in allowed_hosts and frame.url != "about:blank":
                continue
            if index:
                try:
                    owner = await frame.frame_element()
                    iframe_parents[index] = await owner.evaluate(
                        "e => e.closest('[data-poster-form]')?.getAttribute('data-poster-form')"
                    )
                except Exception:
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
            parent_key = iframe_parents.get(child.frame)
            if not parent_key or "body" not in child.fields:
                continue
            parent = next((c for c in candidates if c.frame == 0 and c.key == f"0:{parent_key}"), None)
            if parent and "title" in parent.fields:
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
        fields, evidence = {}, []
        visible = [f for f in snapshot["fields"] if f["visible"] and f["type"] not in {"hidden", "submit"}]
        text_fields = [
            f
            for f in visible
            if f["tag"] == "input" and f["type"] in {"text", ""} and not SEARCH.search(f["label"])
        ]
        if any(f["type"] == "password" for f in visible):
            return None
        for role, pattern in ROLE_WORDS.items():
            choices = []
            for f in visible:
                score = 0.85 if re.search(pattern, f["label"], re.I) else 0
                if role == "body" and (f["editable"] or f["tag"] == "textarea"):
                    score = max(score, 0.8)
                if role == "title" and f["tag"] == "input" and f["type"] in {"text", ""}:
                    score = max(score, 0.3)
                if f["type"] == "search" or SEARCH.search(f["label"]):
                    score = 0
                if score:
                    choices.append((score, f))
            choices.sort(key=lambda c: c[0], reverse=True)
            if choices and (len(choices) == 1 or choices[0][0] > choices[1][0]):
                score, f = choices[0]
                kind = "contenteditable" if f["editable"] else f["tag"]
                fields[role] = FieldRef(
                    role, f["selector"], frame, kind, score, text_fields.index(f) if f in text_fields else 0
                )
                evidence.append("semantic-" + role)
        # A single visible text input next to an editor is useful evidence, not a CSS-name assumption.
        if "title" not in fields:
            titles = [
                f
                for f in visible
                if f["tag"] == "input" and f["type"] in {"text", ""} and not SEARCH.search(f["label"])
            ]
            if len(titles) == 1:
                fields["title"] = FieldRef("title", titles[0]["selector"], frame, "input", 0.6)
                evidence.append("single-text-input-next-to-editor")
        buttons = [
            (0.9 if SUBMIT.search(b["text"]) else 0.5 if b["type"] == "submit" else 0, b)
            for b in snapshot["buttons"]
        ]
        buttons.sort(key=lambda b: b[0], reverse=True)
        if buttons and buttons[0][0] and (len(buttons) == 1 or buttons[0][0] > buttons[1][0]):
            score, b = buttons[0]
            fields["submit"] = FieldRef("submit", b["selector"], frame, "button", score)
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
        )
        candidate.text_inputs = [
            FieldRef("title", f["selector"], frame, "input", 0.55, i) for i, f in enumerate(text_fields)
        ]
        return candidate
