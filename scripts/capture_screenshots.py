"""Capture README screenshots from the live Hugging Face Space with headless Chrome.

    python scripts/capture_screenshots.py [--url https://huggingface.co/spaces/<user>/<space>]

Shoots the huggingface.co Space page (header included), driving the Gradio app
inside its iframe: the landing view, an answered question, the validated SQL,
and the guardrail on a delete request. Each image is cropped to the content.
Asks two real questions, so it uses a little of the Space's Groq quota.
"""
import argparse
import sys
import time
from pathlib import Path

from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
WIDTH, HEIGHT = 1500, 2400

ANSWER_EXAMPLE = "Which departments were over budget"
GUARDRAIL_EXAMPLE = "Delete all the void invoices"


def wait_for(fn, timeout: float, what: str):
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = fn()
            if value:
                return value
        except WebDriverException:
            pass
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for {what}")


class Space:
    def __init__(self, driver, url: str):
        self.d = driver
        self.d.get(url)
        self.frame = wait_for(lambda: next((f for f in self.d.find_elements(By.TAG_NAME, "iframe")
                                            if "hf.space" in (f.get_attribute("src") or "")), None),
                              120, "the app iframe")
        self.frame_top = self.frame.location["y"]
        self.d.switch_to.frame(self.frame)
        wait_for(lambda: self.d.find_elements(By.TAG_NAME, "textarea"), 240, "the app to load")
        wait_for(lambda: "610 tables" in self.d.page_source, 240, "the warehouse summary")
        time.sleep(3)

    def button(self, prefix: str):
        return wait_for(lambda: next((b for b in self.d.find_elements(By.TAG_NAME, "button")
                                      if b.text.strip().startswith(prefix)), None), 30, f"button {prefix!r}")

    def click(self, prefix: str) -> None:
        self.d.execute_script("arguments[0].click();", self.button(prefix))
        time.sleep(1)

    def meta(self) -> str:
        els = self.d.find_elements(By.XPATH, "//*[contains(text(), 'schema tokens sent')]")
        return els[0].text if els else ""

    def ask_example(self, prefix: str, timeout: float = 180) -> None:
        before = self.meta()
        self.click(prefix)      # fills the question box
        self.click("Ask")
        wait_for(lambda: self.meta() and self.meta() != before, timeout, "the agent to answer")
        time.sleep(3)

    def content_bottom(self) -> int:
        return int(self.d.execute_script(
            "const els = [...document.querySelectorAll('*')].filter(e => e.textContent.includes('Synthetic data') "
            "&& !e.children.length); return els.length ? els[els.length-1].getBoundingClientRect().bottom : "
            "document.body.scrollHeight;"))

    def shot(self, name: str) -> None:
        time.sleep(1.5)
        bottom = min(self.frame_top + self.content_bottom() + 28, HEIGHT)
        self.d.switch_to.default_content()
        path = OUT / name
        self.d.save_screenshot(str(path))
        with Image.open(path) as img:
            img.crop((0, 0, img.width, bottom)).save(path, optimize=True)
        self.d.switch_to.frame(self.frame)
        print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size // 1024} KB, {bottom}px tall)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://huggingface.co/spaces/Prashantm99/finsql-agent")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument(f"--window-size={WIDTH},{HEIGHT}")
    options.add_argument("--hide-scrollbars")
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_window_size(WIDTH, HEIGHT)
        print(f"loading {args.url} ...")
        space = Space(driver, args.url)
        print("capturing:")
        space.shot("01-landing.png")

        space.ask_example(ANSWER_EXAMPLE)
        space.shot("02-answer.png")

        space.click("SQL that ran")
        space.shot("03-sql-and-trace.png")
        space.click("SQL that ran")          # collapse again before the next question

        space.ask_example(GUARDRAIL_EXAMPLE)
        space.shot("04-guardrail.png")
    finally:
        driver.quit()


if __name__ == "__main__":
    sys.exit(main())
