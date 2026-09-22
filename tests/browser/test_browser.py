"""Real-browser tests (Chromium via Playwright) against a development-mode server.

Two independent browser contexts per applet (separate cookie jars and
storage, like two visitors) prove that chat, drawing and the game actually
interact through the WebSocket backend, that HTML-like chat text stays inert,
and that the manual Reconnect control restores a working session after the
backend restarts.

Skipped unless RUN_BROWSER_TESTS=1 or a Playwright Chromium is available.
Everything runs on ephemeral loopback ports; nothing here touches production.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator

import pytest

from tests.conftest import REPO_ROOT, ServerProcess, base_env, free_port

pytestmark = [pytest.mark.browser, pytest.mark.slow]

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright  # noqa: E402

PAYLOAD = '<img src=x onerror="document.title=\'XSS\'"><script>document.title="XSS2"</script><b>bold?</b>'


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        # CI installs the matching Chromium with `playwright install`; locally a
        # different Chromium build can be pointed to with CHROMIUM_EXECUTABLE.
        launch_options = {}
        if os.environ.get("CHROMIUM_EXECUTABLE"):
            launch_options["executable_path"] = os.environ["CHROMIUM_EXECUTABLE"]
        try:
            instance = pw.chromium.launch(**launch_options)
        except Exception as exc:
            if os.environ.get("RUN_BROWSER_TESTS") == "1":
                raise
            pytest.skip(f"Chromium not available: {exc}")
        yield instance
        instance.close()


def new_context(browser: Browser) -> BrowserContext:
    context = browser.new_context(viewport={"width": 900, "height": 700})
    context.set_default_timeout(10_000)
    return context


def wait_connected(page: Page) -> None:
    page.wait_for_selector(".demo-status[data-state='open']")


def start_server(port: int) -> ServerProcess:
    env = base_env()
    env.update({"HOST": "127.0.0.1", "PORT": str(port), "SERVE_STATIC": "1"})
    server = ServerProcess(REPO_ROOT, env)
    server.wait_ready()
    return server


@pytest.fixture
def server() -> Iterator[ServerProcess]:
    instance = start_server(free_port())
    try:
        yield instance
    finally:
        instance.stop()


def test_landing_page_links_to_the_three_applets(browser: Browser, server: ServerProcess):
    ctx = new_context(browser)
    page = ctx.new_page()
    page.goto(f"{server.base_url}/")
    assert page.title() == "Lightsail Tower"
    hrefs = page.eval_on_selector_all(".links a", "els => els.map(e => e.getAttribute('href'))")
    assert hrefs == ["chat/", "draw/", "game/"]
    assert "Public shared demo" in page.inner_text("body")
    for href in hrefs:
        page.goto(f"{server.base_url}/{href}")
        wait_connected(page)
    ctx.close()


def test_chat_between_two_browsers_renders_html_like_text_inertly(browser: Browser, server: ServerProcess):
    a, b = new_context(browser), new_context(browser)
    page_a, page_b = a.new_page(), b.new_page()
    for page in (page_a, page_b):
        page.goto(f"{server.base_url}/chat/index.html")
        wait_connected(page)
        assert "not private messaging" in page.inner_text(".demo-notice")

    page_a.fill("#name", "Ada <i>x</i>")
    page_a.fill("#msg", PAYLOAD)
    page_a.press("#msg", "Enter")

    # Sender sees "You: ..." as text, receiver sees "name: text" as text.
    page_a.wait_for_selector("#chat p")
    page_b.wait_for_selector("#chat p")
    assert page_a.inner_text("#chat p") == f"You: {PAYLOAD}"
    assert page_b.inner_text("#chat p") == f"Ada <i>x</i>: {PAYLOAD}"
    for page in (page_a, page_b):
        # No element was created from the message; it is a single text node.
        assert page.eval_on_selector("#chat p", "p => p.children.length") == 0
        assert page.eval_on_selector("#chat p", "p => p.childNodes.length") == 1
        assert page.eval_on_selector("#chat p", "p => p.childNodes[0].nodeType") == 3
        assert page.eval_on_selector("#chat p", "p => p.innerHTML").startswith(
            ("You: &lt;img", "Ada &lt;i&gt;")
        )
        assert page.query_selector("#chat img") is None
        assert page.query_selector("#chat script") is None
        assert page.title() == "WebSocket Chat"  # the onerror/script payloads never ran
    assert page_a.eval_on_selector_all("#chat p", "els => els.length") == 1
    assert page_b.eval_on_selector_all("#chat p", "els => els.length") == 1

    page_b.fill("#msg", "reply from b")
    page_b.click("button.send")
    page_a.wait_for_selector("#chat p:nth-child(2)")
    assert page_a.inner_text("#chat p:nth-child(2)") == "Anonymous: reply from b"
    a.close()
    b.close()


def canvas_ink(page: Page) -> int:
    return page.evaluate(
        """() => {
          const c = document.getElementById('drawCanvas');
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
          return n;
        }"""
    )


def test_drawing_and_clear_propagate_between_two_browsers(browser: Browser, server: ServerProcess):
    a, b = new_context(browser), new_context(browser)
    page_a, page_b = a.new_page(), b.new_page()
    for page in (page_a, page_b):
        page.goto(f"{server.base_url}/draw/")
        wait_connected(page)
    assert canvas_ink(page_b) == 0

    page_a.click("button[data-color='red']")
    box = page_a.locator("#drawCanvas").bounding_box()
    x0, y0 = box["x"] + box["width"] * 0.2, box["y"] + box["height"] * 0.3
    page_a.mouse.move(x0, y0)
    page_a.mouse.down()
    for step in range(1, 21):
        page_a.mouse.move(x0 + step * 8, y0 + step * 4)
    page_a.mouse.up()
    assert canvas_ink(page_a) > 0

    deadline = time.time() + 5
    while time.time() < deadline and canvas_ink(page_b) == 0:
        page_b.wait_for_timeout(50)
    assert canvas_ink(page_b) > 0, "stroke did not reach the second browser"

    page_b.click("#clear")
    deadline = time.time() + 5
    while time.time() < deadline and canvas_ink(page_a) > 0:
        page_a.wait_for_timeout(50)
    assert canvas_ink(page_a) == 0, "clear did not reach the first browser"
    a.close()
    b.close()


def game_state(page: Page) -> dict:
    return page.evaluate(
        "() => ({worms: window.__lightsailGame.worms, others: Object.values(window.__lightsailGame.otherPlayers), "
        "sprite: window.__lightsailGame.sprite, connected: window.__lightsailGame.connected})"
    )


def test_game_two_players_see_each_other_eat_worms_and_handle_disconnect(
    browser: Browser, server: ServerProcess
):
    a, b = new_context(browser), new_context(browser)
    page_a, page_b = a.new_page(), b.new_page()
    for page in (page_a, page_b):
        page.goto(f"{server.base_url}/game/")
        wait_connected(page)
    page_a.wait_for_function("() => window.__lightsailGame.worms.length === 10")
    page_b.wait_for_function("() => window.__lightsailGame.worms.length === 10")
    assert game_state(page_a)["worms"] == game_state(page_b)["worms"]

    # Both players registered with each other on connect.
    page_a.wait_for_function("() => Object.keys(window.__lightsailGame.otherPlayers).length === 1")
    page_b.wait_for_function("() => Object.keys(window.__lightsailGame.otherPlayers).length === 1")
    other_seen_by_b = game_state(page_b)["others"][0]
    assert other_seen_by_b["sprite"] == game_state(page_a)["sprite"]
    assert other_seen_by_b["sprite"] in ("Robin", "Cardinal", "Pigeon", "Woodpecker")
    assert "image" not in other_seen_by_b

    # Movement: A walks right; B sees A's position update with velocity.
    page_a.keyboard.down("ArrowRight")
    page_a.wait_for_timeout(300)
    page_a.keyboard.up("ArrowRight")
    page_b.wait_for_function("() => Object.values(window.__lightsailGame.otherPlayers)[0].posX > 0")

    # Eat: teleport A onto worm 0; the server respawns it for everyone.
    worms_before = game_state(page_a)["worms"]
    wx, wy = worms_before[0]
    page_a.evaluate(f"() => window.__lightsailGame.setPosition({wx}, {wy})")
    page_a.wait_for_function(
        f"() => JSON.stringify(window.__lightsailGame.worms[0]) !== JSON.stringify({json.dumps(worms_before[0])})"
    )
    page_b.wait_for_function(
        f"() => JSON.stringify(window.__lightsailGame.worms[0]) !== JSON.stringify({json.dumps(worms_before[0])})"
    )
    # Both clients hold the same shared positions (another worm within reach of
    # the new position may legitimately have been eaten too).
    assert game_state(page_a)["worms"] == game_state(page_b)["worms"]
    assert len(game_state(page_a)["worms"]) == 10

    # Disconnect: closing A's context removes A from B's world.
    a.close()
    page_b.wait_for_function("() => Object.keys(window.__lightsailGame.otherPlayers).length === 0")
    b.close()


def test_manual_reconnect_after_backend_restart_restores_registration(browser: Browser):
    port = free_port()
    server = start_server(port)
    a, b = new_context(browser), new_context(browser)
    try:
        page_a, page_b = a.new_page(), b.new_page()
        for page in (page_a, page_b):
            page.goto(f"http://127.0.0.1:{port}/game/")
            wait_connected(page)
        page_b.wait_for_function("() => Object.keys(window.__lightsailGame.otherPlayers).length === 1")
        loops_before = page_a.evaluate("() => { window.__frames = 0; return true; }")
        assert loops_before

        # Backend restarts (like a deployment): both pages report the close.
        server.stop()
        for page in (page_a, page_b):
            page.wait_for_selector(".demo-status[data-state='closed']")
            assert "Disconnected" in page.inner_text(".demo-status .text")
            assert page.is_visible(".demo-status button.reconnect")
            assert "server shutting down" in page.inner_text(".demo-status .text")
        assert game_state(page_a)["others"] == []
        # Sending while closed is a no-op (no queue, no error).
        page_a.keyboard.press("ArrowLeft")

        server = start_server(port)
        page_a.click(".demo-status button.reconnect")
        page_b.click(".demo-status button.reconnect")
        wait_connected(page_a)
        wait_connected(page_b)
        # Registration ran again: each sees exactly one other player, not two.
        page_a.wait_for_function("() => Object.keys(window.__lightsailGame.otherPlayers).length === 1")
        page_b.wait_for_function("() => Object.keys(window.__lightsailGame.otherPlayers).length === 1")
        page_a.wait_for_timeout(300)
        assert len(game_state(page_a)["others"]) == 1
        assert len(game_state(page_b)["others"]) == 1
        # Clicking Reconnect while connected does not open a second socket.
        page_a.click(".demo-status button.reconnect", force=True)
        page_a.wait_for_timeout(300)
        assert len(game_state(page_b)["others"]) == 1
    finally:
        a.close()
        b.close()
        server.stop()


def test_chat_send_while_disconnected_shows_notice_not_error(browser: Browser):
    port = free_port()
    server = start_server(port)
    ctx = new_context(browser)
    errors: list[str] = []
    try:
        page = ctx.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"http://127.0.0.1:{port}/chat/")
        wait_connected(page)
        server.stop()
        page.wait_for_selector(".demo-status[data-state='closed']")
        page.fill("#msg", "hello?")
        page.press("#msg", "Enter")
        page.wait_for_selector("#chat p")
        assert "Not connected" in page.inner_text("#chat p")
        assert page.input_value("#msg") == "hello?"  # kept for retry
        assert errors == []
    finally:
        ctx.close()
        server.stop()
