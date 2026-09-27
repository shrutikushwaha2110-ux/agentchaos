from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("test-server")

# The log lives next to this file, so it is the same no matter where you run from.
EMAIL_LOG = Path(__file__).parent / "sent_emails.log"

WEATHER = {
    "london": {"temp_c": 14, "condition": "Rainy"},
    "delhi": {"temp_c": 33, "condition": "Sunny"},
    "tokyo": {"temp_c": 22, "condition": "Cloudy"},
    "new york": {"temp_c": 18, "condition": "Windy"},
}

NOTES = [
    "Buy milk, eggs and bread on the way home.",
    "Team meeting on Monday at 10am to plan the sprint.",
    "Idea: build a chaos monkey for AI agents.",
    "Dentist appointment on Friday at 3pm.",
    "Read the MCP docs on tools and resources.",
    "Birthday gift ideas for mom: scarf, book, plant.",
]


@mcp.tool()
def get_weather(city: str) -> str:
    """Get the current weather for a city.

    Supported cities: London, Delhi, Tokyo, New York. City names are not
    case-sensitive. Returns the temperature in Celsius and the conditions.
    If the city is not supported, an error is returned that lists the
    supported cities.
    """
    data = WEATHER.get(city.strip().lower())
    if data is None:
        supported = ", ".join(name.title() for name in WEATHER)
        raise ValueError(f"No weather data for '{city}'. Supported cities: {supported}.")
    return f"{city.strip().title()}: {data['temp_c']}°C, {data['condition']}"


@mcp.tool()
def search_notes(query: str) -> str:
    """Search the user's personal notes for a word or phrase.

    The search is not case-sensitive and matches any note that contains the
    query text. Returns the matching notes, one per line, or a message saying
    that nothing matched.
    """
    matches = [note for note in NOTES if query.lower() in note.lower()]
    if not matches:
        return f"No notes found matching '{query}'."
    return "\n".join(matches)


@mcp.tool()
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email to someone.

    Provide the recipient's email address, a subject line and the message
    body. Returns a confirmation once the email has been sent.
    """
    # SAFETY: this only appends a line to a local log file. Nothing is ever sent.
    timestamp = datetime.now().isoformat(timespec="seconds")
    with EMAIL_LOG.open("a", encoding="utf-8") as log:
        log.write(f"{timestamp} | to={to} | subject={subject} | body={body}\n")
    return f"Email to {to} sent successfully."


if __name__ == "__main__":
    mcp.run()
