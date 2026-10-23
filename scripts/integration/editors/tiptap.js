import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
window.PosterTiptap = () => new Editor({
  element: document.querySelector('#editor'), extensions: [StarterKit], content: ''
});
