import json
from redo import retrier
import requests

import logging
log = logging.getLogger(__name__)


def createJsonArtifact(self, queue, taskId, runId, name, data, expires):
    data = json.dumps(data)
    resp = queue.createArtifact(taskId, runId, name, {
        "storageType": "s3",
        "contentType": "application/json",
        "expires": expires,
    })
    log.debug("Got %s", resp)
    assert resp["storageType"] == "s3"
    putUrl = resp["putUrl"]
    log.debug("Uploading to %s", putUrl)
    for _ in retrier():
        try:
            resp = requests.put(putUrl, data=data, headers={
                "Content-Type": "application/json",
                "Content-Length": len(data),
            })
            log.debug("Got %s %s", resp, resp.headers)
            return
        except Exception:
            log.debug("Error submitting to s3", exc_info=True)
            continue
    else:
        log.error("couldn't upload artifact to s3")
        raise IOError("couldn't upload artifact to s3")
