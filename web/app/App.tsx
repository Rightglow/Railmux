import { useEffect } from "react";

import CopyCommand from "./components/CopyCommand";
import TerminalRecording from "./components/TerminalRecording";

export default function Home() {
  useEffect(() => {
    let resizeObserver: ResizeObserver | null = null;
    let frame: number | null = null;
    let stopTimer: number | null = null;
    let active = false;

    const stopTracking = () => {
      active = false;
      resizeObserver?.disconnect();
      resizeObserver = null;
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = null;
      if (stopTimer !== null) window.clearTimeout(stopTimer);
      stopTimer = null;
    };

    const alignToHash = () => {
      if (!active) return;
      const targetId = window.location.hash.slice(1);
      if (!targetId) {
        return;
      }
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        frame = null;
        document.getElementById(targetId)?.scrollIntoView({ block: "start" });
      });
    };

    const startTracking = () => {
      stopTracking();
      if (!window.location.hash.slice(1)) return;
      active = true;
      resizeObserver = new ResizeObserver(alignToHash);
      resizeObserver.observe(document.body);
      alignToHash();
      // Asciinema players size themselves after their recordings load. Keep
      // the requested section anchored through that bounded startup window,
      // but never fight deliberate wheel, touch, pointer, or keyboard input.
      stopTimer = window.setTimeout(stopTracking, 8_000);
    };

    const userEvents = ["wheel", "touchstart", "pointerdown", "keydown"];
    startTracking();
    window.addEventListener("hashchange", startTracking);
    window.addEventListener("load", alignToHash);
    for (const event of userEvents) {
      window.addEventListener(event, stopTracking, { passive: true });
    }
    return () => {
      stopTracking();
      window.removeEventListener("hashchange", startTracking);
      window.removeEventListener("load", alignToHash);
      for (const event of userEvents) {
        window.removeEventListener(event, stopTracking);
      }
    };
  }, []);

  return (
    <div className="site-shell">
      <header className="site-nav">
        <a className="wordmark" href="#top" aria-label="Railmux home">
          <span className="wordmark-icon">R</span>
          <span>RAILMUX</span>
        </a>
        <nav aria-label="Main navigation">
          <a href="#workflow">Workflow</a>
          <a href="#controls">Controls</a>
          <a href="#features">Features</a>
          <a href="#ssh">SSH</a>
          <a href="https://github.com/Rightglow/Railmux">GitHub</a>
        </nav>
        <a className="nav-install" href="#install">
          Install <span>↘</span>
        </a>
      </header>

      <main id="top">
        <section className="hero section-wrap">
          <div className="hero-grid">
            <div className="hero-copy">
              <p className="eyebrow">
                <span />
                Built on tmux. Made for coding agents.
              </p>
              <h1>
                Keep every
                <br />
                coding agent
                <br />
                <em>within reach.</em>
              </h1>
              <p className="hero-lede">
                Run, resume, and switch between Claude Code and Codex sessions
                from one persistent terminal workspace.
              </p>
              <div className="hero-actions">
                <a className="button button-primary" href="#install">
                  Get Railmux <span>→</span>
                </a>
                <a
                  className="button button-secondary"
                  href="https://github.com/Rightglow/Railmux"
                >
                  View source
                </a>
              </div>
              <div className="platform-line">
                <span>LOCAL</span> macOS · Linux · Windows · WSL
                <i />
                <span>REMOTE</span> Linux · macOS · Windows
              </div>
            </div>
            <div className="hero-note">
              <span className="hero-note-index">01 / WORKSPACE</span>
              <p>
                One sidebar.
                <br />
                Two live agents.
                <br />
                Zero lost context.
              </p>
            </div>
          </div>
          <div className="hero-demo">
            <div className="capture-meta">
              <span><i /> CAPTURED CLAUDE CODE 2.1.220 · ISOLATED RAILMUX</span>
              <small>recorded Railmux UI · replayed provider transcript · no live provider call</small>
            </div>
            <TerminalRecording
              className="hero-terminal-recording"
              poster="npt:0.2"
              startAt={0}
              autoPlay
              loop
              controls={false}
              dataDemo="desktop-recording"
            />
          </div>
        </section>

        <section className="statement">
          <div className="section-wrap statement-inner">
            <p className="section-kicker">THE PROBLEM</p>
            <h2>
              Agent sessions multiply.
              <br />
              Your mental overhead <em>shouldn&apos;t.</em>
            </h2>
            <p>
              Stop hunting through tmux windows and copying session IDs.
              Railmux turns scattered agent work into one visible, durable
              workspace.
            </p>
          </div>
        </section>

        <section className="workflow" id="workflow">
          <div className="section-wrap">
            <div className="section-heading workflow-heading">
              <p className="section-kicker section-kicker-light">ONE LOOP</p>
              <h2>Browse. Open. Keep moving.</h2>
            </div>
            <div className="workflow-demo-grid">
              <div className="workflow-steps">
                <article>
                  <span>01</span>
                  <h3>Preview</h3>
                  <p>
                    Single-click a stopped session to inspect its transcript
                    without starting it.
                  </p>
                  <kbd>CLICK</kbd>
                </article>
                <article>
                  <span>02</span>
                  <h3>Resume</h3>
                  <p>
                    Press Enter after preview—or double-click—to continue the
                    conversation.
                  </p>
                  <div><kbd>ENTER</kbd><kbd>DOUBLE-CLICK</kbd></div>
                </article>
                <article>
                  <span>03</span>
                  <h3>Switch</h3>
                  <p>
                    Click another running session to switch the Target pane
                    instantly.
                  </p>
                  <div><kbd>C-b Tab</kbd><kbd>RUNNING</kbd></div>
                </article>
                <article>
                  <span>04</span>
                  <h3>Manage</h3>
                  <p>
                    Right-click a Sessions or Running row for every action
                    available in its current state.
                  </p>
                  <kbd>RIGHT-CLICK</kbd>
                </article>
              </div>
              <div className="workflow-player">
                <div className="capture-meta capture-meta-dark">
                  <span><i /> RECORDED RAILMUX UI · ISOLATED TMUX</span>
                  <small>guided input cues · replayed provider transcript · no live provider call</small>
                </div>
                <TerminalRecording
                  source="railmux-workflow-demo.cast"
                  className="workflow-terminal-recording"
                  poster="npt:0.6"
                  controls={false}
                  loop
                  playWhenVisible
                  inputHud
                  cueCols={160}
                  cueRows={38}
                  dataDemo="workflow-recording"
                />
              </div>
              <article className="workflow-menu-proof">
                <div className="workflow-menu-copy">
                  <span>RIGHT-CLICK / SESSION MENU</span>
                  <h3>Manage without leaving the sidebar.</h3>
                  <p>
                    Preview, open, rename, star, copy the title, kill, open a
                    terminal, or delete. Railmux hides actions that do not
                    apply to the selected session.
                  </p>
                </div>
                <div className="workflow-menu-viewport">
                  <TerminalRecording
                    source="railmux-workflow-demo.cast"
                    className="workflow-menu-recording"
                    poster="npt:17.2"
                    controls={false}
                    dataDemo="session-menu-recording"
                  />
                </div>
              </article>
              <aside className="workflow-pointer-note">
                <p>
                  <strong>MOUSE INPUT</strong>
                  Your terminal must report mouse events to Railmux.{" "}
                  <a href="https://github.com/Rightglow/Railmux#2-mouse-buttons-or-f8f9-dont-work--whats-wrong">
                    Check terminal setup
                  </a>
                  .
                </p>
                <p>
                  <strong>COPY</strong>
                  With <code>railmux ssh</code>, drag inside one agent pane to
                  copy locally. Other connections depend on terminal clipboard
                  support.{" "}
                  <a href="https://github.com/Rightglow/Railmux#1-how-do-i-copy-text-from-the-agent-pane">
                    See every copy path
                  </a>
                  .
                </p>
              </aside>
            </div>
          </div>
        </section>

        <section className="lifecycle-section" id="controls">
          <div className="section-wrap">
            <div className="lifecycle-heading">
              <div>
                <p className="section-kicker section-kicker-light">
                  WORKSPACE CONTROL
                </p>
                <h2>
                  Shape the workspace.
                  <br />
                  Leave on your terms.
                </h2>
              </div>
              <div className="lifecycle-intro">
                <p>
                  Change the provider view or reshape the panes without
                  interrupting hidden work. Use the keyboard or click the
                  matching Button Bar control; destructive choices still
                  require an explicit confirmation.
                </p>
                <div>
                  <span><kbd>+</kbd> or click <strong>More</strong></span>
                  <span><kbd>m</kbd> or click <strong>Mode</strong></span>
                  <span><kbd>F8</kbd> or click <strong>Layout</strong></span>
                </div>
              </div>
            </div>
            <div className="workspace-control-grid">
              <article>
                <span>MODE / <kbd>m</kbd></span>
                <div className="mode-switch" aria-hidden="true">
                  <strong>CC</strong>
                  <i>↔</i>
                  <strong>CODEX</strong>
                </div>
                <h3>Switch the whole sidebar.</h3>
                <p>
                  Projects, Sessions, and Running follow the selected
                  provider. Agents in the other mode stay alive and reappear
                  when you switch back.
                </p>
                <small className="status-click-note">
                  <strong>BUTTON BAR FOLDED?</strong>
                  On tmux 3.4+, click the provider name in the lower-left
                  status bar.
                </small>
              </article>
              <article>
                <span>LAYOUT / <kbd>F8</kbd></span>
                <div className="layout-switch" aria-hidden="true">
                  <i><b /></i>
                  <i><b /><b /></i>
                  <i><b /><b /></i>
                </div>
                <h3>One, side by side, or stacked.</h3>
                <p>
                  Cycle the visible workspace without stopping an agent hidden
                  by the new arrangement. The Target pane remains explicit.
                </p>
                <small className="status-click-note">
                  <strong>BUTTON BAR FOLDED?</strong>
                  On tmux 3.4+, click the layout symbol in the lower-left
                  status bar.
                </small>
              </article>
            </div>
            <div className="lifecycle-demo-grid">
              <div className="lifecycle-player">
                <div className="capture-meta capture-meta-dark">
                  <span><i /> RECORDED RAILMUX UI · MODE, LAYOUT, QUIT</span>
                  <small>guided input cues · replayed provider transcript · no live provider call</small>
                </div>
                <TerminalRecording
                  source="railmux-controls-demo.cast"
                  className="controls-terminal-recording"
                  poster="npt:10.6"
                  controls={false}
                  loop
                  playWhenVisible
                  inputHud
                  cueCols={180}
                  cueRows={38}
                  dataDemo="controls-recording"
                />
              </div>
              <div className="lifecycle-choices">
                <article>
                  <span>DETACH</span>
                  <h3><kbd>Ctrl-B d</kbd></h3>
                  <p>
                    Leave this terminal only. Railmux and every agent keep
                    running exactly where they were. This tmux-level action
                    intentionally remains the keyboard exception.
                  </p>
                </article>
                <article>
                  <span>SOFT QUIT</span>
                  <h3><kbd>q</kbd> then <kbd>s</kbd></h3>
                  <p>
                    Close the shared Railmux UI in every attached terminal.
                    Agent sessions stay alive and are recovered next start.
                    Click <strong>Quit</strong>, then press <kbd>s</kbd> in
                    the confirmation.
                  </p>
                </article>
                <article>
                  <span>QUIT</span>
                  <h3><kbd>q</kbd> then <kbd>y</kbd></h3>
                  <p>
                    Close Railmux and stop every running agent session after
                    explicit confirmation. Click <strong>Quit</strong>, then
                    press <kbd>y</kbd> in the confirmation.
                  </p>
                </article>
              </div>
            </div>
          </div>
        </section>

        <section className="features section-wrap" id="features">
          <div className="section-heading">
            <p className="section-kicker">WHAT IT DOES</p>
            <h2>Stay in flow.</h2>
            <p>
              Everything important remains visible. Anything you leave running
              remains alive.
            </p>
          </div>
          <div className="feature-evidence">
            <article className="feature-showcase feature-showcase-agents">
              <div className="feature-showcase-copy">
                <span>01 / PARALLEL WORK</span>
                <h3>Two agents, one workspace.</h3>
                <p>
                  Put two live sessions side by side or stack them. Railmux
                  keeps the Target explicit, so sidebar actions always land in
                  the pane you intended.
                </p>
              </div>
              <div className="feature-real-shot">
                <TerminalRecording
                  source="railmux-dual-demo.cast"
                  className="dual-agent-recording"
                  poster="npt:14.5"
                  controls={false}
                  dataDemo="dual-recording"
                />
                <small>RECORDED RAILMUX UI · REPLAYED CLAUDE CODE + CODEX</small>
              </div>
            </article>
            <article className="feature-showcase feature-showcase-sidebar">
              <div
                className="feature-sidebar-evidence"
                role="img"
                aria-label="Railmux sidebar showing New project, New session, and a running agent"
              >
                <div className="sidebar-evidence-viewport">
                  <TerminalRecording
                    source="railmux-workflow-demo.cast"
                    className="sidebar-evidence-recording"
                    poster="npt:7.8"
                    controls={false}
                    dataDemo="sidebar-evidence"
                  />
                </div>
                <div className="sidebar-evidence-legend" aria-hidden="true">
                  <span>NEW PROJECT</span>
                  <span>NEW SESSION</span>
                  <span>RUNNING</span>
                </div>
              </div>
              <div className="feature-showcase-copy">
                <span>02 / FIND ANYTHING</span>
                <h3>A sidebar that knows your work.</h3>
                <p>
                  New Project and New Session stay pinned at the top. Start or
                  resume there, browse history, and press <kbd>m</kbd>—or click
                  Mode—to switch the whole sidebar between providers.
                </p>
              </div>
            </article>
            <div className="feature-support-grid">
              <article>
                <span>03 / PERSISTENCE</span>
                <h3>Keep the sessions you need alive.</h3>
                <p>
                  Switch projects, detach, or close the Railmux view without
                  interrupting the agents doing the work.
                </p>
              </article>
              <article>
                <span>04 / RECOVERY</span>
                <h3>Designed to recover.</h3>
                <p>
                  Soft restart restores the workspace while watchdog
                  diagnostics report failures without taking agents down.
                </p>
              </article>
              <article>
                <span>05 / DISCOVERY</span>
                <h3>Bring the sessions you already have.</h3>
                <p>
                  Railmux discovers interactive Claude Code and Codex
                  conversations from provider history—even when they were
                  started outside Railmux.
                  <small>
                    One-off <code>codex exec</code> runs and subagent rollouts
                    are filtered from the sidebar.
                  </small>
                </p>
              </article>
            </div>
            <div className="feature-entrypoints">
              <div className="feature-entrypoints-heading">
                <span>06 / ENTRY POINTS</span>
                <h3>Start fresh. Get help.</h3>
                <p>
                  New Project opens a keyboard-and-mouse directory browser.
                  Help keeps shortcuts nearby and can launch a separate,
                  read-only support session in Railmux&apos;s own help
                  workspace against the installed guide—not in your project.
                </p>
              </div>
              <div className="entrypoint-grid">
                <article>
                  <div className="entrypoint-terminal-viewport">
                    <TerminalRecording
                      source="railmux-tour-demo.cast"
                      className="entrypoint-recording"
                      poster="npt:2.5"
                      controls={false}
                      dataDemo="new-project-recording"
                    />
                  </div>
                  <div>
                    <span>NEW PROJECT</span>
                    <small>Browse, filter, create, or choose a directory.</small>
                  </div>
                </article>
                <article>
                  <div className="entrypoint-terminal-viewport">
                    <TerminalRecording
                      source="railmux-tour-demo.cast"
                      className="entrypoint-recording"
                      poster="npt:6.2"
                      controls={false}
                      dataDemo="help-recording"
                    />
                  </div>
                  <div>
                    <span>HELP</span>
                    <small>Read-only Railmux support, outside your project.</small>
                  </div>
                </article>
              </div>
            </div>
          </div>
        </section>

        <section className="ssh-section" id="ssh">
          <div className="section-wrap ssh-grid">
            <div className="ssh-copy">
              <p className="section-kicker section-kicker-light">RAILMUX SSH</p>
              <h2>
                Remote work,
                <br />
                without the redraw tax.
              </h2>
              <p>
                Ordinary terminals can choke when an agent redraws a full tmux
                pane. <code>railmux ssh</code> sends the latest state, coalesces
                changed rows, and keeps agent history locally for responsive
                scrolling.
              </p>
              <ul>
                <li><span>↗</span> Skip superseded intermediate frames</li>
                <li><span>↗</span> Scroll up to 20,000 configured history lines</li>
                <li><span>↗</span> Drag inside one agent pane and copy locally</li>
                <li><span>↗</span> Reconnect without restarting remote agents</li>
                <li><span>↗</span> Install a matching remote helper with consent</li>
              </ul>
              <p className="ssh-selection-note">
                Click URLs to open them locally, or open validated remote paths
                inside Railmux or in a separate terminal. Drag-to-copy stays
                pane-bounded and local.
              </p>
              <CopyCommand command="railmux ssh your-server" />
            </div>
            <div className="ssh-visual" aria-hidden="true">
              <div className="ssh-node ssh-node-remote">
                <span>REMOTE SERVER</span>
                <strong>tmux + Railmux</strong>
                <small>agents keep running here</small>
              </div>
              <div className="ssh-flow ssh-flow-live">
                <i />
                <span>LATEST VISIBLE STATE</span>
                <small>superseded redraws are not painted</small>
              </div>
              <div className="ssh-node ssh-node-local">
                <span>LOCAL TERMINAL</span>
                <strong>fast renderer</strong>
                <small>paint only what matters now</small>
              </div>
              <div className="ssh-flow ssh-flow-history">
                <i />
                <span>HISTORY ON DEMAND</span>
                <small>up to 20,000 lines in a local cache</small>
              </div>
            </div>
          </div>
        </section>

        <section className="compact-section section-wrap">
          <div className="compact-copy">
            <p className="section-kicker">RESPONSIVE WORKSPACE</p>
            <h2>
              Same sessions.
              <br />
              Smaller screen.
            </h2>
            <p>
              When space gets tight, Railmux projects the sidebar and each
              agent onto separate pages. Nothing is killed; off-screen agents
              keep running in protected tmux sessions until selected. The
              phone recording uses touch only: tap
              <strong> New session</strong>, then tap the page labels.
            </p>
            <div className="compact-controls">
              <span>[R] Sidebar</span>
              <span>[1] Agent one</span>
              <span>[2] Agent two</span>
            </div>
          </div>
          <div className="phone-frame" data-demo="compact-recording">
            <div className="phone-speaker" />
            <div className="mobile-capture-label">
              PORTRAIT COMPACT · 46×38
            </div>
            <TerminalRecording
              source="railmux-mobile-demo.cast"
              className="mobile-terminal-recording"
              poster="npt:4.8"
              controls={false}
              loop
              playWhenVisible
              inputHud
              cueCols={46}
              cueRows={38}
              dataDemo="mobile-recording"
            />
          </div>
        </section>

        <section className="install" id="install">
          <div className="section-wrap install-inner">
            <p className="section-kicker">QUICK START</p>
            <div className="install-grid">
              <div>
                <h2>
                  Your agents are already working.
                  <br />
                  Give them a better station.
                </h2>
                <p>
                  On macOS, Linux, and WSL, Railmux requires Python 3.9+, tmux,
                  and at least one supported agent CLI. Native Windows uses
                  Python 3.10+ and provisions its own private managed
                  MSYS2/tmux runtime on first launch.
                </p>
                <p>
                  The first Windows setup is roughly 700 MB. Later Railmux
                  updates reuse that verified base. Railmux installs and
                  validates its own tmux 3.7+; native Codex and Claude Code
                  keep the same Windows sessions and credentials.
                </p>
                <aside className="install-remote-note">
                  <span>REMOTE WORK</span>
                  <p>
                    If ordinary SSH cannot keep up with full-screen redraws,
                    use <code>railmux ssh</code> from macOS, Linux, WSL, or
                    native Windows. A Windows remote must already have its
                    matching managed runtime installed locally.
                  </p>
                </aside>
              </div>
              <div className="install-commands">
                <CopyCommand command="pip install railmux" />
                <CopyCommand command="railmux" />
                <div className="install-links">
                  <a href="https://github.com/Rightglow/Railmux#quick-start">
                    Read the full guide <span>→</span>
                  </a>
                  <a href="https://pypi.org/project/railmux/">
                    View on PyPI <span>→</span>
                  </a>
                </div>
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer>
        <div className="section-wrap footer-grid">
          <div>
            <a className="wordmark wordmark-footer" href="#top">
              <span className="wordmark-icon">R</span>
              <span>RAILMUX</span>
            </a>
            <p>One terminal for every coding agent.</p>
          </div>
          <div className="footer-links">
            <a href="https://github.com/Rightglow/Railmux">GitHub</a>
            <a href="https://pypi.org/project/railmux/">PyPI</a>
            <a href="https://github.com/Rightglow/Railmux/releases">Releases</a>
            <a href="https://github.com/Rightglow/Railmux/blob/main/LICENSE">MIT License</a>
          </div>
          <p className="footer-meta">
            Built for Claude Code, Codex, and the terminals where real work
            happens.
          </p>
        </div>
      </footer>
    </div>
  );
}
