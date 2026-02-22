import asyncio, json, websockets

async def test():
    async with websockets.connect("ws://localhost:8000/ws/hydro-analysis") as ws:
        await ws.send(json.dumps({
            "query": "Analyze pH at -92.414165705, 46.682323895 from 2026-01-10 to 2026-01-15",
            "model_name": "llama3:8b"
        }))
        async for msg in ws:
            data = json.loads(msg)
            print(f"[{data['type']}]", list(data.keys()))
            if data["type"] == "agent_done":
                # 验证新字段存在
                assert "prompt_used" in data, "❌ 缺少 prompt_used"
                assert "llm_raw_output" in data, "❌ 缺少 llm_raw_output"
                print("  ✅ prompt_used 长度:", len(data.get("prompt_used") or ""))
                print("  ✅ llm_raw_output 长度:", len(data.get("llm_raw_output") or ""))
            if data["type"] in ("done", "error"):
                break

asyncio.run(test())