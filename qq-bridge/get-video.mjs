import { readFileSync, writeFileSync } from "node:fs";
import { WsClient } from "./ws-lite.mjs";
const cfg = JSON.parse(readFileSync(new URL("./config.json", import.meta.url), "utf8"));
const headers = {}; if (cfg.onebot?.accessToken) headers.Authorization = `Bearer ${cfg.onebot.accessToken}`;
const sock = WsClient.connect(cfg.onebot.wsUrl, { headers });
sock.once("open", () => sock.send(JSON.stringify({ action: "get_msg", params: { message_id: 1066194180 }, echo: "g1" })));
sock.on("message", (raw) => {
  const m = JSON.parse(raw);
  if (m.echo !== "g1") return;
  const seg = (m?.data?.message ?? []).find((s) => s.type === "video");
  writeFileSync("D:/dsh 大肥鱼/qq-bridge/cache/video-url.txt", seg?.data?.url ?? "", "utf8");
  console.log("retcode", m.retcode, "hasUrl", Boolean(seg?.data?.url));
  process.exit(0);
});
setTimeout(() => { console.error("timeout"); process.exit(1); }, 15000);