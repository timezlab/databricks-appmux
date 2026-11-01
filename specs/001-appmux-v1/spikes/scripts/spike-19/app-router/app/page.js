"use client";

import { useEffect, useState } from "react";

const STATUS_URL = `${process.env.NEXT_PUBLIC_BASE_PATH}/api/status`;
const POLL_INTERVAL_MS = 500;

export default function Page() {
  const [status, setStatus] = useState("loading");
  const [pollCount, setPollCount] = useState(0);

  useEffect(() => {
    let stopped = false;
    async function poll() {
      const response = await fetch(STATUS_URL, { cache: "no-store" });
      const body = await response.json();
      if (!stopped) {
        setStatus(`${body.state} #${body.counter}`);
        setPollCount((count) => count + 1);
      }
    }
    poll();
    const timer = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <main>
      <h1>appmux spike 19</h1>
      <p id="status">{status}</p>
      <p id="polls">{pollCount}</p>
    </main>
  );
}
