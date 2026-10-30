"""Alembic migrations. Helpers shared by migration scripts live here."""


def split_sql(script: str) -> list[str]:
    """Split a SQL script into statements on ';', ignoring semicolons inside $$ bodies,
    quotes and -- comments. asyncpg cannot run several statements in one prepared call."""
    out, buf, i, n = [], [], 0, len(script)
    in_dollar = in_quote = False
    while i < n:
        ch = script[i]
        if not in_quote and script.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        if not in_dollar and ch == "'":
            in_quote = not in_quote
        if not in_dollar and not in_quote and script.startswith("--", i):
            end = script.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch == ";" and not in_dollar and not in_quote:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out
