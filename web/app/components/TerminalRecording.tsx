import { useEffect, useRef, useState } from "react";
import * as AsciinemaPlayer from "asciinema-player";
import "asciinema-player/dist/bundle/asciinema-player.css";

type InputCue = {
  id: number;
  kind: "key" | "mouse" | "touch";
  label: string;
  detail: string;
  mouseDetail?: string;
  mouseClicks?: 1 | 2;
  left?: string;
  top?: string;
};

type TerminalRecordingProps = {
  source?: string;
  className?: string;
  poster?: string;
  startAt?: number | string;
  controls?: boolean;
  loop?: boolean;
  autoPlay?: boolean;
  playWhenVisible?: boolean;
  inputHud?: boolean;
  cueCols?: number;
  cueRows?: number;
  idleTimeLimit?: number;
  dataDemo?: string;
};

function parseInputCue(
  data: string,
  id: number,
  cols: number,
  rows: number,
): InputCue | null {
  const parts = data.split("|");
  if (
    parts[0] === "keymouse"
    && parts.length >= 7
    && parts[5] === "mouse"
  ) {
    const x = Number(parts[2]);
    const y = Number(parts[3]);
    if (
      !Number.isFinite(x)
      || !Number.isFinite(y)
      || x < 1
      || x > cols
      || y < 1
      || y > rows
    ) return null;
    return {
      id,
      kind: "key",
      label: parts[1],
      detail: parts[4],
      mouseDetail: parts.slice(6).join(" · "),
      mouseClicks: parts.slice(6).join(" · ").toLowerCase().startsWith(
        "double-click",
      ) ? 2 : 1,
      left: `${((x - 0.5) / cols) * 100}%`,
      top: `${((y - 0.5) / rows) * 100}%`,
    };
  }
  if (parts[0] === "key" && parts.length >= 3) {
    return {
      id,
      kind: "key",
      label: parts[1],
      detail: parts.slice(2).join(" · "),
    };
  }
  if (
    (parts[0] === "mouse" || parts[0] === "touch")
    && parts.length >= 4
  ) {
    const x = Number(parts[1]);
    const y = Number(parts[2]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    const detail = parts.slice(3).join(" · ");
    const isDoubleClick = detail.toLowerCase().startsWith("double-click");
    const isRightClick = detail.toLowerCase().startsWith("right-click");
    return {
      id,
      kind: parts[0],
      label: parts[0] === "touch"
        ? "TAP"
        : isDoubleClick
          ? "DOUBLE-CLICK"
          : isRightClick
            ? "RIGHT-CLICK"
            : "CLICK",
      detail,
      mouseClicks: isDoubleClick ? 2 : 1,
      left: `${((x - 0.5) / cols) * 100}%`,
      top: `${((y - 0.5) / rows) * 100}%`,
    };
  }
  return null;
}

export default function TerminalRecording({
  source = "railmux-demo.cast",
  className = "",
  poster,
  startAt,
  controls = true,
  loop = false,
  autoPlay = false,
  playWhenVisible = false,
  inputHud = false,
  cueCols = 80,
  cueRows = 24,
  idleTimeLimit = 3,
  dataDemo,
}: TerminalRecordingProps) {
  const container = useRef<HTMLDivElement>(null);
  const cueTimer = useRef<number | null>(null);
  const cueId = useRef(0);
  const [cue, setCue] = useState<InputCue | null>(null);

  useEffect(() => {
    if (!container.current) return;
    const player = AsciinemaPlayer.create(
      `${import.meta.env.BASE_URL}generated/${source}`,
      container.current,
      {
        autoPlay,
        loop,
        poster,
        startAt,
        idleTimeLimit,
        fit: "width",
        controls,
        cursorMode: "hidden",
        keystrokeOverlay: false,
        terminalFontFamily:
          '"SFMono-Regular", "Cascadia Code", "Liberation Mono", Menlo, monospace',
      },
    );
    if (inputHud) {
      player.addEventListener("input", ({ data }) => {
        const next = parseInputCue(
          data,
          ++cueId.current,
          cueCols,
          cueRows,
        );
        if (!next) return;
        setCue(next);
        if (cueTimer.current !== null) window.clearTimeout(cueTimer.current);
        cueTimer.current = window.setTimeout(() => {
          setCue(null);
          cueTimer.current = null;
        }, 2_200);
      });
    }
    const observer = playWhenVisible
      ? new IntersectionObserver(
          ([entry]) => {
            if (entry.isIntersecting) {
              void player.play();
              observer?.disconnect();
            }
          },
          { threshold: 0.45 },
        )
      : null;
    if (observer && container.current) observer.observe(container.current);
    return () => {
      observer?.disconnect();
      if (cueTimer.current !== null) window.clearTimeout(cueTimer.current);
      player.dispose();
    };
  }, [
    autoPlay,
    controls,
    cueCols,
    cueRows,
    inputHud,
    idleTimeLimit,
    loop,
    playWhenVisible,
    poster,
    source,
    startAt,
  ]);

  return (
    <div
      className={`terminal-recording-player ${className}`.trim()}
      data-demo={dataDemo}
    >
      <div className="terminal-recording-host" ref={container} />
      {inputHud && cue ? (
        <div
          className={`terminal-input-layer terminal-input-${cue.kind}`}
          data-input-kind={cue.kind}
          key={cue.id}
        >
          {cue.kind !== "touch" && cue.left && cue.top ? (
            <span
              className="terminal-pointer"
              data-testid="terminal-pointer"
              data-clicks={cue.mouseClicks ?? 1}
              style={{ left: cue.left, top: cue.top }}
            >
              <i />
              {cue.mouseClicks === 2 ? <i className="second-click" /> : null}
              {cue.mouseClicks === 2 ? (
                <b aria-hidden="true" data-testid="terminal-double-click">×2</b>
              ) : null}
            </span>
          ) : null}
          {cue.kind === "touch" ? (
            <span
              className="terminal-touch"
              data-testid="terminal-touch"
              style={{ left: cue.left, top: cue.top }}
            >
              <i />
            </span>
          ) : null}
          <div
            className="terminal-input-hud"
            data-testid="terminal-input-hud"
            aria-live="polite"
          >
            <span>
              {cue.kind === "mouse"
                ? "MOUSE"
                : cue.kind === "touch"
                  ? "TOUCH"
                  : cue.mouseDetail
                    ? "KEY + MOUSE"
                    : "KEY"}
            </span>
            <strong>
              {cue.label}
              {cue.kind === "key" && cue.mouseClicks === 2
                ? " / DOUBLE-CLICK"
                : null}
            </strong>
            <small>
              {cue.detail}
              {cue.mouseDetail ? (
                <em data-testid="terminal-mouse-target">
                  Mouse: {cue.mouseDetail}
                </em>
              ) : null}
            </small>
          </div>
        </div>
      ) : null}
    </div>
  );
}
