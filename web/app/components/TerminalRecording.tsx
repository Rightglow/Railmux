import { useEffect, useRef } from "react";
import * as AsciinemaPlayer from "asciinema-player";
import "asciinema-player/dist/bundle/asciinema-player.css";

export default function TerminalRecording() {
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!container.current) return;
    const player = AsciinemaPlayer.create(
      `${import.meta.env.BASE_URL}generated/railmux-demo.cast`,
      container.current,
      {
        autoPlay: true,
        loop: true,
        idleTimeLimit: 1.5,
        fit: "width",
        controls: true,
        cursorMode: "hidden",
        terminalFontFamily:
          '"SFMono-Regular", "Cascadia Code", "Liberation Mono", Menlo, monospace',
      },
    );
    return () => player.dispose();
  }, []);

  return <div className="terminal-recording-player" ref={container} />;
}
