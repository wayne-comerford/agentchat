// agentchat v1.2 — Slice 1 client logic
// Alpine.js workspace() — manages channels, agents, messages, SSE stream.

const NOSTR_BECH32 = /^npub1[0-9a-z]{20,60}$/i;

function workspace() {
  return {
    // ─────────── state ───────────
    channels: [],
    agents: [],
    messages: [],
    activeChannel: null,
    activeAgent: null,
    draft: '',
    posting: false,
    myName: '',
    myNpub: '',
    streamConnected: false,
    unread: {},                  // channel_id -> count
    mentionOpen: false,
    mentionSuggestions: [],
    mentionSelected: 0,
    mentionQuery: '',
    eventSource: null,

    // ─────────── bootstrap ───────────
    async boot() {
      // Load channels + agents
      try {
        const [chRes, agRes, health] = await Promise.all([
          fetch('/v1/ui/channels').then(r => r.json()),
          fetch('/v1/ui/agents').then(r => r.json()),
          fetch('/health').then(r => r.json()),
        ]);
        this.channels = chRes;
        this.agents = agRes;
        this.myNpub = health.identity || '';
        // Pick name from agents by matching npub
        const mine = agRes.find(a => a.npub === this.myNpub);
        this.myName = mine ? mine.name : (this.myNpub ? this.myNpub.slice(0, 8) + '…' : 'me');
      } catch (e) {
        console.error('boot failed', e);
      }
      // Auto-select first channel
      if (this.channels.length > 0) this.selectChannel(this.channels[0]);
    },

    // ─────────── selection ───────────
    selectChannel(ch) {
      if (this.activeChannel?.id === ch.id) return;
      this.activeChannel = ch;
      this.activeAgent = null;
      this.messages = [];
      this.unread[ch.id] = 0;
      this.attachStream();
    },

    selectAgent(a) {
      this.activeAgent = a;
      this.activeChannel = null;
      this.detachStream();
    },

    // ─────────── SSE stream ───────────
    attachStream() {
      this.detachStream();
      if (!this.activeChannel) return;
      const url = `/v1/ui/stream?channel=${encodeURIComponent(this.activeChannel.id)}`;
      this.eventSource = new EventSource(url);
      this.eventSource.addEventListener('connected', () => {
        this.streamConnected = true;
      });
      this.eventSource.addEventListener('message', (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          this.messages.push(msg);
          this.$nextTick(() => this.scrollToBottom());
        } catch (e) {
          console.warn('bad SSE payload', e);
        }
      });
      this.eventSource.onerror = () => {
        this.streamConnected = false;
        // EventSource auto-reconnects; just reflect status
      };
    },

    detachStream() {
      if (this.eventSource) {
        this.eventSource.close();
        this.eventSource = null;
      }
      this.streamConnected = false;
    },

    onStreamScroll(ev) {
      // Future: load older messages on scroll-to-top
    },

    scrollToBottom() {
      const el = this.$refs.stream;
      if (el) el.scrollTop = el.scrollHeight;
    },

    // ─────────── post ───────────
    async postMessage() {
      if (!this.activeChannel || !this.draft.trim() || this.posting) return;
      this.posting = true;

      // Extract @mentions from draft (strip mention markers from body before sending)
      const mentions = [];
      const stripped = this.draft.replace(/@([a-zA-Z0-9_-]+)/g, (m, name) => {
        const a = this.agents.find(x => x.name === name);
        if (a) mentions.push(a.public_key_hex);
        return m; // keep visible in UI; relay will store the text as-is
      });

      try {
        const res = await fetch('/v1/ui/post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            channel: this.activeChannel.id,
            content: this.draft,
            mentions,
          }),
        });
        const data = await res.json();
        if (!data.ok) {
          alert('post failed: ' + (data.error || 'unknown'));
        } else {
          this.draft = '';
          this.mentionOpen = false;
          // The SSE stream will push our own message back; no manual insert
        }
      } catch (e) {
        alert('post error: ' + e.message);
      } finally {
        this.posting = false;
        this.$nextTick(() => {
          const ta = document.querySelector('textarea');
          if (ta) ta.focus();
        });
      }
    },

    // ─────────── @mention autocomplete ───────────
    updateMentionSuggestions(ev) {
      const text = ev.target.value;
      const cursor = ev.target.selectionStart;
      const upToCursor = text.slice(0, cursor);
      const m = upToCursor.match(/@([a-zA-Z0-9_-]*)$/);
      if (!m) {
        this.mentionOpen = false;
        this.mentionSuggestions = [];
        return;
      }
      this.mentionQuery = m[1];
      const q = this.mentionQuery.toLowerCase();
      const matches = this.agents
        .filter(a => a.name.toLowerCase().startsWith(q))
        .slice(0, 5);
      this.mentionSuggestions = matches;
      this.mentionOpen = matches.length > 0;
      this.mentionSelected = 0;
    },

    applyMention(agent) {
      const text = this.draft;
      const cursor = (document.querySelector('textarea')?.selectionStart) || text.length;
      const upToCursor = text.slice(0, cursor);
      const afterCursor = text.slice(cursor);
      const replaced = upToCursor.replace(/@[a-zA-Z0-9_-]*$/, `@${agent.name} `);
      this.draft = replaced + afterCursor;
      this.mentionOpen = false;
      this.$nextTick(() => {
        const ta = document.querySelector('textarea');
        ta.focus();
        ta.selectionStart = ta.selectionEnd = replaced.length;
      });
    },

    // ─────────── display helpers ───────────
    aliasFor(pubkey) {
      if (!pubkey) return 'unknown';
      const a = this.agents.find(x => x.public_key_hex === pubkey);
      if (a) return a.name;
      return pubkey.slice(0, 8) + '…';
    },

    shortKey(pubkey) {
      if (!pubkey) return '';
      return pubkey.slice(0, 6) + '…' + pubkey.slice(-4);
    },

    formatTime(unixSec) {
      const d = new Date(unixSec * 1000);
      const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      if (sameDay) return `${hh}:${mm}`;
      return `${d.toLocaleDateString()} ${hh}:${mm}`;
    },

    formatContent(text) {
      if (!text) return '';
      // Escape HTML
      const esc = text.replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[c]));
      // Highlight @mentions
      return esc.replace(/@([a-zA-Z0-9_-]+)/g, (m, name) => {
        const known = this.agents.find(a => a.name === name);
        if (known) return `<span class="msg-mention">@${name}</span>`;
        return m;
      });
    },

    avatarColor(seed) {
      // Deterministic pleasant color from seed
      if (!seed) return '#475569';
      let h = 0;
      for (let i = 0; i < seed.length; i++) {
        h = ((h << 5) - h) + seed.charCodeAt(i);
        h |= 0;
      }
      const hue = Math.abs(h) % 360;
      return `hsl(${hue}, 55%, 45%)`;
    },
  };
}

// Initialize Alpine
document.addEventListener('alpine:init', () => {
  window.Alpine.data('workspace', workspace);
});