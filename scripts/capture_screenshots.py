"""Capture README screenshots of the Streamlit demo with headless Chrome.

Start the app first:
    streamlit run streamlit_app.py --server.port 8501 --server.headless true
then:
    python scripts/capture_screenshots.py

Screenshots are plain viewport captures (WYSIWYG). CDP's captureBeyondViewport
composites inconsistently with Streamlit's fixed sidebar and can return a stale
frame, so the window is simply made tall enough for the content instead.
"""
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
URL = "http://localhost:8501"
WIDTH, HEIGHT = 1560, 1700

# Questions are asked via the example buttons: they set the question through
# session state. Typing doesn't commit the value until Enter/blur, which would
# leave the Ask button disabled.
ANSWER_EXAMPLE = "Which departments were over budget last quarter"
REFUSAL_EXAMPLE = "Delete all the void invoices"


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


def buttons(driver, text: str):
    return driver.find_elements(By.XPATH, f"//button[.//p[contains(normalize-space(.), {text!r})]]")


def busy(driver) -> bool:
    return bool(driver.find_elements(By.CSS_SELECTOR, "[data-testid='stSpinner']"))


def answered(driver) -> bool:
    """Report heading plus metric tiles, and the spinner gone."""
    return (bool(driver.find_elements(By.CSS_SELECTOR, "h4"))
            and bool(driver.find_elements(By.CSS_SELECTOR, "[data-testid='stMetric']"))
            and not busy(driver))


def scroll_top(driver) -> None:
    driver.execute_script(
        "window.scrollTo(0, 0);"
        "document.querySelectorAll('section.main, [data-testid=\"stAppViewContainer\"], "
        "[data-testid=\"stMain\"]').forEach(el => el.scrollTop = 0);"
    )
    time.sleep(0.5)


def shot(driver, name: str, scroll_to: str | None = None) -> None:
    time.sleep(1.5)
    if scroll_to:
        for el in driver.find_elements(By.XPATH, f"//*[contains(text(), {scroll_to!r})]")[:1]:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            time.sleep(1)
    else:
        scroll_top(driver)
    path = OUT / name
    driver.save_screenshot(str(path))
    kb = path.stat().st_size // 1024
    print(f"  {path.relative_to(ROOT)}  ({kb} KB)" + ("   <-- suspiciously small" if kb < 10 else ""))


def ask_example(driver, example_text: str, timeout: float = 240) -> None:
    scroll_top(driver)
    wait_for(lambda: buttons(driver, example_text), 30, f"the {example_text!r} button")[0].click()
    time.sleep(2)  # rerun: the question lands in the box and enables Ask
    ask = wait_for(lambda: [b for b in buttons(driver, "Ask") if b.is_enabled()], 30, "Ask to enable")[0]
    ask.click()
    time.sleep(2)
    wait_for(lambda: answered(driver), timeout, "the agent to answer")
    time.sleep(3)  # let the result table and expanders finish rendering


def expand(driver, label: str) -> bool:
    for el in driver.find_elements(By.XPATH, f"//summary[contains(., {label!r})]"):
        expanded = driver.execute_script("return arguments[0].parentElement.open === true;", el)
        if not expanded:
            driver.execute_script("arguments[0].click();", el)
            time.sleep(2)
        return bool(driver.execute_script("return arguments[0].parentElement.open === true;", el))
    return False


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument(f"--window-size={WIDTH},{HEIGHT}")
    options.add_argument("--hide-scrollbars")
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_window_size(WIDTH, HEIGHT)
        driver.get(URL)
        print("waiting for the warehouse to seed and the catalog to build...")
        wait_for(lambda: buttons(driver, "Net revenue by customer region"), 300, "the app to bootstrap")
        time.sleep(3)
        print("capturing:")
        shot(driver, "01-landing.png")

        ask_example(driver, ANSWER_EXAMPLE)
        shot(driver, "02-answer.png")

        for label in ("SQL that ran", "How the agent got there"):
            print(f"  expanded {label!r}: {expand(driver, label)}")
        shot(driver, "03-sql-and-trace.png", scroll_to="SQL that ran")

        ask_example(driver, REFUSAL_EXAMPLE)
        shot(driver, "04-guardrail.png")
    finally:
        driver.quit()


if __name__ == "__main__":
    sys.exit(main())
