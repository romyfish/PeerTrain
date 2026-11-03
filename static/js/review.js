(() => {
  const notesForm = document.querySelector('[data-notes-form]');
  const chatForm = document.querySelector('[data-reflection-form]');
  if (!notesForm || !chatForm) return;
  const notes = notesForm.querySelector('textarea');
  const status = document.querySelector('[data-notes-status]');
  const log = document.querySelector('[data-reflection-log]');
  const avatar = log.querySelector('img').src;
  let savedNotes = notes.value;
  let chatPending = false;
  const input = chatForm.querySelector('textarea');
  const resizeInput = () => {
    input.style.height = '0px';
    const height = Math.min(130, Math.max(44, input.scrollHeight + 2));
    input.style.height = `${height}px`;
    input.style.overflowY = input.scrollHeight > 128 ? 'auto' : 'hidden';
  };
  input.addEventListener('input', resizeInput);
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!chatPending) chatForm.requestSubmit();
    }
  });
  const messageNode = (sender, text) => {
    const article = document.createElement('article');
    article.className = `peertrain-chat-message peertrain-chat-message-${sender === 'AI' ? 'ai' : 'user'}`;
    if (sender === 'AI') {
      const image = document.createElement('img');
      image.src = avatar; image.alt = 'Reflection AI'; image.className = 'reflection-avatar'; article.append(image);
    }
    const bubble = document.createElement('div');
    bubble.className = 'peertrain-chat-message-bubble'; bubble.textContent = text;
    bubble.style.whiteSpace = 'pre-wrap'; article.append(bubble);
    return article;
  };
  window.addEventListener('beforeunload', event => {
    if (notes.value === savedNotes) return;
    event.preventDefault(); event.returnValue = '';
  });
  notes.addEventListener('input', () => { status.textContent = ''; status.classList.remove('is-saved'); });
  const post = async (form, formData = new FormData(form)) => {
    const response = await fetch(form.getAttribute('action'), {
      method: 'POST', body: formData, credentials: 'same-origin',
      headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
    });
    let data;
    try { data = await response.json(); }
    catch { throw new Error('The request could not be completed. Please reload and try again.'); }
    if (!response.ok || !data.ok) throw new Error(data.error || 'Please try again.');
    return data;
  };
  notesForm.addEventListener('submit', async event => {
    event.preventDefault();
    const submitted = notes.value;
    const button = notesForm.querySelector('button'); button.disabled = true;
    status.classList.remove('is-saved');
    try {
      await post(notesForm); savedNotes = submitted;
      status.textContent = notes.value === submitted ? 'Saved' : 'Unsaved changes';
      if (notes.value === submitted) {
        const icon = document.createElement('i'); icon.className = 'bi bi-check-circle'; icon.setAttribute('aria-hidden', 'true');
        status.prepend(icon); status.classList.add('is-saved');
      }
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });
  chatForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (chatPending || !input.value.trim()) return;
    chatPending = true;
    const button = chatForm.querySelector('button');
    const errorBox = document.querySelector('[data-reflection-error]');
    button.disabled = true; input.readOnly = true; errorBox.textContent = '';
    const content = input.value;
    const data = new FormData(chatForm);
    const pendingUser = messageNode('USER', content);
    const pendingAi = messageNode('AI', '');
    pendingAi.classList.add('reflection-pending');
    const pendingBubble = pendingAi.querySelector('.peertrain-chat-message-bubble');
    for (let index = 0; index < 3; index += 1) {
      const dot = document.createElement('span'); dot.className = 'peertrain-typing-dot'; dot.setAttribute('aria-hidden', 'true'); pendingBubble.append(dot);
    }
    const label = document.createElement('span'); label.textContent = 'Responding'; pendingBubble.append(label);
    log.append(pendingUser, pendingAi); log.setAttribute('aria-busy', 'true');
    input.value = ''; resizeInput(); log.scrollTop = log.scrollHeight;
    try {
      const response = await post(chatForm, data);
      log.replaceChildren();
      response.messages.forEach(message => {
        log.append(messageNode(message.sender_type, message.content));
      });
      input.value = ''; chatForm.querySelector('[name=request_id]').value = crypto.randomUUID();
      log.scrollTop = log.scrollHeight;
    } catch (error) { errorBox.textContent = error.message; input.value = content; }
    finally {
      pendingUser.remove(); pendingAi.remove(); log.removeAttribute('aria-busy');
      button.disabled = false; input.readOnly = false; chatPending = false; resizeInput(); input.focus();
    }
  });
  resizeInput();
  log.scrollTop = log.scrollHeight;
})();
