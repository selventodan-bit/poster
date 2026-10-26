"""Create-once Drive admission: durable uncertainty is a stop, never a retry."""

import time
import uuid

from .crypto import sign_result, verify_result
from .errors import CloudError
from .format import canonical, identifier, metadata, version_id


async def claims(library, descriptor):
    cursor, seen, rows = descriptor["run_slot"], set(), []
    while await library.files.info(cursor) is not None:
        if cursor in seen or len(seen) > 10000:
            raise CloudError("INTEGRITY")
        seen.add(cursor)
        value = await library.files.json(cursor)
        if (
            value.get("format") != "poster.cloud.claim.v1"
            or value.get("project_id") != descriptor["project_id"]
        ):
            raise CloudError("INTEGRITY")
        value["id"] = cursor
        identifier(value["next_slot"])
        identifier(value["completion_id"])
        identifier(value["resolution_id"])
        version_id(value["version"])
        if not 1 <= len(value["runs"]) <= 1000:
            raise CloudError("INTEGRITY")
        for run in value["runs"]:
            if len(run) != 3:
                raise CloudError("INTEGRITY")
            for part in run:
                identifier(part)
        rows.append(value)
        cursor = value["next_slot"]
    return rows, cursor


async def completed(library, claim):
    if await library.files.info(claim["completion_id"]) is None:
        return None
    value = verify_result(claim["issuer"], await library.files.json(claim["completion_id"]))
    if value.get("format") != "poster.cloud.batch.v1" or value.get("claim_id") != claim["id"]:
        raise CloudError("RESULT_UNTRUSTED")
    if len(value.get("results", [])) != len(claim["runs"]):
        raise CloudError("RESULT_UNTRUSTED")
    for plan, result in zip(claim["runs"], value["results"], strict=True):
        if result.get("run_id") != plan[0] or result.get("manifest_id") != plan[2]:
            raise CloudError("RESULT_UNTRUSTED")
        raw = await library.files.read(plan[2])
        from .format import digest

        if digest(raw) != result.get("sha256"):
            raise CloudError("RESULT_UNTRUSTED")
        report = verify_result(claim["issuer"], metadata(raw))
        if (
            report.get("run_id") != plan[0]
            or report.get("claim_id") != claim["id"]
            or report.get("status") != result.get("status")
        ):
            raise CloudError("RESULT_UNTRUSTED")
    return value


async def acquire(library, descriptor, bundle, intent, identity, count, parallel):
    history, slot = await claims(library, descriptor)
    if any(item["intent"] == intent for item in history):
        raise CloudError("RUN_ALREADY_CLAIMED")
    if history:
        previous = history[-1]
        completion = await completed(library, previous)
        uncertain = not completion or any(
            r["status"] in {"UNKNOWN", "REVIEW_REQUIRED"} for r in completion["results"]
        )
        if uncertain:
            resolution = await library.files.info(previous["resolution_id"])
            if resolution is None:
                raise CloudError("RUN_UNCERTAIN")
            value = await library.files.json(previous["resolution_id"])
            if value != dict(format="poster.cloud.manual-review.v1", claim_id=previous["id"], reviewed=True):
                raise CloudError("INTEGRITY")
    ids = []
    remaining = 3 + 2 * count
    while remaining:
        chunk = min(remaining, 1000)
        ids.extend(await library.files.ids(chunk))
        remaining -= chunk
    values = iter(ids)
    claim = dict(
        format="poster.cloud.claim.v1",
        mode="DETACHED",
        project_id=bundle.project_id,
        version=bundle.version,
        intent=identifier(intent),
        issuer=identity.public(),
        next_slot=next(values),
        completion_id=next(values),
        resolution_id=next(values),
        parallel=parallel,
        created=time.time(),
        runs=[[uuid.uuid4().hex, next(values), next(values)] for _ in range(count)],
    )
    if len(canonical(claim)) > 200000:
        raise CloudError("PACKAGE_LIMIT")
    # strict_new does not retry after any ambiguous response, even if an
    # identical claim can later be read back. Only this successful creator runs.
    await library.files.create_json(
        slot, "execution-claim.json", descriptor["folders"]["results"], claim, strict_new=True
    )
    return dict(**claim, id=slot)


async def resolve(library, claim, *, explicitly_reviewed=False):
    if explicitly_reviewed is not True:
        raise CloudError("RUN_UNCERTAIN")
    await library.files.create_json(
        claim["resolution_id"],
        "manual-review.json",
        (await library.descriptor(await library.find(claim["project_id"])))["folders"]["results"],
        dict(format="poster.cloud.manual-review.v1", claim_id=claim["id"], reviewed=True),
    )


async def finish(library, descriptor, claim, identity, results):
    value = sign_result(
        identity,
        dict(
            format="poster.cloud.batch.v1",
            mode="DETACHED",
            issuer=identity.public()["id"],
            claim_id=claim["id"],
            results=results,
        ),
    )
    await library.files.create_json(
        claim["completion_id"], "committed-batch.json", descriptor["folders"]["results"], value
    )
