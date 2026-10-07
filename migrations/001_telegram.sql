CREATE TABLE IF NOT EXISTS wooplix_telegram_chats (
    bot_id TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    state JSONB NOT NULL DEFAULT '{"mode":"proposal","thread_id":null,"answers":{}}',
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bot_id, chat_id)
);

CREATE TABLE IF NOT EXISTS wooplix_telegram_updates (
    bot_id TEXT NOT NULL,
    update_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (bot_id, update_id)
);
