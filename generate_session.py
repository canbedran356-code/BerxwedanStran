import asyncio
import os

# Pyrogram currently expects a default event loop during import on Windows/Python 3.14.
asyncio.set_event_loop(asyncio.new_event_loop())

from dotenv import load_dotenv
from pyrogram import Client


async def main() -> None:
    load_dotenv()
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]

    async with Client(
        "assistant_session",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
    ) as app:
        session = await app.export_session_string()
        print("\nAssistant session başarıyla oluşturuldu.\n")
        print("ASSISTANT_SESSION:\n")
        print(session)
        print("\nBu değeri .env dosyasındaki ASSISTANT_SESSION alanına ekleyin.")


if __name__ == "__main__":
    asyncio.run(main())
