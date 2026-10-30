import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body.get("messages", [])
        texts = 0
        images = 0
        audios = 0
        last_user_text = ""
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                texts += 1
                if m.get("role") == "user":
                    last_user_text = content[:150]
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        texts += 1
                        if m.get("role") == "user":
                            last_user_text = part.get("text", "")[:150]
                    elif part.get("type") == "image_url":
                        images += 1
                    elif part.get("type") == "input_audio":
                        audios += 1
        reply = "[mock] {} messages received ({} text, {} image parts, {} audio parts). Last user text: {}".format(
            len(messages), texts, images, audios, last_user_text)
        print("[mock-llm] req: {} messages, {} text, {} images, {} audios, last: {}".format(
            len(messages), texts, images, audios, last_user_text[:80]), flush=True)
        out = json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8189), MockHandler).serve_forever()
