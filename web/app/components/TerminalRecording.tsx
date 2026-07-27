import { useEffect, useRef } from "react";
import * as AsciinemaPlayer from "asciinema-player";
import "asciinema-player/dist/bundle/asciinema-player.css";

type TerminalRecordingProps = {
  source?: string;
  className?: string;
  poster?: string;
  controls?: boolean;
  loop?: boolean;
  autoPlay?: boolean;
  playWhenVisible?: boolean;
  keystrokeOverlay?: boolean;
  dataDemo?: string;
};

export default function TerminalRecording({
  source = "railmux-demo.cast",
  className = "",
  poster,
  controls = true,
  loop = false,
  autoPlay = false,
  playWhenVisible = false,
  keystrokeOverlay = false,
  dataDemo,
}: TerminalRecordingProps) {
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!container.current) return;
    const player = AsciinemaPlayer.create(
      `${import.meta.env.BASE_URL}generated/${source}`,
      container.current,
      {
        autoPlay,
        loop,
        poster,
        idleTimeLimit: 1.5,
        fit: "width",
        controls,
        cursorMode: "hidden",
        keystrokeOverlay,
        terminalFontFamily:
          '"SFMono-Regular", "Cascadia Code", "Liberation Mono", Menlo, monospace',
      },
    );
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
      player.dispose();
    };
  }, [
    autoPlay,
    controls,
    keystrokeOverlay,
    loop,
    playWhenVisible,
    poster,
    source,
  ]);

  return (
    <div
      className={`terminal-recording-player ${className}`.trim()}
      data-demo={dataDemo}
      ref={container}
    />
  );
}
