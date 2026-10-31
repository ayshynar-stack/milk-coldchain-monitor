"""Run the Jenkins pipeline for real and capture screenshots of it.

Used by .github/workflows/jenkins-screenshots.yml on a GitHub runner:
1. waits until the Jenkins started by docker-compose.jenkins.yml is up and the
   job "coldchain-monitor" (created by Configuration as Code) exists;
2. starts build #1 by hand and waits for it;
3. pushes an empty commit and waits for Jenkins to start build #2 by itself
   (SCM polling) - the proof that the pipeline runs on every commit;
4. takes screenshots of the Jenkins pages and of the deployed service and saves
   the console log and Docker state as text files in ./screenshots.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

JENKINS = os.environ.get("JENKINS_URL", "http://localhost:8080").rstrip("/")
USER = os.environ.get("JENKINS_ADMIN_ID", "admin")
PASSWORD = os.environ.get("JENKINS_ADMIN_PASSWORD", "admin")
JOB = f"{JENKINS}/job/coldchain-monitor"
OUT = Path("screenshots")
OUT.mkdir(exist_ok=True)

session = requests.Session()
session.auth = (USER, PASSWORD)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_for(description, check, timeout, interval=5):
    log(f"waiting for {description} (up to {timeout}s)")
    end = time.time() + timeout
    while time.time() < end:
        try:
            result = check()
            if result:
                return result
        except requests.RequestException:
            pass
        time.sleep(interval)
    sys.exit(f"timed out waiting for {description}")


def api(path):
    r = session.get(f"{path}/api/json", timeout=10)
    return r.json() if r.status_code == 200 else None


def trigger_build():
    crumb = session.get(f"{JENKINS}/crumbIssuer/api/json", timeout=10).json()
    r = session.post(f"{JOB}/build", headers={crumb["crumbRequestField"]: crumb["crumb"]},
                     timeout=10)
    log(f"build requested: HTTP {r.status_code}")
    r.raise_for_status()


def wait_build_finished(number, timeout=1500):
    def done():
        info = api(f"{JOB}/{number}")
        return info if info and not info["building"] else None
    info = wait_for(f"build #{number} to finish", done, timeout, 10)
    log(f"build #{number}: {info['result']}")
    return info


def run(cmd):
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return f"$ {cmd}\n{out.stdout}{out.stderr}"


wait_for("Jenkins", lambda: session.get(f"{JENKINS}/login", timeout=10).status_code == 200, 900)
wait_for("the coldchain-monitor job", lambda: api(JOB), 300)

# Build 1: started by hand (registers the pipeline's parameters and triggers).
trigger_build()
wait_for("build #1 to start", lambda: api(f"{JOB}/1"), 300)
first = wait_build_finished(1)

# Build 2: a new commit, picked up by SCM polling (no manual action).
subprocess.run('git config user.name "github-actions[bot]"', shell=True, check=True)
subprocess.run('git config user.email "41898282+github-actions[bot]@users.noreply.github.com"',
               shell=True, check=True)
subprocess.run('git commit --allow-empty -m "ci: commit to demonstrate the Jenkins SCM trigger"',
               shell=True, check=True)
subprocess.run("git push origin HEAD:main", shell=True, check=True)
log("empty commit pushed; Jenkins should notice it within ~2 minutes")
wait_for("build #2 started by the SCM change", lambda: api(f"{JOB}/2"), 600, 10)
second = wait_build_finished(2)
causes = [c.get("shortDescription") for a in second.get("actions", []) for c in a.get("causes", [])]
log(f"build #2 causes: {causes}")

# Text evidence
(OUT / "console_build1.txt").write_text(session.get(f"{JOB}/1/consoleText", timeout=30).text)
(OUT / "console_build2.txt").write_text(session.get(f"{JOB}/2/consoleText", timeout=30).text)
(OUT / "docker_state.txt").write_text(
    run("docker image ls coldchain-monitor") + "\n" + run("docker ps") + "\n"
    + run("curl -s http://localhost:8000/health") + "\n"
    + run("curl -s 'http://localhost:8000/forecast?hours=3'"))
(OUT / "summary.json").write_text(json.dumps(
    {"build1": first["result"], "build2": second["result"], "build2_causes": causes}, indent=2))

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{JENKINS}/login")
    page.fill('input[name="j_username"]', USER)
    page.fill('input[name="j_password"]', PASSWORD)
    page.click('button[name="Submit"], input[name="Submit"]')
    page.wait_for_load_state("networkidle")

    def shot(name, url, full=False, wait_ms=4000):
        page.goto(url)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(wait_ms)
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
        log(f"saved {name}.png")

    shot("01_jenkins_dashboard", f"{JENKINS}/")
    shot("02_stage_view", f"{JOB}/", full=True, wait_ms=6000)
    shot("03_test_result", f"{JOB}/1/testReport/", full=True)
    shot("04_build2_started_by_scm_change", f"{JOB}/2/")
    shot("05_console_output", f"{JOB}/1/console", wait_ms=3000)
    shot("06_service_forecast", "http://localhost:8000/forecast?hours=3", wait_ms=1000)
    shot("07_service_health", "http://localhost:8000/health", wait_ms=1000)
    text = (OUT / "docker_state.txt").read_text().replace("&", "&amp;").replace("<", "&lt;")
    page.set_content(
        "<html><body style='background:#1e1e1e;color:#e6e6e6;margin:0'>"
        "<pre style='font:15px/1.4 monospace;padding:20px'>" + text + "</pre></body></html>"
    )
    page.screenshot(path=str(OUT / "08_docker_images_and_containers.png"), full_page=True)
    log("saved 08_docker_images_and_containers.png")
    browser.close()

if first["result"] != "SUCCESS" or second["result"] != "SUCCESS":
    sys.exit(f"pipeline result: build1={first['result']} build2={second['result']}")
log("done: both builds succeeded")
