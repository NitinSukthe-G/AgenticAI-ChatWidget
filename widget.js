/* =====================================================================
   widget.js - LADDU, the AI chat widget (runs in the browser)

   The "brain" (LLM + tools + memory) lives on the server in Laddu's .py files.
   This file only: draws the mascot + popup + chat panel, sends messages to POST /api/chat,
   and shows the replies.

   How a website uses it (see index.html):
     <link rel="stylesheet" href="/widget.css">
     <script src="/widget.js"></script>
     initLaddu({ api, toast, onCartChanged })   // after the user logs in
     ladduReset()                                // when the user logs out

     api(path, options)  -> the website's fetch helper (adds the login token, handles logout on 401)
     toast(text)         -> the website's small notification (optional)
     onCartChanged()     -> called when Laddu adds to the cart / places an order, so the page can refresh

   Everything is wrapped in a function "(function () { ... })()" so its variables don't clash
   with the website's code. Only initLaddu and ladduReset are made public (window.*).
   ===================================================================== */
(function () {
  const q = sel => document.querySelector(sel);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const LADDU = { info: null, messages: [], open: false, busy: false, popupTimer: null, mounted: false };
  let host = { api: null, toast: () => {}, onCartChanged: () => {} };   // functions given by the website

  const LADDU_STEP_LABELS = {
    search_products: '🔍 searched sweets', get_product_details: '🍬 checked product', get_shop_info: '🏪 shop info',
    view_cart: '🧾 checked cart', add_to_cart: '🛒 added to cart', place_order: '📦 placed order',
    get_my_orders: '📋 checked orders', request_callback: '📞 callback requested',
  };

  // The mascot: a cute traditional boy with a tilak and saffron kurta, holding a laddu.
  function ladduSvg() {
    return `<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Laddu">
      <path d="M16 102 C18 79 33 70 50 70 C67 70 82 79 84 102 Z" fill="#f28c28"/>
      <path d="M16 102 C18 79 33 70 50 70 C67 70 82 79 84 102" fill="none" stroke="#f6c453" stroke-width="1.5" opacity=".6"/>
      <path d="M40 71 L50 83 L60 71" fill="none" stroke="#f6c453" stroke-width="3.2" stroke-linejoin="round"/>
      <circle cx="50" cy="89" r="1.7" fill="#f6c453"/><circle cx="50" cy="95" r="1.7" fill="#f6c453"/>
      <rect x="44" y="61" width="12" height="11" rx="4" fill="#b8733f"/>
      <circle cx="27.5" cy="46" r="5" fill="#c67c4a"/><circle cx="72.5" cy="46" r="5" fill="#c67c4a"/>
      <circle cx="50" cy="44" r="23" fill="#d38e5a"/>
      <path d="M27 43 C25 26 38 17.5 50 17.5 C63 17.5 75 26 73 43 C69 33 61 29 50 29.5 C40 29.5 31 34 27 43 Z" fill="#20150e"/>
      <path d="M49 19 C50 10 58 7 62 11 C57 11.5 54 14 53 19.5 Z" fill="#20150e"/>
      <path d="M50 30.5 L50 37.5" stroke="#d62828" stroke-width="2.8" stroke-linecap="round"/>
      <circle cx="50" cy="40.3" r="1.9" fill="#f6c453"/>
      <ellipse cx="41" cy="46.5" rx="3.7" ry="4.5" fill="#20150e"/><ellipse cx="59" cy="46.5" rx="3.7" ry="4.5" fill="#20150e"/>
      <circle cx="42.3" cy="44.8" r="1.35" fill="#fff"/><circle cx="60.3" cy="44.8" r="1.35" fill="#fff"/>
      <ellipse cx="34.5" cy="53.5" rx="4.2" ry="2.6" fill="#f07c7c" opacity=".6"/><ellipse cx="65.5" cy="53.5" rx="4.2" ry="2.6" fill="#f07c7c" opacity=".6"/>
      <path d="M43 55.5 Q50 62.5 57 55.5" fill="none" stroke="#5a2a14" stroke-width="2.4" stroke-linecap="round"/>
      <ellipse cx="59" cy="88" rx="5" ry="3.8" fill="#d38e5a"/>
      <circle cx="68" cy="84" r="9.5" fill="#f59e0b"/>
      <g fill="#fcd34d"><circle cx="64" cy="80" r="1.3"/><circle cx="69" cy="78.5" r="1.2"/><circle cx="72.5" cy="83" r="1.3"/>
        <circle cx="66.5" cy="85.5" r="1.2"/><circle cx="70" cy="89" r="1.3"/><circle cx="63" cy="88" r="1.1"/></g>
      <ellipse cx="75" cy="87" rx="4.5" ry="3.6" fill="#d38e5a"/>
    </svg>`;
  }

  // The widget builds its own HTML (so the website doesn't need any widget markup).
  function mount() {
    if (LADDU.mounted) return;
    const root = document.createElement('div');
    root.id = 'laddu';
    root.className = 'hidden';
    root.innerHTML = `
      <div class="ld-panel hidden" id="ld-panel" role="dialog" aria-label="Chat with Laddu">
        <div class="ld-head">
          <div class="av" id="ld-av"></div>
          <div class="who"><b id="ld-name"></b><small><span class="online"></span><span id="ld-tag"></span></small></div>
          <button id="ld-new" title="Start a new chat" aria-label="New chat">↻</button>
          <button id="ld-close" title="Close" aria-label="Close chat">✕</button>
        </div>
        <div class="ld-body" id="ld-body" aria-live="polite"></div>
        <div class="ld-chips" id="ld-chips"></div>
        <form class="ld-input" id="ld-form">
          <input id="ld-text" maxlength="1000" placeholder="Ask Laddu anything…" autocomplete="off">
          <button id="ld-send" aria-label="Send">➤</button>
        </form>
      </div>
      <div class="ld-popup hidden" id="ld-popup">
        <button class="x" id="ld-popup-x" aria-label="Close">×</button>
        <span id="ld-popup-text"></span>
        <span class="cta">Chat with me →</span>
      </div>
      <button class="ld-launcher" id="ld-launcher" aria-label="Chat with Laddu"></button>`;
    document.body.appendChild(root);

    q('#ld-launcher').onclick = () => ladduToggle();
    q('#ld-close').onclick = () => ladduToggle(false);
    q('#ld-new').onclick = ladduNewChat;
    q('#ld-popup').onclick = e => { if (e.target.id === 'ld-popup-x') q('#ld-popup').classList.add('hidden'); else ladduToggle(true); };
    q('#ld-form').onsubmit = e => { e.preventDefault(); ladduSend(q('#ld-text').value); };
    q('#ld-body').onclick = e => { const b = e.target.closest('.ld-opt'); if (b) ladduSend(b.dataset.n); };
    q('#ld-chips').onclick = e => { if (e.target.tagName === 'BUTTON') ladduSend(e.target.textContent); };
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && LADDU.open) ladduToggle(false); });
    LADDU.mounted = true;
  }

  async function initLaddu(options = {}) {
    host = { ...host, ...options };
    mount();
    try {
      const data = await host.api('/api/chat/start', { method: 'POST' });   // every visit = a fresh chat
      LADDU.info = data.laddu; LADDU.messages = data.messages;
    } catch (e) { return; }  // chat not available -> just no widget
    const L = LADDU.info;
    q('#ld-launcher').innerHTML = ladduSvg() + '<span class="dot" id="ld-dot"></span>';
    q('#ld-launcher').title = `${L.name} ${L.avatar_emoji}`;
    q('#ld-av').innerHTML = ladduSvg();
    q('#ld-name').textContent = L.name;
    q('#ld-tag').textContent = L.tagline;
    q('#ld-popup-text').textContent = L.popup;
    q('#ld-dot').classList.remove('hidden');    // every visit = a new chat -> show the "new message" dot
    q('#laddu').classList.remove('hidden');
    ladduRender();
    clearTimeout(LADDU.popupTimer);
    LADDU.popupTimer = setTimeout(ladduShowPopup, 1500);   // the greeting popup on EVERY page load / refresh
    loadPersonalSuggestions();
  }

  // Swap the default chips for personal ones (based on the customer's previous chats + orders).
  async function loadPersonalSuggestions() {
    try {
      const r = await host.api('/api/chat/suggestions');
      if (LADDU.info && r.suggestions && r.suggestions.length) {
        LADDU.info.suggestions = r.suggestions;
        if (!LADDU.busy) ladduRender();
      }
    } catch (e) { /* keep the default chips */ }
  }

  function ladduShowPopup() {
    if (LADDU.open) return;
    q('#ld-popup').classList.remove('hidden');
    const btn = q('#ld-launcher');
    btn.classList.add('wave'); setTimeout(() => btn.classList.remove('wave'), 1600);
  }

  function ladduToggle(open = !LADDU.open) {
    LADDU.open = open;
    q('#ld-panel').classList.toggle('hidden', !open);
    q('#laddu').classList.toggle('open', open);
    q('#ld-popup').classList.add('hidden');
    if (open) {
      clearTimeout(LADDU.popupTimer);            // chat opened before the popup appeared -> skip it
      q('#ld-dot')?.classList.add('hidden');
      ladduScroll(); setTimeout(() => q('#ld-text').focus(), 60);
    }
  }

  function ladduReset() {  // on logout: the next user starts fresh
    clearTimeout(LADDU.popupTimer);
    Object.assign(LADDU, { info: null, messages: [], open: false, busy: false });
    if (!LADDU.mounted) return;
    q('#laddu').classList.add('hidden'); q('#ld-panel').classList.add('hidden'); q('#ld-popup').classList.add('hidden');
    q('#ld-body').innerHTML = ''; q('#ld-chips').innerHTML = '';
  }

  // Safe mini-markdown: escape first, then **bold**, "- " bullets, and numbered lines.
  // Numbered lines in Laddu's LATEST message become tappable option buttons.
  function ladduFormat(text, interactive) {
    const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    let html = '', group = null;
    const close = () => { html += group === 'ul' ? '</ul>' : group === 'opt' ? '</div>' : ''; group = null; };
    const open = g => { if (group !== g) { close(); html += g === 'ul' ? '<ul>' : '<div class="ld-options">'; group = g; } };
    const lines = String(text || '').split('\n').flatMap(l =>   // "1. Pickup  2. Delivery" -> two lines
      /^\s*\d{1,2}[.)]\s/.test(l) ? l.split(/\s+(?=\d{1,2}[.)]\s+\S)/) : [l]);
    for (const raw of lines) {
      const line = raw.trim();
      let m;
      if ((m = line.match(/^(\d{1,2})[.)]\s+(.+)$/))) {
        if (interactive) {
          open('opt');
          html += `<button class="ld-opt" data-n="${m[1]}"><span class="n">${m[1]}</span><span>${inline(m[2])}</span></button>`;
        } else { close(); html += `<div><b>${m[1]}.</b> ${inline(m[2])}</div>`; }
      } else if ((m = line.match(/^[-•*]\s+(.+)$/))) {
        open('ul'); html += `<li>${inline(m[1])}</li>`;
      } else {
        close(); html += line ? `<div>${inline(line)}</div>` : '<div class="gap"></div>';
      }
    }
    close();
    return html;
  }

  function ladduBubble(m, interactive) {
    if (m.role === 'user') return `<div class="ld-msg user"><div class="ld-bubble">${esc(m.content).replace(/\n/g, '<br>')}</div></div>`;
    const steps = [...new Set((m.steps || []).map(s => LADDU_STEP_LABELS[s] || s))];
    return `<div class="ld-msg ${m.error ? 'error' : ''}"><div class="mini">${ladduSvg()}</div>
      <div class="ld-bubble">${ladduFormat(m.content, interactive)}
        ${steps.length ? `<div class="ld-steps">${steps.map(s => `<span>${esc(s)}</span>`).join('')}</div>` : ''}</div></div>`;
  }

  function ladduRender() {
    const L = LADDU.info, msgs = LADDU.messages;
    if (!L) return;
    const all = [{ role: 'assistant', content: L.greeting }, ...msgs];  // the greeting always comes first
    q('#ld-body').innerHTML = all.map((m, i) =>
        ladduBubble(m, i === all.length - 1 && m.role === 'assistant' && !m.error && !LADDU.busy)).join('')
      + (LADDU.busy ? `<div class="ld-msg"><div class="mini">${ladduSvg()}</div><div class="ld-bubble ld-typing"><span></span><span></span><span></span></div></div>` : '');
    const fresh = !msgs.some(m => m.role === 'user');
    q('#ld-chips').innerHTML = fresh && !LADDU.busy ? L.suggestions.map(s => `<button type="button">${esc(s)}</button>`).join('') : '';
    q('#ld-text').disabled = q('#ld-send').disabled = LADDU.busy;
    ladduScroll();
  }
  function ladduScroll() { const b = q('#ld-body'); b.scrollTop = b.scrollHeight; }

  async function ladduSend(text) {
    text = String(text || '').trim();
    if (!text || LADDU.busy) return;
    LADDU.messages.push({ role: 'user', content: text });
    LADDU.busy = true; q('#ld-text').value = ''; ladduRender();
    try {
      const r = await host.api('/api/chat', { method: 'POST', body: { message: text } });
      if (r.new_chat) LADDU.messages = LADDU.messages.slice(-1);  // old chat had expired -> keep only this message
      LADDU.messages.push({ role: 'assistant', content: r.reply, steps: r.steps });
      if (r.cart_changed) await host.onCartChanged();   // Laddu changed the cart -> let the website refresh
    } catch (e) {
      LADDU.messages.push({ role: 'assistant', content: 'Oops! ' + e.message, error: true });
    }
    LADDU.busy = false; ladduRender();
    q('#ld-text').focus();
  }

  async function ladduNewChat() {
    if (LADDU.busy || !confirm('Start a new chat with Laddu?')) return;
    try { await host.api('/api/chat', { method: 'DELETE' }); LADDU.messages = []; ladduRender(); host.toast('New chat started'); }
    catch (e) { host.toast(e.message); }
  }

  // The only two things the website can call:
  window.initLaddu = initLaddu;
  window.ladduReset = ladduReset;
})();
