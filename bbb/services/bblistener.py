import arrow

from ..servicebase import ListenerService
from ..tcutils import createJsonArtifact

import logging
log = logging.getLogger(__name__)


# Buildbot status'- these must match http://mxr.mozilla.org/build/source/buildbot/master/buildbot/status/builder.py#25
SUCCESS, WARNINGS, FAILURE, SKIPPED, EXCEPTION, RETRY, CANCELLED = range(7)

class BuildbotListener(ListenerService):
    def __init__(self, tcWorkerGroup, tcWorkerId, *args, **kwargs):
        self.tcWorkerGroup = tcWorkerGroup
        self.tcWorkerId = tcWorkerId
        eventHandlers = {
            "started": self.handleStarted,
            "log_uploaded": self.handleFinished,
        }

        super(BuildbotListener, self).__init__(*args, eventHandlers=eventHandlers, **kwargs)

    def getEvent(self, data):
        return data["_meta"]["routing_key"].split(".")[-1]

    def handleStarted(self, data, msg):
        # TODO: Error handling?
        buildnumber = data["payload"]["build"]["number"]
        for brid in self.buildbot_db.getBuildRequests(buildnumber):
            brid = brid[0]
            task = self.bbb_db.getTaskFromBuildRequest(brid)
            log.info("Claiming %s", task.taskId)
            claim = self.tc_queue.claimTask(task.taskId, task.runId, {
                "workerGroup": self.tcWorkerGroup,
                "workerId": self.tcWorkerId,
            })
            log.debug("Got claim: %s", claim)
            self.bbb_db.updateTakenUntil(brid, claim["takenUntil"])

    def handleFinished(self, data, msg):
        # Get the request_ids from the properties
        try:
            properties = dict((key, (value, source)) for (key, value, source) in data["payload"]["build"]["properties"])
        except KeyError:
            log.error("Couldn't get job properties")
            return

        request_ids = properties.get("request_ids")
        if not request_ids:
            log.error("Couldn't get request ids from %s", data)
            return

        # Sanity check
        assert request_ids[1] == "postrun.py"

        try:
            results = data["payload"]["build"]["results"]
        except KeyError:
            log.error("Couldn't find job results")
            return

        # For each request, get the taskId and runId
        for brid in request_ids[0]:
            try:
                task = self.bbb_db.getTaskFromBuildRequest(brid)
                taskId = task.taskId
                runId = task.runId
            except ValueError:
                log.error("Couldn't find task for %i", brid)
                continue

            log.debug("brid %i : taskId %s : runId %i", brid, taskId, runId)

            # Attach properties as artifacts
            log.info("Attaching properties to task %s", taskId)
            expires = arrow.now().replace(weeks=1).isoformat()
            createJsonArtifact(self.tc_queue, taskId, runId, "properties.json", properties, expires)

            log.info("Buildbot results are %s", results)
            if results == SUCCESS:
                log.info("Marking task %s as completed", taskId)
                self.tc_queue.reportCompleted(taskId, runId, {"success": True})
                self.bbb_db.deleteBuildRequest(brid)
            # Eventually we probably need to set something different here.
            elif results in (WARNINGS, FAILURE):
                log.info("Marking task %s as failed", taskId)
                self.tc_queue.reportFailed(taskId, runId)
                self.bbb_db.deleteBuildRequest(brid)
            # Should never be set for builds, but just in case...
            elif results == SKIPPED:
                pass
            elif results == EXCEPTION:
                log.info("Marking task %s as malformed payload exception", taskId)
                self.tc_queue.reportException(taskId, runId, {"reason": "malformed-payload"})
                self.bbb_db.deleteBuildRequest(brid)
            elif results == RETRY:
                log.info("Marking task %s as malformed payload exception and rerunning", taskId)
                self.tc_queue.reportException(taskId, runId, {"reason": "malformed-payload"})
                self.tc_queue.rerunTask(taskId)
            elif results == CANCELLED:
                log.info("Marking task %s as cancelled", taskId)
                self.tc_queue.cancelTask(taskId)
                self.bbb_db.deleteBuildRequest(brid)
