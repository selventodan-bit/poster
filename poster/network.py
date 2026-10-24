"""Bounded decoded HTTP reads shared by Workflow v1 and Maker graphs."""


class ResponseTooLarge(ValueError):
    pass


async def bounded_body(response, limit):
    chunks, size = [], 0
    async for chunk in response.aiter_bytes(chunk_size=65536):
        size += len(chunk)
        if size > limit:
            raise ResponseTooLarge("HTTP_RESPONSE_LIMIT")
        chunks.append(chunk)
    return b"".join(chunks)
