import asyncio

from app.storage import JsonStorage


def test_storage_keeps_users_sources_and_settings(tmp_path):
    async def scenario():
        path = tmp_path / "users.json"
        storage = JsonStorage(path, default_interval_seconds=300)
        await storage.initialize(frozenset({100}))

        admin = await storage.get_user(100)
        assert admin is not None
        assert admin.role == "admin"

        user, created = await storage.add_user(200, added_by=100, display_name="Test")
        assert created is True
        assert user.telegram_id == 200

        source = await storage.add_source(200, "https://example.com/news", "Example")
        await storage.set_user_field(200, "interval_seconds", 600)
        await storage.set_prompt(200, "processing", "Create the final text without shortening it.")

        reloaded = JsonStorage(path, default_interval_seconds=300)
        await reloaded.initialize(frozenset({100}))
        restored = await reloaded.get_user(200)
        assert restored is not None
        assert restored.settings.interval_seconds == 600
        assert (
            restored.settings.prompts.processing == "Create the final text without shortening it."
        )
        assert restored.sources[0].id == source.id

    asyncio.run(scenario())
