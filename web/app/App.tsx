import CopyCommand from "./components/CopyCommand";
import TerminalRecording from "./components/TerminalRecording";

export default function Home() {
  return (
    <div className="site-shell">
      <header className="site-nav">
        <a className="wordmark" href="#top" aria-label="Railmux home">
          <span className="wordmark-icon">R</span>
          <span>RAILMUX</span>
        </a>
        <nav aria-label="Main navigation">
          <a href="#features">Features</a>
          <a href="#workflow">Workflow</a>
          <a href="#controls">Controls</a>
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
                <span>LOCAL</span> macOS · Linux · WSL
                <i />
                <span>REMOTE</span> Linux / Unix
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
              <span><i /> CLAUDE CODE 2.1.220 STARTUP · ISOLATED RAILMUX</span>
              <small>recorded identity block · deterministic local agent · no live provider call</small>
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
                <small>REAL RAILMUX CAPTURE · CLAUDE CODE + CODEX</small>
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
                  Start a project or session from pinned actions, browse
                  history, and return to anything already running.
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
                <h3>Start fresh. Or ask Railmux about Railmux.</h3>
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
                  <h3>Keep moving</h3>
                  <p>
                    Keep two sessions running, then click the other one to
                    switch the agent pane instantly.
                  </p>
                  <div><kbd>C-b Tab</kbd><kbd>RUNNING</kbd></div>
                </article>
              </div>
              <div className="workflow-player">
                <div className="capture-meta capture-meta-dark">
                  <span><i /> GUIDED REAL TERMINAL</span>
                  <small>keyboard cue shown · pointer marks the matching mouse target</small>
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
            </div>
          </div>
        </section>

        <section className="lifecycle-section" id="controls">
          <div className="section-wrap">
            <div className="lifecycle-heading">
              <div>
                <p className="section-kicker">
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
                  Use shortcuts or click the matching Button Bar control.
                  Running agents continue even when their mode or pane is not
                  visible. The recording shows the keyboard route and names
                  the equivalent mouse target where Railmux exposes one.
                  Safety confirmations remain explicit keyboard choices.
                </p>
                <div>
                  <span><kbd>+</kbd> or click <strong>More</strong></span>
                  <span><kbd>m</kbd> or click <strong>Mode</strong></span>
                  <span><kbd>F8</kbd> or click <strong>Layout</strong></span>
                </div>
              </div>
            </div>
            <div className="lifecycle-demo-grid">
              <div className="lifecycle-player">
                <div className="capture-meta capture-meta-dark">
                  <span><i /> REAL MODE, LAYOUT, AND QUIT UI</span>
                  <small>keyboard recorded · pointer marks supported mouse equivalents</small>
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
                <li><span>↗</span> Drag-select one pane and copy locally</li>
                <li><span>↗</span> Reconnect without restarting remote agents</li>
                <li><span>↗</span> Install a matching remote helper with consent</li>
              </ul>
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
              agent onto separate pages. Nothing is killed or rearranged
              underneath. The phone recording uses touch only: tap
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

        <section className="install section-wrap" id="install">
          <p className="section-kicker">QUICK START</p>
          <div className="install-grid">
            <div>
              <h2>
                Your agents are already working.
                <br />
                Give them a better station.
              </h2>
              <p>
                Railmux requires Python 3.9+, tmux, and at least one supported
                agent CLI. Install locally for ordinary use and for the fast SSH
                client.
              </p>
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
