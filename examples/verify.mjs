import { createRequire } from "node:module";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(resolve(process.env.GW_NODE_SDK_DIR || here, "package.json"));
const OpenAI = require("openai").default;
if (!process.argv[2]) throw new Error("Usage: node verify.mjs <client-key.txt> [model] [report.json]");
const client = new OpenAI({
  baseURL: process.env.GATEWAY_BASE_URL || "http://127.0.0.1:8787/v1",
  apiKey: readFileSync(process.argv[2], "utf8").trim(),
  maxRetries: 0,
  timeout: 180000,
});
const model = process.argv[3] || "passerelle-default";
const response = await client.responses.create({model, input: "Réponds uniquement par BONJOUR."});
if (response.status !== "completed" || !response.output_text.toUpperCase().includes("BONJOUR")) {
  throw new Error("Responses JSON acceptance failed");
}
let text = "", finish = null;
const stream = await client.chat.completions.create({model, stream: true,
  messages: [{role: "user", content: "Réponds uniquement par SALUT."}]});
for await (const chunk of stream) {
  if (chunk.choices.length) {
    text += chunk.choices[0].delta.content || "";
    finish = chunk.choices[0].finish_reason || finish;
  }
}
if (finish !== "stop" || !text.toUpperCase().includes("SALUT")) throw new Error("Chat SSE acceptance failed");
const evidence = {at: new Date().toISOString(), backend: "chatgpt_oauth", sdk: "openai-js 7.47.0", model,
  responses_json: {status: response.status, text: response.output_text},
  chat_sse: {finish_reason: finish, text}};
if (process.argv[4]) writeFileSync(process.argv[4], JSON.stringify(evidence, null, 2));
console.log(JSON.stringify(evidence));
