"""Opt-in browser acceptance test for the deployed or local incident cockpit."""

import os

import pytest

BASE_URL = os.environ.get("HINDSIGHT_BROWSER_BASE_URL")
OPERATOR_TOKEN = os.environ.get("HINDSIGHT_BROWSER_OPERATOR_TOKEN")

requires_browser = pytest.mark.skipif(
    not BASE_URL or not OPERATOR_TOKEN,
    reason="browser URL and operator token are not configured",
)


@requires_browser
def test_operator_can_run_and_explain_signature_workflow():
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.support import expected_conditions as expected
    from selenium.webdriver.support.ui import WebDriverWait

    options = Options()
    options.add_argument("-headless")
    driver = webdriver.Firefox(options=options)
    wait = WebDriverWait(driver, 30)
    try:
        driver.set_window_size(1440, 1000)
        driver.get(BASE_URL)
        wait.until(expected.presence_of_element_located((By.ID, "memories")))
        assert driver.find_element(By.ID, "startRun").get_attribute("disabled")

        driver.find_element(By.ID, "operatorButton").click()
        driver.find_element(By.ID, "operatorToken").send_keys(OPERATOR_TOKEN)
        driver.find_element(By.CSS_SELECTOR, "#operatorForm button[type=submit]").click()
        wait.until_not(lambda browser: browser.find_element(By.ID, "startRun").get_attribute("disabled"))

        driver.find_element(By.ID, "resetDemo").click()
        wait.until(lambda browser: "1 live" in browser.find_element(By.ID, "memoryCount").text)

        driver.find_element(By.ID, "startRun").click()
        wait.until(lambda browser: "awaiting approval" in browser.find_element(By.ID, "runStatus").text)
        driver.find_element(By.ID, "approveRun").click()
        wait.until(lambda browser: browser.find_element(By.ID, "runStatus").text == "completed")
        wait.until(lambda browser: "1 read" in browser.find_element(By.ID, "influenceCount").text)

        driver.find_element(By.ID, "poisonDemo").click()
        wait.until(lambda browser: "2 live" in browser.find_element(By.ID, "memoryCount").text)
        driver.find_element(By.ID, "previewRewind").click()
        wait.until(lambda browser: "versions will close" in browser.find_element(By.ID, "rewindPreview").text)
    finally:
        driver.quit()
