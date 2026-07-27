import CopyCommand from "./components/CopyCommand";
import { CompactDemo, DesktopDemo } from "./components/ProductDemo";
import TerminalRecording from "./components/TerminalRecording";

const features = [
  {
    number: "01",
    title: "Every session stays alive",
    copy: "Switch projects or agents without interrupting a response. Railmux keeps each coding session in its own tmux-backed workspace.",
  },
  {
    number: "02",
    title: "Two agents, one workspace",
    copy: "Place Codex and Claude Code side by side or stacked. Focus, target, and resize panes without losing either agent.",
  },
  {
    number: "03",
    title: "A sidebar that knows your work",
    copy: "Browse projects, filter history, star important sessions, and jump directly to anything already running.",
  },
  {
    number: "04",
    title: "Designed to recover",
    copy: "Soft restart restores your workspace while watchdog diagnostics report failures without killing provider sessions.",
  },
];

const workflow = [
  ["Browse", "Projects and session history from both supported agents appear in one keyboard- and mouse-friendly sidebar."],
  ["Open", "Enter resumes a session. Space previews it. A second agent slot is ready whenever you need parallel work."],
  ["Keep moving", "Detach, reconnect, resize, or switch terminals. The work stays on the machine where it started."],
];

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
          <a href="#ssh">SSH</a>
          <a href="#workflow">Workflow</a>
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
            <DesktopDemo />
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

        <section className="recording-section">
          <div className="section-wrap recording-grid">
            <div className="recording-copy">
              <p className="section-kicker section-kicker-light">
                REAL TERMINAL CAPTURE
              </p>
              <h2>Watch the actual tmux UI.</h2>
              <p>
                Rebuilt automatically from this checkout in an isolated home
                directory, using synthetic session history and no provider
                credentials.
              </p>
            </div>
            <TerminalRecording />
          </div>
        </section>

        <section className="features section-wrap" id="features">
          <div className="section-heading">
            <p className="section-kicker">WHAT IT DOES</p>
            <h2>Stay in flow.</h2>
            <p>Everything important remains visible. Everything running remains alive.</p>
          </div>
          <div className="feature-grid">
            {features.map((feature) => (
              <article className="feature-card" key={feature.number}>
                <span>{feature.number}</span>
                <div className="feature-glyph" aria-hidden="true">
                  <i />
                  <i />
                  <i />
                </div>
                <h3>{feature.title}</h3>
                <p>{feature.copy}</p>
              </article>
            ))}
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
              <div className="packet packet-one">
                <span>FRAME 1842</span>
                <i>superseded</i>
              </div>
              <div className="packet packet-two">
                <span>FRAME 1843</span>
                <i>superseded</i>
              </div>
              <div className="packet packet-live">
                <span>LATEST STATE</span>
                <b>12 changed rows</b>
                <small>2.1 KiB compressed</small>
              </div>
              <div className="connection-line"><i /></div>
              <div className="remote-screen">
                <span>LOCAL TERMINAL</span>
                <div />
                <div />
                <div />
                <small>paint newest state</small>
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
          <CompactDemo />
        </section>

        <section className="workflow" id="workflow">
          <div className="section-wrap">
            <div className="section-heading workflow-heading">
              <p className="section-kicker section-kicker-light">ONE LOOP</p>
              <h2>Browse. Open. Keep moving.</h2>
            </div>
            <div className="workflow-steps">
              {workflow.map(([title, copy], index) => (
                <article key={title}>
                  <span>0{index + 1}</span>
                  <h3>{title}</h3>
                  <p>{copy}</p>
                </article>
              ))}
            </div>
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
