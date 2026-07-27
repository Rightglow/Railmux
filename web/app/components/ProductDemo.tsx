const projects = ["railmux", "compiler-lab", "infra-tools"];
const sessions = [
  { name: "Polish SSH history", meta: "Codex · now", active: true },
  { name: "Review layout policy", meta: "Claude · 8m" },
  { name: "Add mobile controls", meta: "Codex · 21m" },
];

function TrafficLights() {
  return (
    <div className="traffic-lights" aria-hidden="true">
      <span />
      <span />
      <span />
    </div>
  );
}

function AgentPane({
  label,
  provider,
  active,
}: {
  label: string;
  provider: string;
  active?: boolean;
}) {
  return (
    <div className={`agent-pane ${active ? "agent-pane-active" : ""}`}>
      <div className="agent-heading">
        <span>{label}</span>
        <span className="agent-provider">{provider}</span>
      </div>
      <div className="agent-copy">
        {active ? (
          <>
            <p className="terminal-muted">• Read src/railmux/fast_display_client.py</p>
            <p>
              I found the scroll owner boundary. The remote tmux client should
              keep rendering live state while history remains a local overlay.
            </p>
            <div className="diff-block">
              <span className="diff-minus">- forward_wheel(event)</span>
              <span className="diff-plus">+ history.scroll(event.rows)</span>
            </div>
            <p className="terminal-success">✓ 18 focused tests passed</p>
            <p className="terminal-prompt">
              <span>›</span> Ask for a follow-up
              <i />
            </p>
          </>
        ) : (
          <>
            <p className="terminal-muted">❯ Review the responsive layout change</p>
            <p>
              The projection preserves both tmux sessions. Compact mode changes
              what is visible, not what is running.
            </p>
            <p className="terminal-muted">
              Suggested check: restore the 2–4–4 proportions after widening.
            </p>
            <p className="terminal-success">Ready for review.</p>
          </>
        )}
      </div>
    </div>
  );
}

export function DesktopDemo() {
  return (
    <div className="terminal-window terminal-window-desktop" data-demo="desktop">
      <div className="terminal-titlebar">
        <TrafficLights />
        <span>railmux · ~/workspace</span>
        <span className="terminal-title-spacer" />
      </div>
      <div className="desktop-workspace">
        <aside className="demo-sidebar">
          <div className="sidebar-brand">
            <span className="rail-mark">R</span>
            <span>RAILMUX</span>
            <span className="version-pill">0.2.12</span>
          </div>
          <div className="sidebar-section">
            <div className="sidebar-label">PROJECTS</div>
            {projects.map((project, index) => (
              <div
                className={`sidebar-row ${index === 0 ? "sidebar-row-selected" : ""}`}
                key={project}
              >
                <span className="folder-icon">◆</span>
                <span>{project}</span>
                <small>{index === 0 ? "3" : "1"}</small>
              </div>
            ))}
          </div>
          <div className="sidebar-section sidebar-sessions">
            <div className="sidebar-label">
              SESSIONS <span>3</span>
            </div>
            {sessions.map((session) => (
              <div
                className={`session-row ${session.active ? "session-row-active" : ""}`}
                key={session.name}
              >
                <span className="status-dot" />
                <span>
                  {session.name}
                  <small>{session.meta}</small>
                </span>
              </div>
            ))}
          </div>
          <div className="sidebar-footer">
            <span>?</span> Help
            <span>q</span> Quit
            <span>•••</span> More
          </div>
        </aside>
        <main className="agent-grid">
          <AgentPane label="AGENT 1" provider="CODEX" active />
          <AgentPane label="AGENT 2" provider="CLAUDE" />
        </main>
      </div>
      <div className="terminal-status">
        <span className="status-mode">CODEX</span>
        <span>◧ Agent 1</span>
        <span className="status-tip">F8 layout · F9 fullscreen · Ctrl-B Tab sidebar</span>
      </div>
    </div>
  );
}

export function CompactDemo() {
  return (
    <div className="phone-frame" data-demo="compact">
      <div className="phone-speaker" />
      <div className="terminal-window compact-window">
        <div className="compact-topline">
          <span className="rail-mark">R</span>
          <span>railmux</span>
          <span className="compact-network">ssh</span>
        </div>
        <div className="compact-agent">
          <p className="terminal-muted">❯ Continue from the desktop session</p>
          <p>
            The same agent is still running. Compact mode projects one page at
            a time without changing the underlying layout.
          </p>
          <p className="terminal-success">✓ Connection restored</p>
          <div className="compact-code">
            <span>railmux ssh devbox</span>
            <span className="terminal-muted">history: 5,000 lines</span>
          </div>
          <p className="terminal-prompt">
            <span>›</span> Message Codex
            <i />
          </p>
        </div>
        <div className="compact-status">
          <span>[R]</span>
          <span className="compact-current">[1]</span>
          <span>[2]</span>
          <small>CODEX · ◧</small>
        </div>
      </div>
    </div>
  );
}
