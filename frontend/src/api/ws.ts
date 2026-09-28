export function connectWs(onMessage: (message: string) => void): () => void {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/ws`);
  ws.onmessage = (event) => onMessage(String(event.data));
  return () => ws.close();
}
