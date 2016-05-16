import asyncio
import json
import logging
import os
import random
# from shutil import copyfile
import traceback
import urlparse

import sh
import requests

from scriptworker.client import get_temp_creds_from_file
from scriptworker.utils import retry_request
from signingscript.task import task_cert_type, \
    task_signing_formats, validate_task_schema
from signingscript.exceptions import TaskVerificationError, \
    ChecksumMismatchError, SigningServerError
from signingscript.utils import get_hash, get_detached_signatures, load_signing_server_config

log = logging.getLogger(__name__)


async def process_message(context, body, message):
    # move this to async_main ?
    task_id = None
    run_id = None
    work_dir = context.config['work_dir']
    task = {}
    try:
        await validate_task_schema(context)
        # TODO validate graph/decision task?
        await sign(task_id, run_id, task, work_dir)
        # copy to artifact_dir
    except (TaskVerificationError, ):
        log.exception("Cannot verify task, %s", body)
        raise
    except Exception:
        log.exception("Error processing %s", body)
        # what to do here?


async def download_and_sign_file(context, task_id, run_id, url, checksum, cert_type,
                                 signing_formats, work_dir):
    # TODO: better parsing
    filename = urlparse.urlsplit(url).path.split("/")[-1]
    abs_filename = os.path.join(work_dir, filename)
    log.debug("Downloading %s", url)
    # TODO aiohttp
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(abs_filename, 'wb') as fd:
        for chunk in r.iter_content(4096):
            fd.write(chunk)
    log.debug("Done")
    got_checksum = get_hash(abs_filename)
    log.info("SHA512SUM: %s URL: %s", got_checksum, url)
    log.info("SHA1SUM: %s URL: %s", get_hash(abs_filename, "sha1"), url)
    if not got_checksum == checksum:
        msg = "CHECKSUM MISMATCH: Expected {}, got {} for {}".format(
            checksum, got_checksum, url)
        log.debug(msg)
        raise ChecksumMismatchError(msg)
    log.debug("Signing %s", filename)
    await sign_file(work_dir, filename, cert_type, signing_formats)
    # TODO
    # create_artifact(task_id, run_id, "public/env/%s" % filename,
    #                abs_filename)
    detached_signatures = []
    for s_type, s_ext, s_mime in get_detached_signatures(signing_formats):
        d_filename = "{filename}{ext}".format(filename=filename,
                                              ext=s_ext)
        d_abs_filename = os.path.join(work_dir, d_filename)
        assert d_abs_filename  # TODO silence flake8 until we copy
    # TODO
    #    create_artifact(task_id, run_id, "public/env/%s" % d_filename,
    #                    d_abs_filename, content_type=s_mime)
    # copy detached_signatures to artifact dir
        detached_signatures.append((s_type, d_filename))
    return abs_filename, detached_signatures


# @redo.retriable(attempts=10, sleeptime=5, max_sleeptime=30)
async def get_token(context, output_file, cert_type, signing_formats):
    token = None
    data = {"slave_ip": context.config['my_ip'], "duration": 5 * 60}
    all_signing_servers = load_signing_server_config(context)
    signing_servers = get_suitable_signing_servers(
        all_signing_servers, cert_type,
        signing_formats
    )
    random.shuffle(signing_servers)
    for s in signing_servers:
        log.debug("getting token from %s", s.server)
        # TODO: Figure out how to deal with certs not matching hostname,
        #  error: https://gist.github.com/rail/cbacf2d297decb68affa
        # TODO aiohttp
        url = "https://{}/token".format(s.server)
        try:
            token = retry_request(context, url, method='post', data=data,
                                    auth=(s.user, s.password), return_type='content')
            if token:
                break
        except ScriptWorkerException:
            traceback.print_exc()
            continue
    else:
        raise SigningServerError("Cannot retrieve signing token")
    with open(output_file, "wb") as f:
        f.write(token)


async def sign_file(context, work_dir, from_, cert_type, signing_formats, cert, to=None):
    if to is None:
        to = from_
    token = os.path.join(work_dir, "token")
    nonce = os.path.join(work_dir, "nonce")
    get_token(token, cert_type, signing_formats)
    # TODO path to tools
    signtool = os.path.join("tools_checkout", "release/signing/signtool.py")
    cmd = [signtool, "-n", nonce, "-t", token, "-c", cert]
    for s in get_suitable_signing_servers(cert_type, signing_formats):
        cmd.extend(["-H", s.server])
    for f in signing_formats:
        cmd.extend(["-f", f])
    cmd.extend(["-o", to, from_])
    log.debug("Running python %s", " ".join(cmd))
    # aiohttp.subprocess?
    out = sh.python(*cmd, _err_to_out=True, _cwd=work_dir)
    log.debug("COMMAND OUTPUT: %s", out)
    abs_to = os.path.join(work_dir, to)
    log.info("SHA512SUM: %s SIGNED_FILE: %s",
             get_hash(abs_to, "sha512"), to)
    log.info("SHA1SUM: %s SIGNED_FILE: %s",
             get_hash(abs_to, "sha1"), to)
    log.debug("Finished signing")


def get_suitable_signing_servers(signing_servers, cert_type, signing_formats):
    return [s for s in signing_servers[cert_type] if set(signing_formats) & set(s.formats)]


async def read_temp_creds(context):
    await asyncio.sleep(context.config['temp_creds_refresh_seconds'])
    await get_temp_creds_from_file(context.config)


def sign(context, task_id, run_id):
    payload = context.task["payload"]
    # Will we know the artifacts, be able to create the manifest at decision task time?
    manifest_url = payload["signingManifest"]
    work_dir = context.config['work_dir']
    try:
        signing_manifest = await retry_request(context, manifest_url, return_type=json)
    except ScriptWorkerException:
        traceback.print_exc()
        continue
    # TODO: better way to extract filename
    url_prefix = "/".join(manifest_url.split("/")[:-1])
    cert_type = task_cert_type(context.task)
    signing_formats = task_signing_formats(context.task)
    for e in signing_manifest:
        # Fallback to "mar" if "file_to_sign" is not specified
        file_to_sign = e.get("file_to_sign", e.get("mar"))
        file_url = "{}/{}".format(url_prefix, file_to_sign)
        abs_filename, detached_signatures = download_and_sign_file(
            task_id, run_id, file_url, e["hash"], cert_type,
            signing_formats, work_dir)
        # Update manifest data with new values
        e["hash"] = get_hash(abs_filename)
        e["size"] = os.path.getsize(abs_filename)
        e["detached_signatures"] = {}
        for sig_type, sig_filename in detached_signatures:
            e["detached_signatures"][sig_type] = sig_filename
    manifest_file = os.path.join(work_dir, "manifest.json")
    with open(manifest_file, "wb") as f:
        json.dump(signing_manifest, f, indent=2, sort_keys=True)
    log.debug("Uploading manifest for t: %s, r: %s", task_id, run_id)
    # TODO move to artifact_dir
    # create_artifact(task_id, run_id, "public/env/manifest.json",
    #                manifest_file, "application/json")