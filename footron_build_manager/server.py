import hmac
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
import json
from urllib.parse import urljoin

import requests
from hashlib import sha256
from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException, Request
from github import Github

from .config import load_config
from .constants import GITHUB_WEBHOOK_SECRET, GITHUB_ACCESS_TOKEN
from .data import load_build_data, Target as DataTarget, save_build_data

app = FastAPI()
config = load_config()
data = load_build_data()
github = Github(GITHUB_ACCESS_TOKEN)


async def verify_github_webhook(request: Request):
    # Take sha256 hmac of the request body, using GITHUB_WEBHOOK_TOKEN as the key
    if not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_TOKEN not set")
    hmac_sha256 = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), await request.body(), sha256
    ).hexdigest()
    if not request.headers.get("X-Hub-Signature-256") == f"sha256={hmac_sha256}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def github_get_request(url):
    response = requests.get(
        url,
        headers={
            "Authorization": f"token {GITHUB_ACCESS_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    response.raise_for_status()
    return response.json()


def reload_controller(target):
    # Update the controller with the /reload endpoint
    reload_response = requests.get(urljoin(target.controller_api_url, "reload"))
    reload_response.raise_for_status()


def set_commit_status(repository_name, sha, state, context, description=None):
    github.get_repo(repository_name).get_commit(sha).create_status(
        state, context=f"footron-ci/{context}", description=description
    )


def ssh_production_option():
    return f'-o "SetEnv=FT_PRODUCTION={datetime.now().strftime("%d%H")}"'


def rsync_ssh_production_command():
    return f"ssh {ssh_production_option()}"


def handle_workflow_run_completed(event):
    workflow_run = event["workflow_run"]

    if workflow_run["status"] != "completed" or workflow_run["event"] != "push":
        return

    branch = workflow_run["head_branch"]
    sha = workflow_run["head_sha"]

    try:
        target = config.targets[branch]
    except KeyError:
        return

    event_name = workflow_run["name"]
    artifacts_url = workflow_run["artifacts_url"]

    repository_name = event["repository"]["full_name"]

    if event_name not in ["build-controls", "build-experiences"]:
        raise HTTPException(status_code=400, detail="Unknown event name")

    # Start timer
    start_time = time.time()

    if event_name == "build-controls":
        # Request the artifacts endpoint using access token
        artifacts_response = github_get_request(artifacts_url)

        artifacts = artifacts_response["artifacts"]

        experiences_build_url = None
        for artifact in artifacts:
            if artifact["name"] == "web-build":
                experiences_build_url = artifact["archive_download_url"]
                break
        else:
            raise RuntimeError("Missing web build")

        # Download the artifact
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            web_build_zip_path = temp_path / "web-build.zip"
            set_commit_status(
                repository_name, sha, "pending", event_name, "Downloading artifacts"
            )

            subprocess.run(
                [
                    "curl",
                    "-H",
                    f"Authorization: token {GITHUB_ACCESS_TOKEN}",
                    "-L",
                    experiences_build_url,
                    "-o",
                    str(web_build_zip_path),
                ]
            )

            set_commit_status(
                repository_name, sha, "pending", event_name, "Extracting artifacts"
            )
            # Extract the artifacts
            subprocess.run(
                ["unzip", str(web_build_zip_path), "-d", str(temp_path / "build")]
            )

            # rsync the build directory to the target directory
            set_commit_status(
                repository_name, sha, "pending", event_name, "Copying web build"
            )
            print(f'rsync {temp_path / "build"}/ {target.web_path}')
            subprocess.run(
                [
                    "rsync",
                    "-e",
                    rsync_ssh_production_command(),
                    "-a",
                    "--delete",
                    f'{str(temp_path / "build")}/',
                    str(target.web_path),
                ]
            )

            set_commit_status(
                repository_name, sha, "pending", event_name, "Reloading controller"
            )
            reload_controller(target)
    elif event_name == "build-experiences":
        artifacts_response = github_get_request(artifacts_url)

        artifacts = artifacts_response["artifacts"]

        for artifact in artifacts:
            if artifact["name"] == "experiences":
                experiences_build_url = artifact["archive_download_url"]
                break
        else:
            raise RuntimeError("Missing experiences build")

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            web_build_zip_path = temp_path / "experiences.zip"
            # Download the artifact using the access token in a curl header
            set_commit_status(
                repository_name, sha, "pending", event_name, "Downloading artifacts"
            )
            subprocess.run(
                [
                    "curl",
                    "-H",
                    f"Authorization: token {GITHUB_ACCESS_TOKEN}",
                    "-L",
                    experiences_build_url,
                    "-o",
                    str(web_build_zip_path),
                ]
            )

            # Extract the artifacts
            set_commit_status(
                repository_name, sha, "pending", event_name, "Extracting artifacts"
            )
            subprocess.run(["unzip", str(web_build_zip_path), "-d", temp_path])

            set_commit_status(
                repository_name, sha, "pending", event_name, "Comparing hashes"
            )
            hashes_path = temp_path / "hashes.json"
            with open(hashes_path, "r") as hashes_file:
                hashes = json.load(hashes_file)
            if branch not in data.targets:
                data.targets[branch] = DataTarget()

            existing_hashes = data.targets[branch].hashes
            hashes_keys = set(hashes.keys())
            existing_hashes_keys = set(existing_hashes.keys())
            new_hashes = hashes_keys - existing_hashes_keys
            deleted_hashes = existing_hashes_keys - hashes_keys
            overlapping_hashes = hashes_keys & existing_hashes_keys
            changed_hashes = set()

            for hash_key in new_hashes:
                data.targets[branch].hashes[hash_key] = hashes[hash_key]
            for hash_key in overlapping_hashes:
                if hashes[hash_key] != existing_hashes[hash_key]:
                    changed_hashes.add(hash_key)

            controller_host = target.controller_host
            controller_fs_path = target.controller_fs_path

            print("Added experiences:", list(new_hashes))
            print("Deleted experiences:", list(deleted_hashes))
            print("Changed experiences:", list(changed_hashes))

            set_commit_status(
                repository_name,
                sha,
                "pending",
                event_name,
                "Deleting deleted experiences",
            )
            for hash_key in deleted_hashes:
                del data.targets[branch].hashes[hash_key]
                # rm -rf the hash directory on the target host, or the current host
                #  if the host isn't specified
                if controller_host:
                    subprocess.run(
                        [
                            "ssh",
                            *shlex.split(ssh_production_option()),
                            controller_host,
                            f"rm -rf {controller_fs_path}/experiences/{hash_key}",
                        ]
                    )
                else:
                    subprocess.run(
                        [
                            "rm",
                            "-rf",
                            f"{controller_fs_path}/experiences/{hash_key}",
                        ]
                    )

            set_commit_status(
                repository_name,
                sha,
                "pending",
                event_name,
                "Syncing experiences",
            )
            for hash_key in changed_hashes | new_hashes:
                # rsync the changed directory to the directory on the target host
                subprocess.run(
                    [
                        "rsync",
                        "-avP",
                        "--delete",
                        "-e",
                        rsync_ssh_production_command(),
                        # Hack to avoid dealing with video files for now
                        "--exclude=*.mp4",
                        "--exclude=*.webm",
                        f'{str(temp_path / "experiences" / hash_key)}/',
                        f"{target.controller_path}/experiences/{hash_key}",
                    ]
                )

            data.targets[branch].hashes = hashes
            save_build_data(data)

            set_commit_status(
                repository_name, sha, "pending", event_name, "Syncing editorial configs"
            )
            subprocess.run(
                [
                    "rsync",
                    "-avP",
                    "--delete",
                    "-e",
                    rsync_ssh_production_command(),
                    *(
                        f"{str(temp_path)}/{file}.toml"
                        for file in ["folders", "tags", "collections"]
                    ),
                    str(target.controller_path),
                ]
            )

            set_commit_status(
                repository_name, sha, "pending", event_name, "Reloading controller"
            )
            reload_controller(target)

    # "Successful in {}s"
    set_commit_status(
        repository_name,
        sha,
        "success",
        event_name,
        f"Successful in {int(round(time.time() - start_time))}s",
    )


def handle_workflow_job_queued(event):
    workflow_job = event["workflow_job"]

    if workflow_job["name"] != "build":
        return

    run_response = github_get_request(workflow_job["run_url"])
    event_name = run_response["name"]
    if (
        run_response["event"] != "push"
        or event_name
        not in [
            "build-experiences",
            "build-controls",
        ]
        or run_response["branch"] not in config.targets
    ):
        return

    sha = workflow_job["head_sha"]

    repository_name = event["repository"]["full_name"]
    # Use Python GitHub API to set a status on the commit
    github.get_repo(repository_name).get_commit(sha).create_status(
        "pending",
        context=f"footron-ci/{event_name}",
        description="Waiting for build to finish",
    )


@app.post("/build/webhook")
async def handle_webhook(request: Request):
    # Verify the payload signature X-Hub-Signature
    await verify_github_webhook(request)

    payload = await request.json()
    event_type = request.headers.get("X-GitHub-Event")

    if event_type == "workflow_run" and payload["action"] == "completed":
        handle_workflow_run_completed(payload)
    elif event_type == "workflow_job" and payload["action"] == "queued":
        handle_workflow_job_queued(payload)

    # TODO: Handle adding status for pushing to machine
    # https://docs.github.com/en/rest/commits#commit-statuses

    return {"status": "ok"}
