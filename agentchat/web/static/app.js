// agentchat v1.2 — Slice 1 client logic (with auth)
// Alpine.js workspace() — manages channels, agents, messages, SSE stream, login.

const NOSTR_BECH32 = /^npub1[0-9a-z]{20,60}$/i;

function workspace() {
  return {
    // ─────────── state ───────────
    channels: [],
    agents: [],
    identities: [],          // available identities from /v1/auth/identities
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

    // ─────────── Memories drawer ───────────
    memoriesOpen: false,
    memories: {
      loading: false,
      agents: [],
      collapsed: {},          // "agent::section" → bool
      editing: {},            // "agent::section::idx" → bool
      addingLineFor: null,    // "agent::section"
      addingSectionFor: null, // "agent"
      draft: '',              // shared draft buffer (edit/add)
      toast: null,            // { kind: 'ok'|'error', text: '' }
    },

    // ─────────── bootstrap ───────────
    async boot() {
      try {
        const [chRes, agRes, who, ids] = await Promise.all([
          fetch('/v1/ui/channels', { credentials: 'same-origin' }).then(r => r.json()),
          fetch('/v1/ui/agents', { credentials: 'same-origin' }).then(r => r.json()),
          fetch('/v1/auth/whoami', { credentials: 'same-origin' }).then(r => r.json()),
          fetch('/v1/auth/identities', { credentials: 'same-origin' }).then(r => r.json()),
        ]);
        this.channels = chRes;
        this.agents = agRes;
        this.identities = ids;
        if (who.logged_in) {
          this.myName = who.name;
          this.myNpub = who.npub;
        } else {
          this.myName = '';
          this.myNpub = '';
        }
      } catch (e) {
        console.error('boot failed', e);
      }
      // Start the agent_status SSE so the sidebar can render live liveness
      // + focused channel updates without polling.
      this.connectAgentStatus();
      // Auto-select: prefer #general (where the work happens), else first channel
      if (this.channels.length > 0) {
        const preferred = this.channels.find(c => c.id === 'general') || this.channels[0];
        this.selectChannel(preferred);
      }
    },

    // ─────────── auth ───────────
    async login(name) {
      try {
        const res = await fetch('/v1/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ name }),
        });
        const data = await res.json();
        if (!data.ok) {
          alert('login failed: ' + (data.error || 'unknown'));
          return;
        }
        this.myName = data.name;
        this.myNpub = data.npub;
      } catch (e) {
        alert('login error: ' + e.message);
      }
    },

    async logout() {
      try {
        await fetch('/v1/auth/logout', {
          method: 'POST',
          credentials: 'same-origin',
        });
      } catch (e) { /* ignore */ }
      this.myName = '';
      this.myNpub = '';
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

    // ─────────── agent_status SSE ───────────
    connectAgentStatus() {
      // Tear down any existing connection before reconnecting.
      if (this._agentStatusES) {
        try { this._agentStatusES.close(); } catch (e) {}
        this._agentStatusES = null;
      }
      let es;
      try {
        es = new EventSource('/v1/ui/stream?channel=agent_status', { withCredentials: true });
      } catch (e) {
        console.error('agent_status EventSource failed to construct', e);
        setTimeout(() => this.connectAgentStatus(), 3000);
        return;
      }
      this._agentStatusES = es;

      // Initial snapshot — populate status_entry on every agent.
      es.addEventListener('snapshot', (ev) => {
        try {
          const snap = JSON.parse(ev.data);
          this.applyAgentStatusSnapshot(snap);
        } catch (e) { /* ignore */ }
      });

      // Per-agent status updates — merge into the matching agents[] entry.
      es.addEventListener('agent_status', (ev) => {
        try {
          const payload = JSON.parse(ev.data);
          this.applyAgentStatus(payload.agent, payload.state);
        } catch (e) { /* ignore */ }
      });

      // Focus pin/clear events.
      es.addEventListener('focus', (ev) => {
        try {
          const payload = JSON.parse(ev.data);
          this.applyAgentFocus(payload.agent, payload.channel);
        } catch (e) { /* ignore */ }
      });

      // On error, the EventSource will auto-reconnect, but if the close
      // was hard we tear down and reconnect ourselves to avoid stuck state.
      es.addEventListener('error', () => {
        if (es.readyState === EventSource.CLOSED) {
          setTimeout(() => this.connectAgentStatus(), 3000);
        }
      });
    },

    applyAgentStatusSnapshot(snap) {
      const statusByName = snap.agents || {};
      const focusByName = snap.focus || {};
      this.agents = this.agents.map((a) => {
        const s = statusByName[a.name] || null;
        const f = focusByName[a.name]?.channel || null;
        return {
          ...a,
          status_entry: s
            ? { ...s, focused_channel: s.focused_channel || f }
            : (f ? { status: 'idle', last_activity_ts: 0, focused_channel: f, last_message: null } : null),
        };
      });
    },

    applyAgentStatus(name, state) {
      this.agents = this.agents.map((a) =>
        a.name === name ? { ...a, status_entry: state } : a
      );
    },

    applyAgentFocus(name, channel) {
      this.agents = this.agents.map((a) => {
        if (a.name !== name) return a;
        const se = a.status_entry || {
          status: 'idle', last_activity_ts: 0,
          focused_channel: null, last_message: null,
        };
        return { ...a, status_entry: { ...se, focused_channel: channel } };
      });
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
      if (!this.myName) {
        alert('Sign in first (click ⇅ at the bottom of the sidebar).');
        return;
      }
      this.posting = true;

      // Extract @mentions from draft
      const mentions = [];
      this.draft.replace(/@([a-zA-Z0-9_-]+)/g, (m, name) => {
        const a = this.agents.find(x => x.name === name);
        if (a) mentions.push(a.public_key_hex);
        return m;
      });

      try {
        const res = await fetch('/v1/ui/post', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
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
      if (!seed) return '#475569';
      let h = 0;
      for (let i = 0; i < seed.length; i++) {
        h = ((h << 5) - h) + seed.charCodeAt(i);
        h |= 0;
      }
      const hue = Math.abs(h) % 360;
      return `hsl(${hue}, 55%, 45%)`;
    },

    // ─────────── Memories drawer methods ───────────
    toggleMemories() {
      this.memoriesOpen = !this.memoriesOpen;
      if (this.memoriesOpen && (!this.memories.agents || this.memories.agents.length === 0)) {
        this.loadMemories();
      }
    },

    async loadMemories() {
      this.memories.loading = true;
      try {
        const r = await fetch('/v1/ui/memory/agents', { credentials: 'same-origin' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        this.memories.agents = j.agents || [];
      } catch (e) {
        this.toast('error', `Load failed: ${e.message || e}`);
        this.memories.agents = [];
      } finally {
        this.memories.loading = false;
      }
    },

    toggleSection(agentName, sectionTitle) {
      const key = `${agentName}-${sectionTitle}`;
      this.memories.collapsed[key] = !this.memories.collapsed[key];
    },

    startEdit(agentName, sectionTitle, idx, currentLine) {
      const key = `${agentName}-${sectionTitle}-${idx}`;
      this.memories.editing[key] = true;
      this.memories.draft = currentLine;
    },

    cancelEdit() {
      this.memories.editing = {};
      this.memories.draft = '';
    },

    async commitEdit(agentName, sectionTitle, idx) {
      const editKey = `${agentName}-${sectionTitle}-${idx}`;
      const newLine = (this.memories.draft || '').trim();
      const oldLine = this._findLine(agentName, sectionTitle, idx);
      if (!newLine) {
        this.toast('error', 'Line cannot be empty');
        return;
      }
      if (newLine === oldLine) {
        this.cancelEdit();
        return;
      }
      // Strategy: PUT the whole section with the edited line swapped in.
      const allLines = this._getSectionLines(agentName, sectionTitle).map((ln, i) =>
        i === idx ? newLine : ln
      );
      try {
        const r = await fetch(
          `/v1/ui/memory/agents/${encodeURIComponent(agentName)}/sections/${encodeURIComponent(sectionTitle)}`,
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ lines: allLines }),
          }
        );
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
        this.cancelEdit();
        await this.loadMemories();
        this.toast('ok', `Updated ${agentName} · ${sectionTitle}`);
      } catch (e) {
        this.toast('error', `Edit failed: ${e.message || e}`);
      }
    },

    async removeLine(agentName, sectionTitle, idx) {
      const allLines = this._getSectionLines(agentName, sectionTitle);
      if (idx >= allLines.length) return;
      const newLines = allLines.filter((_, i) => i !== idx);
      try {
        const r = await fetch(
          `/v1/ui/memory/agents/${encodeURIComponent(agentName)}/sections/${encodeURIComponent(sectionTitle)}`,
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ lines: newLines }),
          }
        );
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
        await this.loadMemories();
        this.toast('ok', `Removed line from ${agentName}`);
      } catch (e) {
        this.toast('error', `Remove failed: ${e.message || e}`);
      }
    },

    addLinePrompt(agentName, sectionTitle) {
      this.memories.addingLineFor = `${agentName}::${sectionTitle}`;
      this.memories.draft = '';
    },

    async commitAddLine(agentName, sectionTitle) {
      const line = (this.memories.draft || '').trim();
      if (!line) {
        this.toast('error', 'Line cannot be empty');
        return;
      }
      try {
        const r = await fetch(
          `/v1/ui/memory/agents/${encodeURIComponent(agentName)}/sections/${encodeURIComponent(sectionTitle)}/lines`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ line }),
          }
        );
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
        this.memories.addingLineFor = null;
        this.memories.draft = '';
        await this.loadMemories();
        this.toast('ok', `Added line to ${agentName}`);
      } catch (e) {
        this.toast('error', `Add failed: ${e.message || e}`);
      }
    },

    async commitAddSection(agentName) {
      const title = (this.memories.draft || '').trim();
      if (!title) {
        this.toast('error', 'Section name cannot be empty');
        return;
      }
      try {
        const r = await fetch(
          `/v1/ui/memory/agents/${encodeURIComponent(agentName)}/sections/${encodeURIComponent(title)}`,
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ lines: [] }),
          }
        );
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
        this.memories.addingSectionFor = null;
        this.memories.draft = '';
        await this.loadMemories();
        this.toast('ok', `Created section "${title}" for ${agentName}`);
      } catch (e) {
        this.toast('error', `Create section failed: ${e.message || e}`);
      }
    },

    // ── helpers ──
    _findLine(agentName, sectionTitle, idx) {
      const lines = this._getSectionLines(agentName, sectionTitle);
      return lines[idx] || '';
    },

    _getSectionLines(agentName, sectionTitle) {
      const agent = (this.memories.agents || []).find(a => a.name === agentName);
      if (!agent) return [];
      const section = (agent.sections || []).find(
        s => (s.title || '(intro)') === (sectionTitle || '(intro)')
      );
      return section ? [...(section.lines || [])] : [];
    },

    toast(kind, text) {
      this.memories.toast = { kind, text };
      setTimeout(() => {
        // Only clear if it's still this toast (avoid races).
        if (this.memories.toast && this.memories.toast.text === text) {
          this.memories.toast = null;
        }
      }, 3000);
    },
  };
}

// Initialize Alpine
document.addEventListener('alpine:init', () => {
  window.Alpine.data('workspace', workspace);
});
