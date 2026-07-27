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
              <span><i /> REAL AGENT RESPONSE · ISOLATED RAILMUX</span>
              <small>captured once · replayed without provider credentials</small>
            </div>
            <TerminalRecording
              className="hero-terminal-recording"
              poster="npt:8"
              startAt={7.4}
              autoPlay
              controls
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
            <p>Everything important remains visible. Everything running remains alive.</p>
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
                <img
                  src={`${import.meta.env.BASE_URL}generated/dual-agent-workspace.png`}
                  alt="A real Railmux terminal with two agent panes"
                />
                <small>REAL CAPTURE · SIDE-BY-SIDE LAYOUT</small>
              </div>
            </article>
            <article className="feature-showcase feature-showcase-sidebar">
              <div
                className="feature-sidebar-evidence"
                role="img"
                aria-label="Railmux sidebar showing New project, New session, and a running agent"
              >
                <TerminalRecording
                  source="railmux-workflow-demo.cast"
                  className="sidebar-evidence-recording"
                  poster="npt:5.2"
                  controls={false}
                  dataDemo="sidebar-evidence"
                />
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
                <h3>Every session stays alive.</h3>
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
                  <h3>Browse</h3>
                  <p>See projects, history, and everything already running.</p>
                  <kbd>MOUSE</kbd>
                </article>
                <article>
                  <span>02</span>
                  <h3>Open</h3>
                  <p>Launch a new agent without leaving the workspace.</p>
                  <div><kbd>MOUSE</kbd><kbd>NEW SESSION</kbd></div>
                </article>
                <article>
                  <span>03</span>
                  <h3>Keep moving</h3>
                  <p>Return to the sidebar, then reopen the running agent.</p>
                  <div><kbd>C-b Tab</kbd><kbd>RUNNING</kbd></div>
                </article>
              </div>
              <div className="workflow-player">
                <div className="capture-meta capture-meta-dark">
                  <span><i /> GUIDED REAL TERMINAL</span>
                  <small>mouse and key cues stay visible during each action</small>
                </div>
                <TerminalRecording
                  source="railmux-workflow-demo.cast"
                  className="workflow-terminal-recording"
                  poster="npt:0.6"
                  controls
                  playWhenVisible
                  inputHud
                  cueCols={76}
                  cueRows={30}
                  dataDemo="workflow-recording"
                />
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
              underneath.
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
              REAL 46×26 TERMINAL
            </div>
            <TerminalRecording
              source="railmux-mobile-demo.cast"
              className="mobile-terminal-recording"
              poster="npt:3.5"
              controls={false}
              loop
              playWhenVisible
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
