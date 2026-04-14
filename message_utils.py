"""Telegram message utilities."""

MAX_MESSAGE_LENGTH = 4096


def chunk_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a message into chunks that fit within Telegram's character limit.

    Tries to split at paragraph boundaries (double newlines) to keep messages readable.
    """
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        # Try to split at the last paragraph boundary within the limit
        split_at = text.rfind("\n\n", 0, max_length)
        if split_at == -1:
            # Fallback: hard split at max_length
            split_at = max_length
        else:
            split_at += 2  # Include the \n\n

        chunks.append(text[:split_at])
        text = text[split_at:]

    return chunks


async def send_chunked_message(message, text: str, **kwargs) -> None:
    """Send a potentially long message by splitting it into chunks."""
    for chunk in chunk_message(text):
        await message.reply_text(chunk, **kwargs)
